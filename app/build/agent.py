"""The two LLM-backed steps of the build agent, plus offline stand-ins.

1. Extractor: input files (drawing, sketch, photo, PDF) -> DimensionSpec draft.
2. Planner: confirmed DimensionSpec (+ inputs for context) -> BuildPlan.

Both return structured JSON validated against the pydantic schemas. Neither
ever produces geometry or judges it; builder.py builds and verify.py judges.

Claude is opt-in (PARTS_CAD_LLM=anthropic) because it sends customer files to
the API. Use an organisation with zero-data-retention terms for real jobs.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import os
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.build.schemas import KIND_HELP, AttemptRecord, BuildPlan, DimensionSpec
from app.build.wire import PlanInvalid, WirePlan, from_plan, to_plan

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_IMAGE_EDGE = 2000


class LLMError(Exception):
    """The API call itself failed (auth, network, refusal, truncation). Not retried by the loop."""


def llm_status() -> tuple[bool, str]:
    if os.environ.get("PARTS_CAD_LLM", "").lower() != "anthropic":
        return False, ("Claude is off. Set PARTS_CAD_LLM=anthropic (and ANTHROPIC_API_KEY, or log in with `ant auth login`) "
                       "to enable extraction and planning. This sends input files to the Anthropic API.")
    return True, f"Claude enabled ({os.environ.get('PARTS_CAD_MODEL', DEFAULT_MODEL)})."


# --------------------------------------------------------------------------
# Schema + content helpers
# --------------------------------------------------------------------------


def _strictify(node):
    """Prepare a pydantic JSON schema for structured outputs: const -> enum,
    drop keywords the API ignores, and require every property (optional ones
    are nullable, so the model writes null instead of omitting them)."""
    if isinstance(node, dict):
        node = {k: _strictify(v) for k, v in node.items() if k not in ("default", "title", "discriminator")}
        if "const" in node:
            node["enum"] = [node.pop("const")]
        if node.get("type") == "object" and "properties" in node:
            node["required"] = list(node["properties"].keys())
        return node
    if isinstance(node, list):
        return [_strictify(v) for v in node]
    return node


def output_schema(model: type[BaseModel]) -> dict:
    import anthropic

    return anthropic.transform_schema(_strictify(model.model_json_schema()))


def input_blocks(paths: list[Path]) -> list[dict]:
    """Images (downscaled if huge) and PDFs as Messages API content blocks."""
    blocks: list[dict] = []
    for p in paths:
        suffix = p.suffix.lower()
        if suffix == ".pdf":
            blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                         "data": base64.standard_b64encode(p.read_bytes()).decode()}})
        elif suffix in IMAGE_TYPES:
            data, media = p.read_bytes(), IMAGE_TYPES[suffix]
            from PIL import Image

            with Image.open(io.BytesIO(data)) as im:
                if max(im.size) > MAX_IMAGE_EDGE or len(data) > 4_500_000:
                    im = im.convert("RGB")
                    im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
                    buf = io.BytesIO()
                    im.save(buf, "PNG", optimize=True)
                    data, media = buf.getvalue(), "image/png"
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                      "data": base64.standard_b64encode(data).decode()}})
    return blocks


# --------------------------------------------------------------------------
# Claude client
# --------------------------------------------------------------------------


class ClaudeJSON:
    """One structured-output request: system + user content -> validated model."""

    def __init__(self, client=None, model: str | None = None, effort: str | None = None):
        self._client = client
        self.model = model or os.environ.get("PARTS_CAD_MODEL", DEFAULT_MODEL)
        self.effort = effort or os.environ.get("PARTS_CAD_EFFORT", "high")

    @property
    def client(self):
        if self._client is None:
            import anthropic

            # User-scoped keys (not tied to a workspace) must name the workspace to bill.
            ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
            self._client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": ws} if ws else None)
        return self._client

    def call(self, system: str, content: list[dict], out: type[T]) -> tuple[T, dict]:
        import anthropic

        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # re-run on a fallback model if a safety classifier declines
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": output_schema(out)}},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": content}],
            ) as stream:
                msg = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed: set ANTHROPIC_API_KEY or run `ant auth login`.") from e
        except anthropic.PermissionDeniedError as e:
            raise LLMError(f"Anthropic API permission denied: {e.message}") from e
        except anthropic.NotFoundError as e:
            raise LLMError(f"Model or endpoint not found ({self.model}): {e.message}") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Anthropic rate limit hit; try again shortly.") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Anthropic rejected the request: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Anthropic API.") from e

        usage = {
            "model": getattr(msg, "model", self.model),
            "request_id": getattr(msg, "_request_id", None),
            "input_tokens": msg.usage.input_tokens,
            "output_tokens": msg.usage.output_tokens,
            "cache_read_input_tokens": getattr(msg.usage, "cache_read_input_tokens", None),
            "cache_creation_input_tokens": getattr(msg.usage, "cache_creation_input_tokens", None),
            "stop_reason": msg.stop_reason,
        }
        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise LLMError(f"The model declined this request (category: {cat or 'unspecified'}).")
        if msg.stop_reason == "max_tokens":
            raise LLMError("The model's answer was cut off (max_tokens).")
        text = next((b.text for b in msg.content if b.type == "text"), None)
        if text is None:
            raise LLMError("The model returned no JSON.")
        try:
            return out.model_validate_json(text), usage
        except ValidationError as e:
            raise PlanInvalid(f"Answer did not validate: {e}") from e


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------

DATUM = """Coordinates are millimetres with the datum at the part's minimum corner: X to the right and Y up (away from the viewer) in the top view, Z up from the bottom face. In a third-angle drawing the TOP view shows X (right) and Y (up), the FRONT view shows X (right) and Z (up), and the RIGHT view shows Y (right) and Z (up)."""

EXTRACT_SYSTEM = f"""You read engineering inputs for machined parts (drawings, PDF prints, hand sketches, photos) and write down the dimensions a CAD model must satisfy. A separate program will build a model and then measure it against your list, so every entry must be measurable exactly as you describe it. A wrong number produces a wrong part that passes its own check, so accuracy matters more than completeness.

{DATUM} If the drawing is first-angle, map its views accordingly and say so in a note. Convert inches to mm (x25.4) and set units_on_input.

Dimension kinds (what the measuring program does with each):
""" + "\n".join(f"- {k}: {v}" for k, v in KIND_HELP.items()) + """

How to place points and axes:
- Give all three overall sizes (axis x, y and z).
- Holes: put `at` on the hole's axis at its entry face. Record diameter, depth (or through=true), and each located coordinate as a hole_position with axis x/y/z and feature_diameter. Use hole_count for patterns ("4X"). For counterbores give both diameters at the same `at`, and set feature_diameter on depth/position entries to say which one.
- Pocket and step depths measured from the top: kind depth, direction -z, `at` inside the pocket where no hole pierces the floor. Depths from other faces use that face's direction (+z from the bottom, -x from the right side, +x from the left side, and so on).
- Pocket or slot length/width: gap_width along the axis, with `at` at the pocket centre at mid-depth.
- Edge distances and wall thicknesses ("pocket starts 25 from the left edge"): material_width along the axis, with `at` inside the material between the two faces at the same height as the feature.
- Fillets and corner radii: radius with `at` on the arc (for a corner fillet, its midpoint) and axis parallel to the arc's axis.

Rules:
- Record only what the input states, or what follows from it by arithmetic (say what arithmetic in `note`).
- Never guess a missing or unreadable value. Leave it out and add an open question; make it blocking when the part can't be built without it.
- A photo or sketch with no written dimensions and no reference object of known size has no scale: set scale_basis "none", return no dimensions, and ask for dimensions.
- tol_plus/tol_minus: the stated tolerance, else the general tolerance from the title block, else null.
- confidence below 0.8 also gets a non-blocking open question asking the shop to confirm that value.
- label: copy the callout as written, e.g. "Ø8.5 THRU 4X", "R5", "25".
- description: one or two sentences on the stock form and features (which face each feature is on), for the person building the plan.
- Copy material and finish notes if present.
- Number dimensions D1, D2, ... and open questions Q1, Q2, ...; questions go to the shop unless only the customer can answer."""

PLAN_SYSTEM = f"""You turn a confirmed list of dimensions into a build plan for a deterministic CAD builder. The builder executes your plan exactly. A separate program then measures the result: the plan passes only if every confirmed dimension measures within 0.01 mm of nominal, the part is one closed solid, every hole you drill survives as a complete hole, and the model has no hole the dimensions don't mention.

{DATUM}

The plan format is flat: every field is always present, and a field an object doesn't use holds its "not used" value: 0 for numbers, [] for lists, "none" for choices. Points are [x, y, z] lists.

Stock (choose one kind; fill only its fields):
- box: size_x, size_y, size_z; corner_radius rounds the four vertical edges (0 = sharp).
- cylinder: diameter and height along Z; its axis is at (d/2, d/2).
- extrusion: closed outline `points` [[u, v], ...] on plane xy (extruded +Z), xz (extruded +Y) or yz (extruded +X), with depth; optional concave_corner_radius / convex_corner_radius. Use it for L-brackets, T-sections and non-rectangular outlines.

Operations, applied in order:
- hole: diameter; direction = drilling direction into the material (-z = from the top face down); positions = entry points on the face drilled from (for -z holes, z is that face's height), or bolt_circle = [cx, cy, cz, pitch_diameter, count, start_angle_deg]; depth (0 = through all); counterbore_diameter + counterbore_depth (0 = none).
- pocket: center on the entry face; direction; length along the first in-plane axis (x for ±z, y for ±x, x for ±y) and width along the second (y for ±z, z for ±x and ±y); depth (0 = through); corner_radius (width/2 gives an obround slot); angle_deg.
- boss: adds material. shape cylinder (diameter) or box (length, width), growing `height` from center along direction.
- cut_box: removes the axis-aligned region corner_min..corner_max (steps, notches, rebates). Extend it past the part where the cut is open. Optional corner_radius on edges parallel to corner_axis.

Rules:
- Every number must come from the confirmed dimensions or follow from them by arithmetic. List the ids of the dimensions each operation implements in `dims`.
- Anything the dimensions don't fix (an unstated depth, a feature only mentioned in the description) goes in `assumptions` with the reason. Leaving a feature out is better than inventing one.
- Order: stock, then bosses, then cuts and pockets, then holes, so later cuts don't destroy holes.
- A hole that starts on a pocket floor has its entry point on that floor.
- Give operations short unique ids (P1, H1, ...).
- If you are shown a previous attempt with verification failures, fix exactly those problems and keep everything that passed unchanged.

Example: a 100 x 60 x 20 plate with a centred 40 x 20 x 5 deep pocket (R3 corners) and two Ø6 through holes at (10, 30) and (90, 30):
{{"stock": {{"kind": "box", "size_x": 100, "size_y": 60, "size_z": 20, "corner_radius": 0, "diameter": 0, "height": 0, "plane": "none", "points": [], "depth": 0, "concave_corner_radius": 0, "convex_corner_radius": 0}},
 "operations": [
  {{"op": "pocket", "id": "P1", "dims": ["D4", "D5", "D6", "D7"], "direction": "-z", "positions": [], "bolt_circle": [], "diameter": 0, "depth": 5, "counterbore_diameter": 0, "counterbore_depth": 0, "center": [50, 30, 20], "length": 40, "width": 20, "corner_radius": 3, "angle_deg": 0, "shape": "none", "height": 0, "corner_min": [], "corner_max": [], "corner_axis": "none"}},
  {{"op": "hole", "id": "H1", "dims": ["D8", "D9", "D10"], "direction": "-z", "positions": [[10, 30, 20], [90, 30, 20]], "bolt_circle": [], "diameter": 6, "depth": 0, "counterbore_diameter": 0, "counterbore_depth": 0, "center": [], "length": 0, "width": 0, "corner_radius": 0, "angle_deg": 0, "shape": "none", "height": 0, "corner_min": [], "corner_max": [], "corner_axis": "none"}}],
 "assumptions": []}}"""


# --------------------------------------------------------------------------
# Extractors and planners
# --------------------------------------------------------------------------


class Extractor(Protocol):
    def extract(self, inputs: list[Path], notes: str | None) -> tuple[DimensionSpec, dict | None]: ...


class Planner(Protocol):
    source: str

    def plan(self, spec: DimensionSpec, inputs: list[Path], previous: AttemptRecord | None) -> tuple[BuildPlan, dict | None]: ...


class ClaudeExtractor:
    def __init__(self, llm: ClaudeJSON | None = None):
        self.llm = llm or ClaudeJSON()

    def extract(self, inputs: list[Path], notes: str | None) -> tuple[DimensionSpec, dict]:
        content = input_blocks(inputs)
        if not content:
            raise LLMError("No readable inputs (images or PDF) to extract from.")
        content.append({"type": "text", "text": (f"Shop notes: {notes}\n\n" if notes else "") +
                        "Write the dimension spec for this part."})
        try:
            return self.llm.call(EXTRACT_SYSTEM, content, DimensionSpec)
        except PlanInvalid as e:
            raise LLMError(str(e)) from e


class ClaudePlanner:
    source = "planner"

    def __init__(self, llm: ClaudeJSON | None = None, include_inputs: bool = True):
        self.llm = llm or ClaudeJSON()
        self.include_inputs = include_inputs

    def plan(self, spec: DimensionSpec, inputs: list[Path], previous: AttemptRecord | None) -> tuple[BuildPlan, dict]:
        content = input_blocks(inputs) if self.include_inputs else []
        text = "Confirmed dimensions (JSON):\n" + spec.model_dump_json(indent=1)
        if previous is not None:
            text += "\n\nPrevious attempt plan (JSON):\n" + (from_plan(previous.plan).model_dump_json() if previous.plan else "none")
            problems = []
            if previous.plan_error:
                problems.append(previous.plan_error)
            if previous.build_error:
                problems.append("Build error: " + previous.build_error)
            if previous.report:
                problems += previous.report.failures()
            text += "\n\nVerification failures to fix:\n" + "\n".join(f"- {p}" for p in problems)
        content.append({"type": "text", "text": text + "\n\nWrite the build plan."})
        wire, usage = self.llm.call(PLAN_SYSTEM, content, WirePlan)
        return to_plan(wire), usage


class StaticExtractor:
    """Returns a fixed spec (benchmark oracle, tests)."""

    def __init__(self, spec: DimensionSpec):
        self.spec = spec

    def extract(self, inputs, notes):
        return self.spec.model_copy(deep=True), None


class StaticPlanner:
    """Returns a fixed plan, optionally mutated (benchmark oracle and mutant runs)."""

    def __init__(self, plan: BuildPlan, mutate: Callable[[dict], str] | None = None, source: str = "oracle"):
        self._plan, self._mutate, self.source = plan, mutate, source
        self.mutation: str | None = None

    def plan(self, spec, inputs, previous):
        data = copy.deepcopy(self._plan.model_dump())
        if self._mutate:
            self.mutation = self._mutate(data)
        return BuildPlan.model_validate(data), None


def plan_from_json(text: str) -> BuildPlan:
    try:
        return BuildPlan.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as e:
        raise PlanInvalid(str(e)) from e

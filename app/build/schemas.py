"""Contracts for Phase 2: building a model from a sketch, photo or drawing.

Flow:
    inputs -> DimensionSpec (extracted, then edited and confirmed by a person)
           -> BuildPlan (proposed by the planner, LLM or human)
           -> solid (deterministic builder)
           -> VerificationReport (deterministic; measures the solid against the
              confirmed spec, independent of whoever wrote the plan)

Coordinates: millimetres, datum at the part's minimum corner. X to the right
and Y up in the top view, Z up from the bottom face.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

from app.schemas import OpenQuestion, Vec3

Axis = Literal["x", "y", "z"]
Direction = Literal["+x", "-x", "+y", "-y", "+z", "-z"]

# --------------------------------------------------------------------------
# Dimension spec: what the part must measure
# --------------------------------------------------------------------------

DimKind = Literal[
    "overall",
    "hole_diameter",
    "hole_depth",
    "hole_position",
    "hole_count",
    "depth",
    "material_width",
    "gap_width",
    "radius",
]

KIND_HELP: dict[str, str] = {
    "overall": "Overall extent of the part along `axis`.",
    "hole_diameter": "Diameter of the hole whose axis passes nearest `at`.",
    "hole_depth": "Depth of the hole nearest `at` (flat-bottom wall length); through=true means it passes through.",
    "hole_position": "Coordinate along `axis` of the centre of the hole nearest `at`, from the datum.",
    "hole_count": "Number of holes of `feature_diameter`.",
    "depth": "Distance from the part's bounding face (where `direction` starts) to the first material met at `at`; through=true means no material on that line.",
    "material_width": "Length of solid material along `axis` through `at` (inside material): wall, rib, plate or edge-distance.",
    "gap_width": "Length of empty space along `axis` through `at` (inside the gap), bounded by material: slot, pocket length/width.",
    "radius": "Radius of the cylindrical surface nearest `at` whose axis is parallel to `axis` (fillet, corner, boss).",
}

NEEDS_AT = {"hole_diameter", "hole_depth", "hole_position", "depth", "material_width", "gap_width", "radius"}
NEEDS_AXIS = {"overall", "hole_position", "material_width", "gap_width", "radius"}


class RequiredDimension(BaseModel):
    id: str = Field(description="Short unique id, e.g. D1.")
    label: str = Field(description="The dimension as written on the input, e.g. 'Ø6.5 THRU 4X' or '100'.")
    kind: DimKind
    nominal: float | None = Field(None, description="Nominal value in mm (a count for hole_count). Null only when through is true.")
    tol_plus: float | None = Field(None, description="Manufacturing tolerance above nominal in mm, if stated or given by a general tolerance; else null.")
    tol_minus: float | None = Field(None, description="Manufacturing tolerance below nominal in mm (positive number); else null.")
    at: Vec3 | None = Field(None, description="Locator/probe point in part coordinates (mm). Needed for every kind except overall and hole_count.")
    axis: Axis | None = Field(None, description="overall: extent axis. hole_position: coordinate measured. material_width/gap_width: probe direction. radius: arc axis.")
    direction: Direction | None = Field(None, description="depth only: direction into the part from its bounding face, e.g. -z = from the top face down.")
    feature_diameter: float | None = Field(None, description="hole_count: diameter counted. hole_depth/hole_position: diameter that picks the hole when several share a centre (counterbores).")
    through: bool = Field(False, description="hole_depth/depth: the feature goes all the way through.")
    source: Literal["drawing", "sketch", "photo", "user", "dfm_fix"] = "drawing"
    confidence: float = Field(1.0, description="0 to 1: how sure the reading is.")
    note: str | None = None

    def problems(self) -> list[str]:
        """Structural issues that make the dimension unmeasurable."""
        p: list[str] = []
        if self.kind in NEEDS_AT and self.at is None:
            p.append(f"{self.id}: '{self.kind}' needs a locator point (at).")
        if self.kind in NEEDS_AXIS and self.axis is None:
            p.append(f"{self.id}: '{self.kind}' needs an axis.")
        if self.kind == "depth" and self.direction is None:
            p.append(f"{self.id}: 'depth' needs a direction (e.g. -z from the top).")
        if self.kind == "hole_count" and self.feature_diameter is None:
            p.append(f"{self.id}: 'hole_count' needs feature_diameter.")
        if self.through and self.kind not in ("hole_depth", "depth"):
            p.append(f"{self.id}: through only applies to hole_depth and depth.")
        if not self.through:
            if self.nominal is None:
                p.append(f"{self.id}: nominal value missing.")
            elif self.nominal <= 0 and self.kind != "hole_count":
                p.append(f"{self.id}: nominal must be positive.")
            elif self.kind == "hole_count" and (self.nominal < 1 or self.nominal != int(self.nominal)):
                p.append(f"{self.id}: hole_count must be a whole number ≥ 1.")
        return p


class DimensionSpec(BaseModel):
    part_name: str | None = None
    units_on_input: Literal["mm", "inch", "unknown"] = Field("mm", description="Units written on the input. All values in this spec are converted to mm.")
    description: str = Field("", description="One or two sentences on the part's shape and features, for the planner.")
    scale_basis: Literal["dimensioned", "reference_object", "none"] = Field(
        "dimensioned", description="How sizes are known: written dimensions, a visible object of known size, or not at all.")
    general_tolerance_mm: float | None = Field(None, description="General tolerance band half-width from the title block, if any.")
    material_text: str | None = Field(None, description="Material as written on the input, if any.")
    finish_text: str | None = Field(None, description="Finish/coating notes as written, if any.")
    dimensions: list[RequiredDimension] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)

    def dim(self, dim_id: str) -> RequiredDimension | None:
        return next((d for d in self.dimensions if d.id == dim_id), None)

    def tightest_band_mm(self) -> float | None:
        bands = [d.tol_plus + d.tol_minus for d in self.dimensions
                 if d.tol_plus is not None and d.tol_minus is not None and d.kind != "hole_count"]
        return min(bands) if bands else None


def readiness(spec: DimensionSpec) -> list[str]:
    """Deterministic gate before generation. The planner can't skip this."""
    issues: list[str] = []
    if spec.scale_basis == "none":
        issues.append("No scale: sizes can't be taken from this input. Add written dimensions or a reference object, or enter the dimensions by hand.")
    for axis in ("x", "y", "z"):
        n = sum(1 for d in spec.dimensions if d.kind == "overall" and d.axis == axis)
        if n == 0:
            issues.append(f"Overall size along {axis.upper()} is missing.")
        elif n > 1:
            issues.append(f"More than one overall size along {axis.upper()}.")
    ids = [d.id for d in spec.dimensions]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        issues.append(f"Duplicate dimension id {dup}.")
    for d in spec.dimensions:
        issues += d.problems()
    for q in spec.open_questions:
        if q.blocking:
            issues.append(f"Open question: {q.question}")
    return issues


# --------------------------------------------------------------------------
# Build plan: a small feature DSL (no free-form code is ever executed)
# --------------------------------------------------------------------------


class Point2(BaseModel):
    u: float
    v: float


class StockBox(BaseModel):
    kind: Literal["box"]
    size: Vec3 = Field(description="Length (x), width (y), height (z) in mm; minimum corner at the origin.")
    corner_radius: float | None = Field(None, description="Radius on the four edges parallel to Z (rounded corners in the top view), or null.")


class StockCylinder(BaseModel):
    kind: Literal["cylinder"]
    diameter: float
    height: float = Field(description="Along Z. The axis sits at (d/2, d/2) so the bounding box starts at the origin.")


class StockExtrusion(BaseModel):
    kind: Literal["extrusion"]
    plane: Literal["xy", "xz", "yz"] = Field(description="Sketch plane. xy: points are (x, y), extruded along +Z. xz: (x, z), extruded along +Y. yz: (y, z), extruded along +X.")
    points: list[Point2] = Field(description="Closed outline, in order, without repeating the first point. Keep all coordinates ≥ 0.")
    depth: float = Field(description="Extrusion length in mm.")
    concave_corner_radius: float | None = Field(None, description="Radius applied to inside corners of the outline, or null.")
    convex_corner_radius: float | None = Field(None, description="Radius applied to outside corners of the outline, or null.")


Stock = Annotated[Union[StockBox, StockCylinder, StockExtrusion], Field(discriminator="kind")]


class BoltCircle(BaseModel):
    center: Vec3 = Field(description="Centre of the circle on the entry face.")
    diameter: float = Field(description="Pitch circle diameter.")
    count: int
    start_angle_deg: float = Field(0.0, description="Angle of the first hole from the first in-plane axis (x for Z holes, y for X holes, x for Y holes).")


class HoleOp(BaseModel):
    op: Literal["hole"]
    id: str
    dims: list[str] = Field(default_factory=list, description="Ids of the confirmed dimensions this feature implements.")
    diameter: float
    direction: Direction = Field(description="Drilling direction into the material, e.g. -z = drilled from the top face downward.")
    positions: list[Vec3] = Field(default_factory=list, description="Entry points on the surface (mm). For -z holes, z is the height of the face drilled from.")
    bolt_circle: BoltCircle | None = Field(None, description="Alternative to positions: holes evenly spaced on a circle.")
    depth: float | None = Field(None, description="Depth from the entry point in mm; null = through all.")
    counterbore_diameter: float | None = None
    counterbore_depth: float | None = None


class PocketOp(BaseModel):
    op: Literal["pocket"]
    id: str
    dims: list[str] = Field(default_factory=list)
    center: Vec3 = Field(description="Centre of the pocket opening on the entry face.")
    direction: Direction = Field(description="Cutting direction into the material, e.g. -z from the top face.")
    length: float = Field(description="Size along the first in-plane axis: x for ±z pockets, y for ±x, x for ±y.")
    width: float = Field(description="Size along the second in-plane axis: y for ±z pockets, z for ±x and ±y.")
    depth: float | None = Field(None, description="Depth from the entry face in mm; null = through all.")
    corner_radius: float | None = Field(None, description="Radius of the corners parallel to the cutting direction. Half the width makes an obround slot.")
    angle_deg: float | None = Field(None, description="Rotation about the cutting axis, or null.")


class BossOp(BaseModel):
    op: Literal["boss"]
    id: str
    dims: list[str] = Field(default_factory=list)
    shape: Literal["cylinder", "box"]
    base_center: Vec3 = Field(description="Centre of the boss footprint on the face it grows from.")
    direction: Direction = Field(description="Direction the boss grows out of the face, e.g. +z grows up from the top face.")
    height: float
    diameter: float | None = Field(None, description="Cylinder bosses.")
    length: float | None = Field(None, description="Box bosses: size along the first in-plane axis (as for pockets).")
    width: float | None = Field(None, description="Box bosses: size along the second in-plane axis.")


class CutBoxOp(BaseModel):
    op: Literal["cut_box"]
    id: str
    dims: list[str] = Field(default_factory=list)
    corner_min: Vec3 = Field(description="Minimum corner of the axis-aligned region removed (steps, notches, rebates). Extend past the part where the cut is open.")
    corner_max: Vec3
    corner_radius: float | None = Field(None, description="Radius on the region's edges parallel to corner_axis (rounds the inside corners left by the cut).")
    corner_axis: Axis | None = None


Operation = Annotated[Union[HoleOp, PocketOp, BossOp, CutBoxOp], Field(discriminator="op")]


class BuildPlan(BaseModel):
    stock: Stock
    operations: list[Operation] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list, description="Every value or choice not taken from the confirmed dimensions, with the reason.")

    @model_validator(mode="after")
    def _unique_ids(self):
        ids = [o.id for o in self.operations]
        dups = sorted({i for i in ids if ids.count(i) > 1})
        if dups:
            raise ValueError(f"duplicate operation ids: {', '.join(dups)}")
        return self


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


class ValidityCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class CheckResult(BaseModel):
    dim_id: str
    label: str
    kind: DimKind
    expected: str
    measured: str | None
    measured_value: float | None
    deviation: float | None
    passed: bool
    method: str


class CoverageNote(BaseModel):
    feature_id: str
    message: str


class VerificationReport(BaseModel):
    passed: bool
    model_tolerance_mm: float
    validity: list[ValidityCheck]
    dimensions: list[CheckResult]
    features: list[ValidityCheck]
    coverage: list[CoverageNote]

    def failures(self) -> list[str]:
        """Plain-text failure list; this is the feedback a planner retry receives."""
        out = [f"Validity: {v.name}: {v.detail}" for v in self.validity if not v.passed]
        out += [f"{c.dim_id} {c.kind} ({c.label}): expected {c.expected}, measured {c.measured or 'nothing'}. {c.method}"
                for c in self.dimensions if not c.passed]
        out += [f"Feature check: {f.name}: {f.detail}" for f in self.features if not f.passed]
        return out


class GeneratedFile(BaseModel):
    name: str
    path: str
    sha256: str
    size_bytes: int


class AttemptRecord(BaseModel):
    attempt: int
    source: Literal["planner", "manual", "dfm_fix", "oracle", "mutant"]
    plan: BuildPlan | None = None
    plan_error: str | None = None
    build_error: str | None = None
    report: VerificationReport | None = None
    files: dict[str, GeneratedFile] = Field(default_factory=dict)
    feedback_used: list[str] = Field(default_factory=list)
    llm_usage: dict | None = None

    @property
    def passed(self) -> bool:
        return self.report is not None and self.report.passed

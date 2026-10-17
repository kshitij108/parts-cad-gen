"""Intake stage: identify uploads and list the questions that block a quote.

Read-only by construction: it receives file paths and reads a few header bytes.
It has no database or storage handle. Questions are addressed to the shop user,
who may forward them to the customer.
"""

from __future__ import annotations

from pathlib import Path

from app.schemas import FileKind, IdentifiedFile, IntakeInput, IntakeResult, OpenQuestion, UploadedFile

_EXT_KINDS: dict[str, FileKind] = {
    ".step": "step",
    ".stp": "step",
    ".pdf": "pdf",
    ".iges": "iges",
    ".igs": "iges",
    ".x_t": "parasolid",
    ".x_b": "parasolid",
    ".sldprt": "solidworks",
    ".dxf": "dxf",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
}

_UNSUPPORTED_NOTES: dict[str, str] = {
    "iges": "IGES not supported yet. Ask the customer for STEP (AP214/AP242).",
    "parasolid": "Parasolid not supported yet. Ask the customer for STEP.",
    "solidworks": "Native SolidWorks files are not read. Ask the customer to export STEP.",
    "dxf": "DXF is a 2D format (sheet metal is out of Phase 1 scope).",
    "image": "Photos/sketches need the Phase 2 build stage.",
    "unknown": "Unrecognised file type.",
}


def detect_kind(path: str, original_name: str) -> FileKind:
    """Sniff content first; fall back to extension."""
    try:
        head = Path(path).read_bytes()[:64]
    except OSError:
        head = b""
    if head.startswith(b"ISO-10303-21"):
        return "step"
    if head.startswith(b"%PDF"):
        return "pdf"
    return _EXT_KINDS.get(Path(original_name).suffix.lower(), "unknown")


def _identify(f: UploadedFile) -> IdentifiedFile:
    kind = detect_kind(f.path, f.original_name)
    role = {"step": "model", "pdf": "drawing"}.get(kind, "other")
    supported = kind in ("step", "pdf")
    return IdentifiedFile(
        file_id=f.id,
        original_name=f.original_name,
        kind=kind,
        role=role,
        supported=supported,
        note=None if supported else _UNSUPPORTED_NOTES[kind],
    )


def run(inp: IntakeInput) -> IntakeResult:
    files = [_identify(f) for f in inp.files]
    models = [f for f in files if f.kind == "step"]
    drawings = [f for f in files if f.kind == "pdf"]
    spec = inp.spec
    qs: list[OpenQuestion] = []

    if not models:
        qs.append(OpenQuestion(id="intake.no_model", field="files", ask="customer",
                               question="No STEP model uploaded. Request a STEP file (Phase 1 quotes from STEP only)."))
    if len(models) > 1:
        qs.append(OpenQuestion(id="intake.multiple_models", field="files", blocking=False,
                               question=f"{len(models)} STEP files uploaded; quoting the first. Split into separate jobs if they are different parts."))
    if not drawings:
        qs.append(OpenQuestion(id="intake.no_drawing", field="files", ask="customer", blocking=False,
                               question="No PDF drawing. Tolerances and finish will rely on what the shop enters; confirm with the customer."))

    if spec.material_id is None:
        qs.append(OpenQuestion(id="intake.material", field="material_id",
                               question="Which material? (Pick from the rate card, or add it there first.)"))
    elif spec.material_id not in inp.known_material_ids:
        qs.append(OpenQuestion(id="intake.material_unknown", field="material_id",
                               question=f"Material '{spec.material_id}' is not on the rate card. Add it or pick another."))

    if not spec.quantities:
        qs.append(OpenQuestion(id="intake.quantity", field="quantities",
                               question="What quantities should be quoted? (e.g. 1, 10, 50)"))

    for fid in spec.finish_ids:
        if fid not in inp.known_finish_ids:
            qs.append(OpenQuestion(id=f"intake.finish_unknown.{fid}", field="finish_ids",
                                   question=f"Finish '{fid}' is not on the rate card. Add it or remove it."))

    if spec.deadline is None:
        qs.append(OpenQuestion(id="intake.deadline", field="deadline", ask="customer", blocking=False,
                               question="No required date given. The quote will show lead time only."))

    return IntakeResult(
        files=files,
        spec=spec,
        model_file_id=models[0].file_id if models else None,
        drawing_file_id=drawings[0].file_id if drawings else None,
        open_questions=qs,
    )

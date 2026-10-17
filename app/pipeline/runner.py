"""Runs intake -> geometry -> drawing -> dfm -> quote for a job.

Each stage receives only its typed input (no session, no storage handle), so
role limits are structural: intake can't write files, quote can't touch
geometry. The runner is the only code that persists. Every output is
re-validated against its schema before being stored or passed on; a failure
stops downstream stages and is recorded.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import Job, StageRun
from app.pipeline import dfm, drawing, geometry, intake, quote
from app.schemas import (
    DfmInput,
    DfmReport,
    DrawingExtraction,
    GeometrySummary,
    IntakeInput,
    IntakeResult,
    JobSpec,
    OpenQuestion,
    QuoteDraft,
    QuoteInput,
    RateCard,
    UploadedFile,
)

T = TypeVar("T", bound=BaseModel)
STAGES = ("intake", "geometry", "drawing", "dfm", "quote")


class StageFailed(Exception):
    pass


def _record(session: Session, job: Job, run_no: int, stage: str, model: type[T], fn: Callable[[], T]) -> T:
    try:
        out = fn()
        out = model.model_validate(out.model_dump())  # contract check before anything downstream sees it
    except Exception as e:
        session.add(StageRun(job_id=job.id, run_no=run_no, stage=stage, status="error",
                             error=f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))
        raise StageFailed(stage) from e
    session.add(StageRun(job_id=job.id, run_no=run_no, stage=stage, status="ok", output=out.model_dump(mode="json")))
    return out


def _skip(session: Session, job: Job, run_no: int, stage: str, why: str) -> None:
    session.add(StageRun(job_id=job.id, run_no=run_no, stage=stage, status="skipped", error=why))


def run_pipeline(session: Session, job: Job, rate_card: RateCard) -> None:
    run_no = job.current_run + 1
    job.current_run = run_no
    spec = JobSpec.model_validate(job.spec)
    files = [UploadedFile(id=f.id, original_name=f.original_name, path=f.path, sha256=f.sha256, size_bytes=f.size_bytes)
             for f in job.files]
    by_id = {f.id: f for f in files}
    questions: list[OpenQuestion] = []

    try:
        ir = _record(session, job, run_no, "intake", IntakeResult, lambda: intake.run(IntakeInput(
            files=files, spec=spec,
            known_material_ids=[m.id for m in rate_card.materials],
            known_finish_ids=[f.id for f in rate_card.finishes],
        )))
        questions += ir.open_questions

        if ir.model_file_id is None:
            for s in ("geometry", "drawing", "dfm", "quote"):
                _skip(session, job, run_no, s, "No STEP model.")
        else:
            geo = _record(session, job, run_no, "geometry", GeometrySummary,
                          lambda: geometry.run(by_id[ir.model_file_id].path))
            pdf = Path(by_id[ir.drawing_file_id].path) if ir.drawing_file_id else None
            dwg = _record(session, job, run_no, "drawing", DrawingExtraction, lambda: drawing.run(pdf))
            questions += dwg.open_questions
            # Drawing vs job spec: the drawing governs material/finish callouts.
            if dwg.material_text and spec.material_id and dwg.material_text.lower() not in spec.material_id.lower():
                questions.append(OpenQuestion(id="reconcile.material", field="material_id",
                                              question=f"Drawing says '{dwg.material_text}' but the job material is '{spec.material_id}'. Which is correct?"))
            rep = _record(session, job, run_no, "dfm", DfmReport,
                          lambda: dfm.run(DfmInput(geometry=geo, drawing=dwg, spec=ir.spec, rate_card=rate_card)))
            qd = _record(session, job, run_no, "quote", QuoteDraft,
                         lambda: quote.run(QuoteInput(geometry=geo, drawing=dwg, spec=ir.spec, dfm=rep, rate_card=rate_card)))
            questions += qd.open_questions
    except StageFailed:
        job.status = "error"
        session.flush()
        return

    blocking = any(q.blocking for q in dedupe_questions(questions))
    job.status = "needs_info" if blocking else "in_review"
    session.flush()


def latest_outputs(job: Job) -> dict[str, StageRun]:
    return {r.stage: r for r in job.runs if r.run_no == job.current_run}


def dedupe_questions(questions: list[OpenQuestion]) -> list[OpenQuestion]:
    """Drop repeats by id, and a blocking question whose field already has one
    (intake and quote both ask for a missing material)."""
    out: list[OpenQuestion] = []
    ids: set[str] = set()
    blocking_fields: set[str] = set()
    for q in questions:
        if q.id in ids or (q.blocking and q.field in blocking_fields):
            continue
        ids.add(q.id)
        if q.blocking:
            blocking_fields.add(q.field)
        out.append(q)
    return out


def open_questions(job: Job) -> list[OpenQuestion]:
    runs = latest_outputs(job)
    qs = [OpenQuestion.model_validate(q)
          for stage in STAGES if (r := runs.get(stage)) and r.status == "ok" and r.output
          for q in r.output.get("open_questions", [])]
    return dedupe_questions(qs)

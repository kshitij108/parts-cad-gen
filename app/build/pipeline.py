"""Generate -> build -> verify loop for one confirmed DimensionSpec.

Each attempt is: planner proposes a plan, the builder builds it, verify.py
measures it against the confirmed spec. A failed attempt's measured failures
are fed back to the planner for the next attempt. API failures stop the loop
(retrying them burns money without new information).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.build.agent import LLMError, PlanInvalid, Planner
from app.build.builder import BuildError, build
from app.build.export import export_attempt
from app.build.schemas import AttemptRecord, BuildPlan, DimensionSpec, readiness
from app.build.verify import verify


class NotReady(Exception):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


def run_attempt(n: int, plan: BuildPlan, spec: DimensionSpec, out_dir: Path, source: str,
                feedback: list[str] | None = None, usage: dict | None = None,
                title: str | None = None, material: str | None = None) -> AttemptRecord:
    rec = AttemptRecord(attempt=n, source=source, plan=plan, feedback_used=feedback or [], llm_usage=usage)
    try:
        shape = build(plan)
    except BuildError as e:
        rec.build_error = str(e)
        return rec
    except Exception as e:  # OpenCascade raises its own exception types
        rec.build_error = f"{type(e).__name__}: {e}"
        return rec
    rec.report = verify(shape, spec, plan)
    rec.files = export_attempt(shape, out_dir / f"attempt-{n}", spec, rec.report, title=title, material=material)
    return rec


def feedback_for(rec: AttemptRecord) -> list[str]:
    fb: list[str] = []
    if rec.plan_error:
        fb.append(rec.plan_error)
    if rec.build_error:
        fb.append("Build error: " + rec.build_error)
    if rec.report:
        fb += rec.report.failures()
    return fb


def generate(spec: DimensionSpec, inputs: list[Path], planner: Planner, out_dir: Path, *,
             max_attempts: int = 3, first_attempt: int = 1, title: str | None = None, material: str | None = None,
             on_attempt: Callable[[AttemptRecord], None] | None = None) -> list[AttemptRecord]:
    issues = readiness(spec)
    if issues:
        raise NotReady(issues)
    records: list[AttemptRecord] = []
    previous: AttemptRecord | None = None
    for i in range(max_attempts):
        n = first_attempt + i
        try:
            plan, usage = planner.plan(spec, inputs, previous)
        except PlanInvalid as e:
            rec = AttemptRecord(attempt=n, source=planner.source, plan_error=str(e),
                                feedback_used=feedback_for(previous) if previous else [])
        except LLMError as e:
            rec = AttemptRecord(attempt=n, source=planner.source, plan_error=f"Planner unavailable: {e}")
            records.append(rec)
            if on_attempt:
                on_attempt(rec)
            break
        else:
            rec = run_attempt(n, plan, spec, out_dir, planner.source,
                              feedback=feedback_for(previous) if previous else [], usage=usage,
                              title=title, material=material)
        records.append(rec)
        if on_attempt:
            on_attempt(rec)
        if rec.passed:
            break
        previous = rec
    return records

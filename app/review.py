"""Human review: per-line decisions, job approval, and the frozen package.

Nothing is marked approved without an explicit decision on every quote line and
no blocking open questions. Decisions are tied to a pipeline run; re-running
the pipeline (e.g. after answering questions) requires a fresh review.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db import Job, Package, ReviewEvent
from app.pipeline.runner import latest_outputs, open_questions
from app.schemas import QuoteDraft

LINE_ACTIONS = ("approve", "edit", "reject")


class ReviewError(ValueError):
    pass


@dataclass
class Decision:
    action: str
    amount: float | None
    note: str | None
    actor: str


def current_quote(job: Job) -> QuoteDraft | None:
    r = latest_outputs(job).get("quote")
    return QuoteDraft.model_validate(r.output) if r and r.status == "ok" and r.output else None


def line_decisions(job: Job) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for e in job.events:  # ordered by id; later events win
        if e.run_no == job.current_run and e.action in LINE_ACTIONS:
            out[e.target] = Decision(e.action, e.amount, e.note, e.actor)
    return out


def decide_line(session: Session, job: Job, line_id: str, action: str, amount: float | None, note: str | None, actor: str) -> None:
    if job.status not in ("in_review", "needs_info"):
        raise ReviewError(f"Job is {job.status}; lines can't be changed.")
    if action not in LINE_ACTIONS:
        raise ReviewError(f"Unknown action {action!r}.")
    if not actor.strip():
        raise ReviewError("Reviewer name is required.")
    q = current_quote(job)
    line = next((l for qq in (q.quantities if q else []) for l in qq.lines if l.id == line_id), None)
    if line is None:
        raise ReviewError(f"No line {line_id!r} in the current quote.")
    if action in ("approve", "edit") and (amount is None or amount < 0):
        raise ReviewError("An amount ≥ 0 is required to approve or edit a line.")
    if action == "approve" and not (line.low - 0.005 <= amount <= line.high + 0.005):
        raise ReviewError("Approved amount must be within the line's range; use Edit to override it.")
    if action in ("edit", "reject") and not (note and note.strip()):
        raise ReviewError("A note explaining the change is required to edit or reject a line.")
    session.add(ReviewEvent(job_id=job.id, run_no=job.current_run, target=line_id, action=action,
                            amount=None if action == "reject" else amount, note=note, actor=actor.strip()))
    session.flush()


def totals(job: Job) -> dict[int, float | None]:
    """Final lot total per quantity from decisions; None while any line is undecided."""
    q, d = current_quote(job), line_decisions(job)
    out: dict[int, float | None] = {}
    for qq in (q.quantities if q else []):
        if any(l.id not in d for l in qq.lines):
            out[qq.quantity] = None
        else:
            out[qq.quantity] = round(sum(d[l.id].amount or 0 for l in qq.lines if d[l.id].action != "reject"), 2)
    return out


def approval_blockers(job: Job) -> list[str]:
    reasons: list[str] = []
    if job.status not in ("in_review",):
        reasons.append(f"Job status is '{job.status}'.")
    blocking = [q for q in open_questions(job) if q.blocking]
    if blocking:
        reasons.append(f"{len(blocking)} blocking question(s) are open.")
    q = current_quote(job)
    if q is None or q.status != "draft":
        reasons.append("There is no draft quote.")
    else:
        d = line_decisions(job)
        undecided = sum(1 for qq in q.quantities for l in qq.lines if l.id not in d)
        if undecided:
            reasons.append(f"{undecided} quote line(s) have no decision.")
    return reasons


def approve_job(session: Session, job: Job, actor: str, note: str | None) -> Package:
    if not actor.strip():
        raise ReviewError("Approver name is required.")
    blockers = approval_blockers(job)
    if blockers:
        raise ReviewError(" ".join(blockers))
    session.add(ReviewEvent(job_id=job.id, run_no=job.current_run, target="job", action="approve_job", note=note, actor=actor.strip()))
    session.flush()
    runs = latest_outputs(job)
    snapshot = {
        "job": {"id": job.id, "title": job.title, "spec": job.spec, "run_no": job.current_run},
        "originals": [{"id": f.id, "name": f.original_name, "sha256": f.sha256, "size_bytes": f.size_bytes} for f in job.files],
        "stages": {s: {"status": r.status, "output": r.output, "error": r.error} for s, r in runs.items()},
        "decisions": {k: vars(v) for k, v in line_decisions(job).items()},
        "totals": totals(job),
        "trail": [{"at": e.created_at.isoformat(), "actor": e.actor, "action": e.action, "target": e.target,
                   "amount": e.amount, "note": e.note, "run_no": e.run_no} for e in job.events],
    }
    pkg = Package(job_id=job.id, run_no=job.current_run, snapshot=snapshot, approved_by=actor.strip())
    session.add(pkg)
    job.status = "approved"
    session.flush()
    return pkg


def reject_job(session: Session, job: Job, actor: str, note: str) -> None:
    if not actor.strip() or not note.strip():
        raise ReviewError("Reviewer name and a reason are required to reject.")
    if job.status == "approved":
        raise ReviewError("Job is already approved.")
    session.add(ReviewEvent(job_id=job.id, run_no=job.current_run, target="job", action="reject_job", note=note, actor=actor.strip()))
    job.status = "rejected"
    session.flush()

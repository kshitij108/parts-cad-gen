"""Build jobs on top of the shared Job / StageRun / ReviewEvent / Package tables.

Stage rows for a build job (newest row of each stage wins):
    extract         DimensionSpec drafted by the extractor (or an error)
    spec            the working spec, after every human edit
    spec_confirmed  the spec a person confirmed; generation verifies against it
    attempt         one AttemptRecord per build-and-verify attempt

Human decisions (spec edits, confirmation, acceptance, DFM fixes) are
ReviewEvents. Acceptance freezes everything into a Package.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app import storage
from app.build.agent import Extractor, LLMError, Planner
from app.build.fixes import FIXABLE_RULES, apply_fix
from app.build.pipeline import NotReady, generate, run_attempt
from app.build.schemas import AttemptRecord, BuildPlan, DimensionSpec, readiness
from app.db import Job, JobFile, Package, ReviewEvent, StageRun
from app.pipeline.dfm import tool_axis
from app.pipeline.runner import latest_outputs, run_pipeline
from app.schemas import DfmFlag, DfmReport, GeometrySummary, JobSpec, RateCard

INPUT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}
BUSY = {"extracting", "generating"}


class BuildJobError(ValueError):
    pass


# --------------------------------------------------------------------------
# Reading state
# --------------------------------------------------------------------------


def _latest(job: Job, stage: str) -> StageRun | None:
    rows = [r for r in job.runs if r.stage == stage]
    return max(rows, key=lambda r: r.id) if rows else None


def current_spec(job: Job) -> DimensionSpec | None:
    r = _latest(job, "spec")
    return DimensionSpec.model_validate(r.output) if r and r.output else None


def confirmed_spec(job: Job) -> DimensionSpec | None:
    """The confirmed spec, unless it has been edited since confirmation."""
    c, e = _latest(job, "spec_confirmed"), _latest(job, "spec")
    if c is None or (e is not None and e.id > c.id):
        return None
    return DimensionSpec.model_validate(c.output)


def extraction(job: Job) -> StageRun | None:
    return _latest(job, "extract")


def attempts(job: Job) -> list[AttemptRecord]:
    rows = sorted((r for r in job.runs if r.stage == "attempt" and r.output), key=lambda r: r.id)
    return [AttemptRecord.model_validate(r.output) for r in rows]


def latest_attempt(job: Job) -> AttemptRecord | None:
    a = attempts(job)
    return a[-1] if a else None


def accepted_attempt(job: Job) -> AttemptRecord | None:
    if not job.packages:
        return None
    pkg = max(job.packages, key=lambda p: p.created_at)
    return AttemptRecord.model_validate(pkg.snapshot["attempt"])


def input_paths(job: Job) -> list[Path]:
    return [Path(f.path) for f in job.files if Path(f.original_name).suffix.lower() in INPUT_SUFFIXES]


def generated_dir(job: Job) -> Path:
    return storage.job_dir(job.id) / "generated"


def _event(session: Session, job: Job, action: str, actor: str, target: str = "build", note: str | None = None,
           amount: float | None = None) -> None:
    job.events.append(ReviewEvent(run_no=job.current_run, target=target, action=action,
                                  actor=actor.strip() or "system", note=note, amount=amount))


def _require_actor(actor: str) -> str:
    if not actor or not actor.strip():
        raise BuildJobError("Your name is required.")
    return actor.strip()


def _require_idle(job: Job) -> None:
    if job.status in BUSY:
        raise BuildJobError(f"Job is {job.status}; wait for it to finish.")


# --------------------------------------------------------------------------
# Spec: extract, edit, confirm
# --------------------------------------------------------------------------


def start_spec(session: Session, job: Job) -> None:
    """Blank working spec for manual entry."""
    job.runs.append(StageRun(run_no=0, stage="spec", status="ok",
                         output=DimensionSpec(scale_basis="dimensioned", description="").model_dump(mode="json")))
    job.status = "needs_spec"


def run_extraction(session: Session, job: Job, extractor: Extractor) -> None:
    notes = JobSpec.model_validate(job.spec).customer_notes
    try:
        spec, usage = extractor.extract(input_paths(job), notes)
    except LLMError as e:
        job.runs.append(StageRun(run_no=0, stage="extract", status="error", error=str(e)))
        job.status = "needs_spec"
        return
    out = spec.model_dump(mode="json")
    job.runs.append(StageRun(run_no=0, stage="extract", status="ok",
                         output={"spec": out, "usage": usage}))
    job.runs.append(StageRun(run_no=0, stage="spec", status="ok", output=out))
    job.status = "spec_review"


def save_spec(session: Session, job: Job, spec: DimensionSpec, actor: str) -> None:
    actor = _require_actor(actor)
    _require_idle(job)
    if job.status == "accepted":
        raise BuildJobError("The model is accepted; the spec is frozen.")
    job.runs.append(StageRun(run_no=0, stage="spec", status="ok", output=spec.model_dump(mode="json")))
    _event(session, job, "edit_spec", actor, "spec", note=f"{len(spec.dimensions)} dimensions")
    job.status = "spec_review"


def confirm_spec(session: Session, job: Job, actor: str) -> None:
    actor = _require_actor(actor)
    _require_idle(job)
    spec = current_spec(job)
    if spec is None:
        raise BuildJobError("There is no spec to confirm.")
    issues = readiness(spec)
    if issues:
        raise BuildJobError("Can't confirm yet: " + " ".join(issues))
    job.runs.append(StageRun(run_no=0, stage="spec_confirmed", status="ok", output=spec.model_dump(mode="json")))
    _event(session, job, "confirm_spec", actor, "spec", note=f"{len(spec.dimensions)} dimensions confirmed")
    job.status = "ready"


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def _record_attempt(session: Session, job: Job, rec: AttemptRecord) -> None:
    job.runs.append(StageRun(run_no=rec.attempt, stage="attempt",
                         status="ok" if rec.passed else "error", output=rec.model_dump(mode="json"),
                         error=None if rec.passed else (rec.plan_error or rec.build_error or "verification failed")))


def _material(job: Job, spec: DimensionSpec | None = None) -> str | None:
    """Material for the drawing title block: as written on the input, else the job's rate-card id."""
    return (spec.material_text if spec else None) or JobSpec.model_validate(job.spec).material_id


def begin_generation(session: Session, job: Job, actor: str) -> None:
    actor = _require_actor(actor)
    _require_idle(job)
    if confirmed_spec(job) is None:
        raise BuildJobError("Confirm the dimension spec first.")
    if job.status == "accepted":
        raise BuildJobError("The model is already accepted.")
    _event(session, job, "generate", actor)
    job.status = "generating"


def run_generation(session: Session, job: Job, planner: Planner, max_attempts: int = 3, commit=None) -> list[AttemptRecord]:
    spec = confirmed_spec(job)
    first = len(attempts(job)) + 1

    def on_attempt(rec: AttemptRecord) -> None:
        _record_attempt(session, job, rec)
        if commit:
            commit()

    try:
        recs = generate(spec, input_paths(job), planner, generated_dir(job), max_attempts=max_attempts,
                        first_attempt=first, title=job.title, material=_material(job, spec), on_attempt=on_attempt)
    except NotReady as e:
        rec = AttemptRecord(attempt=first, source=planner.source, plan_error="; ".join(e.issues))
        _record_attempt(session, job, rec)
        recs = [rec]
    last = recs[-1] if recs else None
    job.status = "model_review" if (last and last.passed) else "failed"
    return recs


def run_manual_plan(session: Session, job: Job, plan: BuildPlan, actor: str) -> AttemptRecord:
    actor = _require_actor(actor)
    _require_idle(job)
    if job.status == "accepted":
        raise BuildJobError("The model is already accepted.")
    spec = confirmed_spec(job)
    if spec is None:
        raise BuildJobError("Confirm the dimension spec first; every model is verified against it.")
    n = len(attempts(job)) + 1
    rec = run_attempt(n, plan, spec, generated_dir(job), "manual", title=job.title, material=_material(job, spec))
    _record_attempt(session, job, rec)
    _event(session, job, "manual_plan", actor, f"attempt-{n}")
    job.status = "model_review" if rec.passed else "failed"
    return rec


# --------------------------------------------------------------------------
# Sign-off and hand-off
# --------------------------------------------------------------------------


def accept_model(session: Session, job: Job, actor: str, note: str | None) -> Package:
    actor = _require_actor(actor)
    _require_idle(job)
    rec, spec = latest_attempt(job), confirmed_spec(job)
    if rec is None or not rec.passed:
        raise BuildJobError("Only a model that passed verification can be accepted.")
    if spec is None:
        raise BuildJobError("The spec changed after this model was verified; regenerate first.")
    _event(session, job, "accept_model", actor, f"attempt-{rec.attempt}", note=note)
    session.flush()
    snapshot = {
        "job": {"id": job.id, "title": job.title, "kind": "build", "spec": job.spec},
        "originals": [{"id": f.id, "name": f.original_name, "sha256": f.sha256, "size_bytes": f.size_bytes} for f in job.files],
        "confirmed_spec": spec.model_dump(mode="json"),
        "attempt": rec.model_dump(mode="json"),
        "trail": [{"at": e.created_at.isoformat(), "actor": e.actor, "action": e.action, "target": e.target, "note": e.note}
                  for e in job.events],
    }
    pkg = Package(run_no=rec.attempt, snapshot=snapshot, approved_by=actor)
    job.packages.append(pkg)
    job.status = "accepted"
    session.flush()
    return pkg


def send_to_quote(session: Session, job: Job, shop_id: str, rate_card: RateCard, actor: str) -> Job:
    actor = _require_actor(actor)
    rec = accepted_attempt(job)
    if job.status != "accepted" or rec is None:
        raise BuildJobError("Accept the model before sending it to quoting.")
    spec = DimensionSpec.model_validate(max(job.packages, key=lambda p: p.created_at).snapshot["confirmed_spec"])
    js = JobSpec.model_validate(job.spec)
    if js.tightest_tolerance_mm is None:
        js.tightest_tolerance_mm = spec.tightest_band_mm()
    quote = Job(shop_id=shop_id, title=f"{job.title} (generated)", kind="quote", parent_id=job.id, spec=js.model_dump(mode="json"))
    session.add(quote)
    session.flush()
    for key in ("step", "pdf"):
        gf = rec.files.get(key)
        if gf is None:
            continue
        name = f"{job.title or 'part'}-{gf.name}".replace("/", "_")
        fid, path, sha = storage.save_original(quote.id, name, Path(gf.path).read_bytes())
        quote.files.append(JobFile(id=fid, original_name=name, path=str(path), sha256=sha, size_bytes=path.stat().st_size))
    session.flush()
    _event(session, job, "send_to_quote", actor, quote.id)
    run_pipeline(session, quote, rate_card)
    return quote


# --------------------------------------------------------------------------
# DFM fix from a quote job back into its build job
# --------------------------------------------------------------------------


def fixable_flags(quote: Job) -> list[DfmFlag]:
    if quote.kind != "quote" or not quote.parent_id:
        return []
    r = latest_outputs(quote).get("dfm")
    if not r or r.status != "ok" or not r.output:
        return []
    return [f for f in DfmReport.model_validate(r.output).flags if f.rule in FIXABLE_RULES]


def apply_dfm_fix(session: Session, quote: Job, build: Job, flag_id: str, rate_card: RateCard, actor: str) -> AttemptRecord:
    actor = _require_actor(actor)
    flag = next((f for f in fixable_flags(quote) if f.id == flag_id), None)
    if flag is None:
        raise BuildJobError("That DFM flag has no automatic fix.")
    base = accepted_attempt(build)
    if base is None or base.plan is None:
        raise BuildJobError("The build job has no accepted model to fix.")
    spec = DimensionSpec.model_validate(max(build.packages, key=lambda p: p.created_at).snapshot["confirmed_spec"])
    geo_run = latest_outputs(quote).get("geometry")
    axis = "z"
    if geo_run and geo_run.output:
        v, _ = tool_axis(GeometrySummary.model_validate(geo_run.output))
        axis = "xyz"[max(range(3), key=lambda i: abs((v.x, v.y, v.z)[i]))]
    plan, new_spec, changes = apply_fix(flag, base.plan, spec, rate_card, axis)
    if not changes:
        raise BuildJobError("Nothing in the plan matched this flag; fix it by editing the plan.")
    build.runs.append(StageRun(run_no=0, stage="spec", status="ok", output=new_spec.model_dump(mode="json")))
    build.runs.append(StageRun(run_no=0, stage="spec_confirmed", status="ok", output=new_spec.model_dump(mode="json")))
    _event(session, build, "dfm_fix", actor, flag.id, note=" ".join(changes) + f" (from quote job {quote.id[:8]})")
    session.flush()
    n = len(attempts(build)) + 1
    rec = run_attempt(n, plan, new_spec, generated_dir(build), "dfm_fix", feedback=[flag.title] + changes,
                      title=build.title, material=_material(build, new_spec))
    _record_attempt(session, build, rec)
    build.status = "model_review" if rec.passed else "failed"
    session.flush()
    return rec


def spec_from_form(form, base: DimensionSpec) -> DimensionSpec:
    """Rebuild a DimensionSpec from the editable table on the build page."""

    def f(key: str) -> float | None:
        v = (form.get(key) or "").strip()
        return float(v) if v else None

    dims = []
    i = 0
    while f"d-{i}-kind" in form:
        p = f"d-{i}-"
        i += 1
        if form.get(p + "delete") or not (form.get(p + "id") or "").strip():
            continue
        at = None
        if any((form.get(p + k) or "").strip() for k in ("x", "y", "z")):
            at = {"x": f(p + "x") or 0.0, "y": f(p + "y") or 0.0, "z": f(p + "z") or 0.0}
        dims.append({
            "id": form.get(p + "id").strip(), "label": (form.get(p + "label") or "").strip(),
            "kind": form.get(p + "kind"), "nominal": f(p + "nominal"),
            "tol_plus": f(p + "tol_plus"), "tol_minus": f(p + "tol_minus"), "at": at,
            "axis": form.get(p + "axis") or None, "direction": form.get(p + "direction") or None,
            "feature_diameter": f(p + "feature_diameter"), "through": bool(form.get(p + "through")),
            "source": form.get(p + "source") or "user", "confidence": f(p + "confidence") or 1.0,
            "note": (form.get(p + "note") or "").strip() or None,
        })
    resolved = set(form.getlist("resolve")) if hasattr(form, "getlist") else set()
    return DimensionSpec.model_validate({
        **base.model_dump(mode="json"),
        "part_name": (form.get("part_name") or "").strip() or None,
        "description": (form.get("description") or "").strip(),
        "scale_basis": form.get("scale_basis") or base.scale_basis,
        "general_tolerance_mm": f("general_tolerance_mm"),
        "material_text": (form.get("material_text") or "").strip() or None,
        "finish_text": (form.get("finish_text") or "").strip() or None,
        "dimensions": dims,
        "open_questions": [q.model_dump() for q in base.open_questions if q.id not in resolved],
    })


def spec_from_json(text: str) -> DimensionSpec:
    return DimensionSpec.model_validate(json.loads(text))

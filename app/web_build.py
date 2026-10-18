"""Web routes for Phase 2 build jobs (sketch / photo / drawing -> verified model)."""

from __future__ import annotations

import json
import mimetypes
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.build import jobs as bj
from app.build.agent import PlanInvalid, llm_status, plan_from_json
from app.build.schemas import KIND_HELP, DimensionSpec, readiness
from app.db import Job, JobFile, Shop
from app.schemas import JobSpec, RateCard


def _back(url: str, error: str | None = None) -> RedirectResponse:
    from urllib.parse import quote_plus

    return RedirectResponse(f"{url}?error={quote_plus(error)}" if error else url, status_code=303)


def _quantities(raw: str) -> list[int]:
    qs = sorted({int(x) for x in raw.replace(" ", "").split(",") if x})
    if any(q <= 0 for q in qs):
        raise ValueError("Quantities must be positive whole numbers.")
    return qs


def add_build_routes(app: FastAPI, templates, db, shop, Sessions) -> None:
    def get_build(job_id: str, s: Session, sh: Shop) -> Job:
        job = s.get(Job, job_id)
        if job is None or job.shop_id != sh.id or job.kind != "build":
            raise HTTPException(404)
        return job

    # ------------------------------------------------------------ background

    def bg_extract(job_id: str) -> None:
        with Sessions.begin() as s:
            job = s.get(Job, job_id)
            try:
                bj.run_extraction(s, job, app.state.make_extractor())
            except Exception as e:  # never leave a job stuck in "extracting"
                from app.db import StageRun

                job.runs.append(StageRun(run_no=0, stage="extract", status="error", error=f"{type(e).__name__}: {e}"))
                if bj.current_spec(job) is None:
                    bj.start_spec(s, job)
                job.status = "needs_spec"

    def bg_generate(job_id: str, max_attempts: int) -> None:
        s = Sessions()
        try:
            job = s.get(Job, job_id)
            try:
                bj.run_generation(s, job, app.state.make_planner(), max_attempts, commit=s.commit)
            except Exception as e:
                s.rollback()
                job = s.get(Job, job_id)
                from app.build.schemas import AttemptRecord

                bj._record_attempt(s, job, AttemptRecord(attempt=len(bj.attempts(job)) + 1, source="planner",
                                                         plan_error=f"Generation crashed: {type(e).__name__}: {e}"))
                job.status = "failed"
            s.commit()
        finally:
            s.close()

    # ----------------------------------------------------------------- pages

    @app.get("/builds", response_class=HTMLResponse)
    def builds(request: Request, error: str | None = None, s: Session = Depends(db), sh: Shop = Depends(shop)):
        rows = s.scalars(select(Job).where(Job.shop_id == sh.id, Job.kind == "build").order_by(Job.created_at.desc())).all()
        ok, llm_msg = llm_status()
        return templates.TemplateResponse(request, "builds.html", {
            "jobs": rows, "shop": sh, "rc": RateCard.model_validate(sh.rate_card), "error": error,
            "llm_ok": ok, "llm_msg": llm_msg})

    @app.post("/builds")
    async def create_build(background: BackgroundTasks, files: list[UploadFile] = File(...), title: str = Form(""),
                           notes: str = Form(""), material_id: str = Form(""), quantities: str = Form(""),
                           finish_ids: list[str] = Form([]), s: Session = Depends(db), sh: Shop = Depends(shop)):
        uploads = [f for f in files if f.filename]
        if not uploads:
            return _back("/builds", "Upload a sketch, photo or drawing.")
        try:
            spec = JobSpec(material_id=material_id or None, quantities=_quantities(quantities),
                           finish_ids=[f for f in finish_ids if f], customer_notes=notes or None)
        except (ValueError, ValidationError) as e:
            return _back("/builds", str(e))
        job = Job(shop_id=sh.id, kind="build", title=title.strip() or Path(uploads[0].filename).stem,
                  spec=spec.model_dump(mode="json"), status="needs_spec")
        s.add(job)
        s.flush()
        try:
            for up in uploads:
                fid, path, sha = storage.save_original(job.id, up.filename, await up.read())
                job.files.append(JobFile(id=fid, original_name=up.filename, path=str(path), sha256=sha,
                                         size_bytes=path.stat().st_size))
        except ValueError as e:
            storage.delete_job_files(job.id)
            s.rollback()
            return _back("/builds", str(e))
        bj.start_spec(s, job)
        ok, _ = llm_status()
        if ok and bj.input_paths(job):
            job.status = "extracting"
            s.commit()  # the background task opens its own session
            background.add_task(bg_extract, job.id)
        return _back(f"/builds/{job.id}")

    @app.get("/builds/{job_id}", response_class=HTMLResponse)
    def build_view(job_id: str, request: Request, error: str | None = None, s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_build(job_id, s, sh)
        spec = bj.current_spec(job) or DimensionSpec()
        confirmed = bj.confirmed_spec(job)
        ok, llm_msg = llm_status()
        children = s.scalars(select(Job).where(Job.parent_id == job.id).order_by(Job.created_at.desc())).all()
        atts = bj.attempts(job)
        latest = atts[-1] if atts else None
        example_plan = latest.plan.model_dump_json(indent=2) if latest and latest.plan else json.dumps({
            "stock": {"kind": "box", "size": {"x": 100, "y": 60, "z": 20}, "corner_radius": None},
            "operations": [], "assumptions": []}, indent=2)
        ext = bj.extraction(job)
        return templates.TemplateResponse(request, "build.html", {
            "job": job, "shop": sh, "error": error, "spec": spec, "confirmed": confirmed,
            "issues": readiness(spec), "attempts": list(reversed(atts)), "latest": latest,
            "accepted": bj.accepted_attempt(job), "children": children, "llm_ok": ok, "llm_msg": llm_msg,
            "extraction": ext, "kinds": list(KIND_HELP.keys()), "kind_help": KIND_HELP,
            "example_plan": example_plan, "busy": job.status in bj.BUSY,
            "input_files": [f for f in job.files if Path(f.original_name).suffix.lower() in bj.INPUT_SUFFIXES],
            "jobspec": JobSpec.model_validate(job.spec),
        })

    # --------------------------------------------------------------- actions

    def act(job_id: str, fn, s: Session, sh: Shop, ok_url: str | None = None):
        job = get_build(job_id, s, sh)
        try:
            fn(job)
        except (bj.BuildJobError, ValueError, ValidationError, PlanInvalid) as e:
            s.rollback()
            return _back(f"/builds/{job_id}", str(e))
        s.commit()  # before any background task reads the job
        return _back(ok_url or f"/builds/{job_id}")

    @app.post("/builds/{job_id}/extract")
    def extract(job_id: str, background: BackgroundTasks, actor: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        def fn(job):
            ok, msg = llm_status()
            if not ok:
                raise bj.BuildJobError(msg)
            bj._require_actor(actor)
            bj._require_idle(job)
            if job.status == "accepted":
                raise bj.BuildJobError("The model is accepted; the spec is frozen.")
            bj._event(s, job, "extract", actor)
            job.status = "extracting"
            background.add_task(bg_extract, job.id)
        return act(job_id, fn, s, sh)

    @app.post("/builds/{job_id}/spec")
    async def save_spec(job_id: str, request: Request, s: Session = Depends(db), sh: Shop = Depends(shop)):
        form = await request.form()
        def fn(job):
            base = bj.current_spec(job) or DimensionSpec()
            bj.save_spec(s, job, bj.spec_from_form(form, base), form.get("actor", ""))
        return act(job_id, fn, s, sh)

    @app.post("/builds/{job_id}/spec-json")
    def save_spec_json(job_id: str, raw: str = Form(...), actor: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        def fn(job):
            try:
                spec = bj.spec_from_json(raw)
            except json.JSONDecodeError as e:
                raise ValueError(f"Not valid JSON: {e}")
            bj.save_spec(s, job, spec, actor)
        return act(job_id, fn, s, sh)

    @app.post("/builds/{job_id}/confirm")
    def confirm(job_id: str, actor: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        return act(job_id, lambda job: bj.confirm_spec(s, job, actor), s, sh)

    @app.post("/builds/{job_id}/generate")
    def gen(job_id: str, background: BackgroundTasks, actor: str = Form(""), max_attempts: int = Form(3),
            s: Session = Depends(db), sh: Shop = Depends(shop)):
        def fn(job):
            ok, msg = llm_status()
            if not ok:
                raise bj.BuildJobError(msg + " Or paste a build plan below.")
            bj.begin_generation(s, job, actor)
            background.add_task(bg_generate, job.id, max(1, min(5, max_attempts)))
        return act(job_id, fn, s, sh)

    @app.post("/builds/{job_id}/plan")
    def manual_plan(job_id: str, raw: str = Form(...), actor: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        return act(job_id, lambda job: bj.run_manual_plan(s, job, plan_from_json(raw), actor), s, sh)

    @app.post("/builds/{job_id}/reset")
    def reset(job_id: str, actor: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        def fn(job):
            bj._require_actor(actor)
            if job.status not in bj.BUSY:
                raise bj.BuildJobError("Job is not running.")
            bj._event(s, job, "reset", actor, note=f"was {job.status}")
            job.status = "ready" if bj.confirmed_spec(job) else "spec_review"
        return act(job_id, fn, s, sh)

    @app.post("/builds/{job_id}/accept")
    def accept(job_id: str, actor: str = Form(""), note: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        return act(job_id, lambda job: bj.accept_model(s, job, actor, note or None), s, sh)

    @app.post("/builds/{job_id}/to-quote")
    def to_quote(job_id: str, actor: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_build(job_id, s, sh)
        try:
            quote = bj.send_to_quote(s, job, sh.id, RateCard.model_validate(sh.rate_card), actor)
        except bj.BuildJobError as e:
            s.rollback()
            return _back(f"/builds/{job_id}", str(e))
        return _back(f"/jobs/{quote.id}")

    @app.post("/jobs/{job_id}/fix/{flag_id}")
    def dfm_fix(job_id: str, flag_id: str, actor: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        quote = s.get(Job, job_id)
        if quote is None or quote.shop_id != sh.id or not quote.parent_id:
            raise HTTPException(404)
        build = get_build(quote.parent_id, s, sh)
        try:
            bj.apply_dfm_fix(s, quote, build, flag_id, RateCard.model_validate(sh.rate_card), actor)
        except bj.BuildJobError as e:
            s.rollback()
            return _back(f"/jobs/{job_id}", str(e))
        return _back(f"/builds/{build.id}")

    # ----------------------------------------------------------------- files

    def _send(path: Path, job: Job, download: bool = False) -> Response:
        root = storage.job_dir(job.id).resolve()
        p = path.resolve()
        if not p.is_relative_to(root) or not p.exists():
            raise HTTPException(404)
        media = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix == ".stl":
            media = "model/stl"
        headers = {"Cache-Control": "private, no-store"}
        return FileResponse(p, media_type=media, headers=headers,
                            filename=p.name if download else None, content_disposition_type="attachment" if download else "inline")

    @app.get("/builds/{job_id}/attempts/{n}/{key}")
    def attempt_file(job_id: str, n: int, key: str, download: int = 0, s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_build(job_id, s, sh)
        rec = next((a for a in bj.attempts(job) if a.attempt == n), None)
        if rec is None or key not in rec.files:
            raise HTTPException(404)
        return _send(Path(rec.files[key].path), job, bool(download))

    @app.get("/jobs/{job_id}/files/{file_id}")
    def original_file(job_id: str, file_id: str, download: int = 0, s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = s.get(Job, job_id)
        if job is None or job.shop_id != sh.id:
            raise HTTPException(404)
        f = next((f for f in job.files if f.id == file_id), None)
        if f is None:
            raise HTTPException(404)
        return _send(Path(f.path), job, bool(download))

    @app.get("/jobs/{job_id}/files/{file_id}/mesh.stl")
    def original_mesh(job_id: str, file_id: str, s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = s.get(Job, job_id)
        if job is None or job.shop_id != sh.id:
            raise HTTPException(404)
        f = next((f for f in job.files if f.id == file_id), None)
        if f is None:
            raise HTTPException(404)
        out = storage.job_dir(job.id) / "derived" / f"{f.id}.stl"
        if not out.exists():
            from app.build.export import write_stl
            from app.pipeline.geometry import GeometryError, load_step

            try:
                shape = load_step(f.path)
            except GeometryError:
                raise HTTPException(422, "Not a readable STEP file")
            out.parent.mkdir(parents=True, exist_ok=True)
            write_stl(shape, out)
        return _send(out, job)

    # ----------------------------------------------------------------- bench

    def bench_root() -> Path:
        return storage.data_root() / "bench"

    @app.get("/bench", response_class=HTMLResponse)
    def bench(request: Request, error: str | None = None, sh: Shop = Depends(shop)):
        from app.build.bench.cases import CASES

        runs = []
        root = bench_root()
        if root.exists():
            for d in sorted((x for x in root.iterdir() if x.is_dir()), reverse=True)[:30]:
                rep = d / "report.json"
                summary = json.loads(rep.read_text())["summary"] if rep.exists() else None
                runs.append({"name": d.name, "summary": summary})
        ok, llm_msg = llm_status()
        return templates.TemplateResponse(request, "bench.html", {
            "shop": sh, "error": error, "runs": runs, "cases": CASES, "llm_ok": ok, "llm_msg": llm_msg})

    @app.post("/bench")
    def start_bench(background: BackgroundTasks, agent: str = Form("oracle"), cases: list[str] = Form([]),
                    variants: list[str] = Form([]), attempts: int = Form(3)):
        from app.build.bench.run import AGENTS, run_bench

        if agent not in AGENTS:
            return _back("/bench", "Unknown agent.")
        ok, msg = llm_status()
        if agent.startswith("live") and not ok:
            return _back("/bench", msg)
        background.add_task(run_bench, agent, cases or None, variants or ["drawing"], bench_root(), max(1, min(5, attempts)))
        return _back("/bench")

    @app.get("/bench/{run}/{path:path}")
    def bench_file(run: str, path: str):
        root = bench_root().resolve()
        p = (root / run / path).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "no-store"})


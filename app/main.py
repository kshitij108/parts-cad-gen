"""FastAPI app: upload, job/package view, line review, approval, rate card.

Run with: uvicorn app.main:create_app --factory --reload

Phase 1 scaffold: single shop, no auth. The reviewer types their name on each
decision. Every query is still scoped to a shop so multi-tenant auth can slot
in without restructuring.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import review, storage
from app.build import jobs as build_jobs
from app.build.agent import ClaudeExtractor, ClaudePlanner
from app.db import Job, JobFile, ReviewEvent, Shop, make_engine, make_sessionmaker
from app.web_build import add_build_routes
from app.pipeline.runner import latest_outputs, open_questions, run_pipeline
from app.schemas import DfmReport, DrawingExtraction, GeometrySummary, IntakeResult, JobSpec, RateCard

HERE = Path(__file__).parent
EXAMPLE_RATE_CARD = json.loads((HERE / "example_rate_card.json").read_text())


def _parse_quantities(raw: str) -> list[int]:
    try:
        qs = sorted({int(x) for x in raw.replace(" ", "").split(",") if x})
    except ValueError:
        raise ValueError("Quantities must be whole numbers separated by commas.")
    if any(q <= 0 for q in qs):
        raise ValueError("Quantities must be positive.")
    return qs


def _parse_float(raw: str | None) -> float | None:
    raw = (raw or "").strip()
    return float(raw) if raw else None


def _back(url: str, error: str | None = None) -> RedirectResponse:
    return RedirectResponse(f"{url}?error={quote_plus(error)}" if error else url, status_code=303)


def create_app(db_url: str | None = None) -> FastAPI:
    engine = make_engine(db_url)
    Sessions = make_sessionmaker(engine)
    app = FastAPI(title="parts-cad")
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["money"] = lambda v: f"{v:,.2f}" if v is not None else "—"

    with Sessions.begin() as s:
        if s.scalar(select(Shop)) is None:
            s.add(Shop(name=EXAMPLE_RATE_CARD["shop_name"], rate_card=EXAMPLE_RATE_CARD, rate_card_is_example=True))

    # Claude-backed by default; tests and the benchmark swap in offline stand-ins.
    app.state.make_extractor = ClaudeExtractor
    app.state.make_planner = ClaudePlanner

    def db():
        # Plain session with an explicit commit, so routes that hand work to a
        # background task can commit first (the task opens its own session).
        s = Sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def shop(s: Session = Depends(db)) -> Shop:
        return s.scalar(select(Shop))

    def get_job(job_id: str, s: Session, sh: Shop) -> Job:
        job = s.get(Job, job_id)
        if job is None or job.shop_id != sh.id:
            raise HTTPException(404)
        return job

    # ------------------------------------------------------------------ jobs

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, error: str | None = None, s: Session = Depends(db), sh: Shop = Depends(shop)):
        jobs = s.scalars(select(Job).where(Job.shop_id == sh.id, Job.kind == "quote").order_by(Job.created_at.desc())).all()
        rc = RateCard.model_validate(sh.rate_card)
        return templates.TemplateResponse(request, "index.html", {"jobs": jobs, "rc": rc, "shop": sh, "error": error})

    @app.post("/jobs")
    async def create_job(
        files: list[UploadFile] = File(...),
        title: str = Form(""),
        material_id: str = Form(""),
        quantities: str = Form(""),
        finish_ids: list[str] = Form([]),
        deadline: str = Form(""),
        tightest_tolerance_mm: str = Form(""),
        customer_notes: str = Form(""),
        s: Session = Depends(db),
        sh: Shop = Depends(shop),
    ):
        uploads = [f for f in files if f.filename]
        if not uploads:
            return _back("/", "Upload at least one file.")
        try:
            spec = JobSpec(
                material_id=material_id or None,
                quantities=_parse_quantities(quantities),
                finish_ids=[f for f in finish_ids if f],
                deadline=date.fromisoformat(deadline) if deadline else None,
                tightest_tolerance_mm=_parse_float(tightest_tolerance_mm),
                customer_notes=customer_notes or None,
            )
        except (ValueError, ValidationError) as e:
            return _back("/", str(e))
        job = Job(shop_id=sh.id, title=title.strip() or uploads[0].filename, spec=spec.model_dump(mode="json"))
        s.add(job)
        s.flush()
        try:
            for up in uploads:
                fid, path, sha = storage.save_original(job.id, up.filename, await up.read())
                job.files.append(JobFile(id=fid, original_name=up.filename, path=str(path), sha256=sha, size_bytes=path.stat().st_size))
        except ValueError as e:
            storage.delete_job_files(job.id)
            s.rollback()
            return _back("/", str(e))
        s.flush()
        run_pipeline(s, job, RateCard.model_validate(sh.rate_card))
        return _back(f"/jobs/{job.id}")

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_view(job_id: str, request: Request, error: str | None = None, s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_job(job_id, s, sh)
        if job.kind == "build":
            return RedirectResponse(f"/builds/{job.id}" + (f"?{request.url.query}" if request.url.query else ""), status_code=303)
        runs = latest_outputs(job)

        def out(stage, model):
            r = runs.get(stage)
            return model.model_validate(r.output) if r and r.status == "ok" and r.output else None

        return templates.TemplateResponse(request, "job.html", {
            "job": job, "error": error, "runs": runs, "shop": sh,
            "rc": RateCard.model_validate(sh.rate_card),
            "spec": JobSpec.model_validate(job.spec),
            "intake": out("intake", IntakeResult),
            "geometry": out("geometry", GeometrySummary),
            "drawing": out("drawing", DrawingExtraction),
            "dfm": out("dfm", DfmReport),
            "quote": review.current_quote(job),
            "questions": open_questions(job),
            "decisions": review.line_decisions(job),
            "totals": review.totals(job),
            "blockers": review.approval_blockers(job),
            "parent": s.get(Job, job.parent_id) if job.parent_id else None,
            "fixable": {f.id for f in build_jobs.fixable_flags(job)},
            "model_file": next((f for f in job.files if f.id == (out("intake", IntakeResult).model_file_id if out("intake", IntakeResult) else None)), None),
        })

    @app.post("/jobs/{job_id}/spec")
    def update_spec(
        job_id: str,
        actor: str = Form(...),
        material_id: str = Form(""),
        quantities: str = Form(""),
        finish_ids: list[str] = Form([]),
        deadline: str = Form(""),
        tightest_tolerance_mm: str = Form(""),
        s: Session = Depends(db),
        sh: Shop = Depends(shop),
    ):
        job = get_job(job_id, s, sh)
        url = f"/jobs/{job.id}"
        if job.status == "approved":
            return _back(url, "Approved jobs are frozen.")
        if not actor.strip():
            return _back(url, "Your name is required.")
        try:
            spec = JobSpec.model_validate(job.spec).model_copy(update=dict(
                material_id=material_id or None,
                quantities=_parse_quantities(quantities),
                finish_ids=[f for f in finish_ids if f],
                deadline=date.fromisoformat(deadline) if deadline else None,
                tightest_tolerance_mm=_parse_float(tightest_tolerance_mm),
            ))
        except (ValueError, ValidationError) as e:
            return _back(url, str(e))
        job.spec = spec.model_dump(mode="json")
        s.add(ReviewEvent(job_id=job.id, run_no=job.current_run, target="spec", action="update_spec",
                          note=json.dumps(job.spec), actor=actor.strip()))
        run_pipeline(s, job, RateCard.model_validate(sh.rate_card))
        return _back(url)

    @app.post("/jobs/{job_id}/lines/{line_id}")
    def decide_line(job_id: str, line_id: str, action: str = Form(...), actor: str = Form(""),
                    amount: str = Form(""), note: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_job(job_id, s, sh)
        try:
            review.decide_line(s, job, line_id, action, _parse_float(amount), note or None, actor)
        except (review.ReviewError, ValueError) as e:
            return _back(f"/jobs/{job.id}", str(e))
        return RedirectResponse(f"/jobs/{job.id}#{line_id}", status_code=303)

    @app.post("/jobs/{job_id}/approve")
    def approve(job_id: str, actor: str = Form(""), note: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_job(job_id, s, sh)
        try:
            review.approve_job(s, job, actor, note or None)
        except review.ReviewError as e:
            return _back(f"/jobs/{job.id}", str(e))
        return _back(f"/jobs/{job.id}")

    @app.post("/jobs/{job_id}/reject")
    def reject(job_id: str, actor: str = Form(""), note: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_job(job_id, s, sh)
        try:
            review.reject_job(s, job, actor, note)
        except review.ReviewError as e:
            return _back(f"/jobs/{job.id}", str(e))
        return _back(f"/jobs/{job.id}")

    @app.post("/jobs/{job_id}/delete")
    def delete(job_id: str, confirm: str = Form(""), s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_job(job_id, s, sh)
        if confirm != "DELETE":
            return _back(f"/jobs/{job.id}", "Type DELETE to confirm.")
        storage.delete_job_files(job.id)
        kind = job.kind
        s.delete(job)
        return _back("/builds" if kind == "build" else "/")

    @app.get("/jobs/{job_id}/quote.csv")
    def quote_csv(job_id: str, s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_job(job_id, s, sh)
        q = review.current_quote(job)
        if q is None:
            raise HTTPException(404, "No quote")
        d = review.line_decisions(job)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["quantity", "line_id", "category", "label", "low", "high", "decision", "final", "note", "formula"])
        for qq in q.quantities:
            for l in qq.lines:
                dec = d.get(l.id)
                w.writerow([qq.quantity, l.id, l.category, l.label, l.low, l.high,
                            dec.action if dec else "", dec.amount if dec else "", dec.note if dec else "", l.formula])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="quote-{job.id[:8]}.csv"'})

    @app.get("/jobs/{job_id}/package.json")
    def package_json(job_id: str, s: Session = Depends(db), sh: Shop = Depends(shop)):
        job = get_job(job_id, s, sh)
        if not job.packages:
            raise HTTPException(404, "Not approved yet")
        pkg = max(job.packages, key=lambda p: p.created_at)
        return JSONResponse({"package_id": pkg.id, "approved_by": pkg.approved_by,
                             "approved_at": pkg.created_at.isoformat(), **pkg.snapshot})

    # ------------------------------------------------------------- rate card

    @app.get("/rate-card", response_class=HTMLResponse)
    def rate_card(request: Request, error: str | None = None, sh: Shop = Depends(shop)):
        return templates.TemplateResponse(request, "rate_card.html", {
            "shop": sh, "rc": RateCard.model_validate(sh.rate_card), "error": error,
            "raw": json.dumps(sh.rate_card, indent=2),
        })

    @app.post("/rate-card")
    def save_rate_card(raw: str = Form(...), s: Session = Depends(db), sh: Shop = Depends(shop)):
        try:
            rc = RateCard.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            return _back("/rate-card", f"Not saved: {e}")
        sh.rate_card = rc.model_dump(mode="json")
        sh.name = rc.shop_name
        sh.rate_card_is_example = False
        return _back("/rate-card")

    add_build_routes(app, templates, db, shop, Sessions)
    return app


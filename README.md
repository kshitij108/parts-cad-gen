# parts-cad

Check and quote workspace for machined parts. Phase 1 covers 3-axis CNC milling from STEP files (plus a PDF drawing when there is one). A shop uploads a customer's files and gets back a geometry summary, a DFM report and a draft quote with a range on every line. A person reviews every line before the package is approved and frozen.

See [docs/phase1-scope.md](docs/phase1-scope.md) for scope decisions and how they differ from the original spec.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn app.main:create_app --factory --reload
# open http://127.0.0.1:8000
```

Data (SQLite database and uploaded originals) goes in `./data/`, which git ignores. To put it elsewhere, set `PARTS_CAD_DATA` and `PARTS_CAD_DB`.

The app starts with an **example rate card** and shows a banner until you replace it at `/rate-card`.

## Test

```bash
.venv/bin/pytest -q
```

The tests build their STEP parts with OpenCascade (`tests/fixtures.py`), so no binary fixtures are checked in.

## Pipeline

```
upload ─▶ intake ─▶ geometry ─▶ drawing ─▶ dfm ─▶ quote ─▶ human review ─▶ package
          (files,    (STEP via   (PDF;     (rules)  (rate   (per line)      (frozen
          questions)  OpenCascade) stub)             card)                   snapshot)
```

| Module | Does | Deterministic? |
|---|---|---|
| `app/pipeline/intake.py` | Identifies file types from content, lists blocking and non-blocking questions. Never fills in a default. | yes |
| `app/pipeline/geometry.py` | Bounding box, volume, area, face types, hole candidates, internal radii, sharp internal edges, wall thickness by ray sampling. | yes |
| `app/pipeline/drawing.py` | Reads the PDF drawing. **Stubbed**: returns `not_implemented` and questions. A vision model will go behind the `DrawingExtractor` protocol. | no (later) |
| `app/pipeline/dfm.py` | Machine envelope, thin walls, deep or small holes, internal corners, tolerance vs. machine capability. Lists the checks it did not run. | yes |
| `app/pipeline/quote.py` | Material, setup, programming, machining, inspection, finishing, margin and minimum charge, per quantity, each with formula, evidence and a low/high range. Blocks instead of guessing. | yes |
| `app/pipeline/runner.py` | Runs the stages, validates each output against its schema, and records every run. The only code that writes to the database. | — |
| `app/review.py` | Per-line approve/edit/reject (edits and rejections need a note), approval gate, frozen package. | — |

All contracts between stages are in `app/schemas.py`.

**Role limits are built into the code structure.** Each stage function receives only its typed input. No stage gets a database session or a storage handle, so intake can't write files and the quote stage can't change geometry.

## Not built yet

- Drawing extraction (vision model), and linking drawing tolerances to model faces
- Pocket and undercut recognition, and setup planning beyond "2 + off-axis hole directions"
- Auth and multi-tenancy (a single shop for now; queries are already scoped by shop)
- Background job queue (the pipeline currently runs inside the upload request)
- Calibration from a shop's past quotes, and a structured rate-card editor
- ERP export
- 3D preview

# parts-cad

Workspace for machined parts (3-axis CNC milling). It has two flows:

- **Check + quote (Phase 1).** A shop uploads a customer's STEP file, plus the PDF drawing when there is one. It gets back a geometry summary, a DFM report and a draft quote with a range on every line. A person reviews every line before the package is approved and frozen.
- **Build (Phase 2).** Someone uploads a sketch, photo, scanned drawing or PDF print. The dimensions are read and shown for confirmation. A model is then generated, and it is shown as finished only after it measures correctly against those dimensions. The output is a STEP file plus a 2D drawing, and the part can go straight into check + quote.

Background:
- [docs/phase1-scope.md](docs/phase1-scope.md): scope decisions for Phase 1.
- [docs/phase2-build.md](docs/phase2-build.md): how generated models are built and verified, and how to test generation.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/uvicorn app.main:create_app --factory --reload
# open http://127.0.0.1:8000
```

Data (SQLite database, uploaded originals, generated files, benchmark runs) goes in `./data/`, which git ignores. To put it elsewhere, set `PARTS_CAD_DATA` and `PARTS_CAD_DB`.

The app starts with an **example rate card** and shows a banner until you replace it at `/rate-card`.

### Turning on Claude (build flow)

Reading inputs and planning models use Claude. It is **off by default**, because turning it on sends input files to the Anthropic API.

```bash
export PARTS_CAD_LLM=anthropic
export ANTHROPIC_API_KEY=...        # or run `ant auth login` once
export ANTHROPIC_WORKSPACE_ID=wrkspc_...   # only for user-scoped keys (sk-ant-usr-...), which must name a workspace
# optional: PARTS_CAD_MODEL (default claude-opus-5-5), PARTS_CAD_EFFORT (default high)
```

Use an Anthropic organisation with zero-data-retention terms before processing customer files. Requests opt into server-side refusal fallbacks (`fallbacks: "default"`).

With Claude off, the build flow still works end to end: type or paste the dimensions, then paste a build plan (JSON). Everything downstream (building, verification, drawings, STEP, quoting) is deterministic and needs no API.

## Test

```bash
.venv/bin/pytest -q                          # 131 tests, ~25 s, no network
.venv/bin/python -m app.build.bench --agent oracle    # harness sanity: must pass every case
.venv/bin/python -m app.build.bench --agent mutant    # verifier sensitivity: must catch every error
.venv/bin/python -m app.build.bench --agent live --variants drawing,sketch   # Claude end to end (costs API calls)
```

Benchmark reports are written to `data/bench/<run>/report.html`. They can also be started and browsed from the **Bench** page in the app. See [docs/phase2-build.md](docs/phase2-build.md#testing-generation) for what each agent measures.

The tests build their STEP parts with OpenCascade (`tests/fixtures.py`, `app/build/bench/cases.py`), so no binary fixtures are checked in.

## Check + quote pipeline

```
upload ─▶ intake ─▶ geometry ─▶ drawing ─▶ dfm ─▶ quote ─▶ human review ─▶ package
```

| Module | Does | Deterministic? |
|---|---|---|
| `app/pipeline/intake.py` | Identifies file types from content and lists blocking and non-blocking questions. Never fills in a default. | yes |
| `app/pipeline/geometry.py` | Bounding box, volume, area, face types, hole candidates, internal radii, sharp internal edges, and wall thickness by ray sampling. | yes |
| `app/pipeline/drawing.py` | Reads the PDF drawing. **Stubbed** in the quote flow. Returns `not_implemented` plus questions. | no (later) |
| `app/pipeline/dfm.py` | Machine envelope, thin walls, deep or small holes, internal corners, and tolerance vs. machine capability. Lists the checks it did not run. | yes |
| `app/pipeline/quote.py` | One line each for material, setup, programming, machining, inspection, finishing, margin and minimum charge, per quantity. Every line has a formula, evidence and a low/high range. Blocks instead of guessing. | yes |
| `app/pipeline/runner.py` | Runs the stages, validates each output against its schema, and records every run. | — |
| `app/review.py` | Per-line approve/edit/reject (edits and rejections need a note), the approval gate, and the frozen package. | — |

## Build pipeline

```
inputs ─▶ extract ─▶ person confirms dimensions ─▶ plan ─▶ build ─▶ verify ─┬─▶ person accepts ─▶ STEP + drawing ─▶ check + quote
          (Claude)                                  (Claude     (OpenCascade)  │
                                                     or person)                └─▶ failures fed back to the planner (up to N attempts)
```

| Module | Does | Deterministic? |
|---|---|---|
| `app/build/schemas.py` | The dimension spec (what must be true), the build plan (a small feature language) and the verification report. | — |
| `app/build/agent.py` | Claude extractor and planner (structured JSON output), plus offline stand-ins. | no |
| `app/build/builder.py` | Plan → solid. Stock: box, cylinder, extruded profile. Operations: holes (with counterbores and bolt circles), pockets/slots, bosses, box cuts. | yes |
| `app/build/measure.py`, `verify.py` | Measures the solid against the confirmed dimensions to 0.01 mm. Also checks that the solid is valid, closed, a single body, survives a STEP round trip, and has no unconfirmed holes. | yes |
| `app/build/sheet.py` | 2D drawing from hidden-line projections of the model. Every number on it is measured. Output as SVG, PDF and PNG. | yes |
| `app/build/fixes.py` | Accepted DFM fixes as plan edits (e.g. inside corner radii), then rebuilt and re-verified. | yes |
| `app/build/jobs.py`, `app/web_build.py` | Build jobs, the review trail, sign-off, the hand-off to quoting, and the routes. | — |
| `app/build/bench/` | Reference parts and the scoring harness. | — |

**Role limits are built into the code structure.** No stage gets a database session or storage handle. The LLM steps only produce a spec or a plan, never geometry or a pass/fail verdict.

## Not built yet

- Drawing extraction in the *quote* flow. The build flow's extractor is ready to reuse there, to check a customer's STEP against their drawing.
- Pocket and undercut recognition on uploaded STEP files; setup planning beyond "2 + off-axis hole directions".
- Turned parts (revolve), fillets/chamfers on arbitrary edges, threads, and angled holes in the build flow.
- Auth and multi-tenancy (a single shop for now; queries are already scoped by shop).
- A real job queue: background work runs in-process, so a server restart mid-run leaves a job "running". Use the "Stop waiting" button to reset it.
- Calibration from a shop's past quotes, a structured rate-card editor, and ERP export.

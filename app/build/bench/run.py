"""Build benchmark: can the agent rebuild known parts from drawings alone?

For each reference case:
  1. build the known-good plan -> ground-truth solid + ground-truth dimensions
  2. render the input the agent sees (drawing, sketch, or a bare picture)
  3. agent extracts a spec from the input, then plans/builds/verifies (with retries)
  4. score the result against ground truth, not against the agent's own spec

The key failure this exposes: a model that passes its *own* verification but
fails ground truth means the agent misread the input. That is the confident
wrong answer the product must never ship, so the report calls it out.

Agents:
  oracle     ground-truth spec and plan (harness sanity check: must score 100%)
  mutant     ground-truth spec, plan with one deliberate error (verifier must catch all)
  live-plan  ground-truth spec, Claude plans (isolates planning)
  live       Claude extracts and plans (end to end)

    python -m app.build.bench --agent oracle
    python -m app.build.bench --agent live --cases block_pocket,l_bracket --variants drawing,sketch
"""

from __future__ import annotations

import argparse
import html
import json
import time
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Trsf, gp_Vec

from app.build.agent import ClaudeExtractor, ClaudePlanner, LLMError, StaticExtractor, StaticPlanner
from app.build.bench.cases import BY_NAME, CASES, BenchCase, mutator
from app.build.bench.inputs import VARIANTS, preview_png, render_input
from app.build.builder import build
from app.build.derive import derive_spec, no_scale_spec
from app.build.measure import Measurer
from app.build.pipeline import generate
from app.build.schemas import DimensionSpec, RequiredDimension, readiness
from app.build.sheet import make_sheet, to_svg
from app.build.verify import check_dimension, verify

AGENTS = ("oracle", "mutant", "live-plan", "live")


@dataclass
class CaseResult:
    case: str
    variant: str
    expect: str
    outcome: str = "error"  # verified | failed | refused | blocked | error
    passed: bool = False
    self_verified: bool = False
    confident_wrong: bool = False
    attempts: int = 0
    gt_pass: int = 0
    gt_total: int = 0
    iou: float | None = None
    bbox_error_mm: float | None = None
    holes_expected: int = 0
    holes_found: int | None = None
    extraction_recall: float | None = None  # literal: reference dims restated the same way
    extracted_dims: int = 0
    extracted_correct: int | None = None  # agent's dims that hold on the reference part (misreads fail)
    mutation: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)


def _volume(shape) -> float:
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    return p.Mass()


def _at_origin(shape):
    m = Measurer(shape)
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(-m.lo[0], -m.lo[1], -m.lo[2]))
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def iou(a, b) -> float:
    a, b = _at_origin(a), _at_origin(b)
    common = BRepAlgoAPI_Common(a, b)
    common.Build()
    inter = _volume(common.Shape()) if common.IsDone() else 0.0
    union = _volume(a) + _volume(b) - inter
    return inter / union if union > 0 else 0.0


def _matches(g: RequiredDimension, x: RequiredDimension) -> bool:
    if g.kind != x.kind or g.through != x.through:
        return False
    for attr in ("axis", "direction"):
        if getattr(g, attr) and getattr(x, attr) and getattr(g, attr) != getattr(x, attr):
            return False
    if not g.through and (x.nominal is None or abs((g.nominal or 0) - x.nominal) > 0.01):
        return False
    if g.kind == "hole_count" and (x.feature_diameter is None or abs(g.feature_diameter - x.feature_diameter) > 0.01):
        return False
    if g.at and x.at:
        if max(abs(g.at.x - x.at.x), abs(g.at.y - x.at.y), abs(g.at.z - x.at.z)) > 3.0 and g.kind.startswith("hole"):
            return False
    return True


def extraction_recall(gt: DimensionSpec, got: DimensionSpec) -> float:
    if not gt.dimensions:
        return 1.0
    pool = list(got.dimensions)
    hit = 0
    for g in gt.dimensions:
        m = next((x for x in pool if _matches(g, x)), None)
        if m is not None:
            pool.remove(m)
            hit += 1
    return hit / len(gt.dimensions)


def agents_for(agent: str, case: BenchCase, gt_spec: DimensionSpec):
    if agent == "oracle":
        return StaticExtractor(gt_spec), StaticPlanner(case.build_plan(), source="oracle")
    if agent == "mutant":
        return StaticExtractor(gt_spec), StaticPlanner(case.build_plan(), mutate=mutator(case.name), source="mutant")
    if agent == "live-plan":
        return StaticExtractor(gt_spec), ClaudePlanner()
    if agent == "live":
        return ClaudeExtractor(), ClaudePlanner()
    raise ValueError(f"unknown agent {agent}")


def run_case(case: BenchCase, variant: str, agent: str, out: Path, attempts: int) -> CaseResult:
    t0 = time.time()
    res = CaseResult(case=case.name, variant="picture" if case.picture_only else variant, expect=case.expect)
    d = out / "cases" / f"{case.name}-{res.variant}"
    d.mkdir(parents=True, exist_ok=True)

    gt_plan = case.build_plan()
    gt_shape = build(gt_plan)
    if case.picture_only:
        gt_spec = no_scale_spec(case.title)
        sheet = make_sheet(gt_shape, None, None, mode="input", views=("iso",), annotate=False, chrome=False)
        drawing_spec = None
    else:
        gt_spec, dropped = derive_spec(gt_plan, gt_shape, extras=case.extras, part_name=case.title,
                                       description="", material_text=case.material)
        if dropped:
            res.notes.append("Ground truth dropped: " + "; ".join(dropped))
        sheet = make_sheet(gt_shape, gt_spec, None, mode="input", title=case.title, material=case.material)
        drawing_spec = gt_spec
    name, data = render_input(sheet, res.variant, zlib.crc32(case.name.encode()) % 1000)
    inp = d / name
    inp.write_bytes(data)
    (d / "input_preview.png").write_bytes(preview_png(sheet, res.variant, data))
    res.files["input"] = str((d / "input_preview.png").relative_to(out))
    res.files["input_file"] = str(inp.relative_to(out))
    (d / "ground_truth.json").write_text(json.dumps({"plan": gt_plan.model_dump(mode="json"),
                                                     "spec": gt_spec.model_dump(mode="json")}, indent=1))
    res.holes_expected = len(Measurer(gt_shape).holes)

    extractor, planner = agents_for(agent, case, gt_spec)
    try:
        spec, usage = extractor.extract([inp], None)
    except LLMError as e:
        res.outcome, res.notes = "error", res.notes + [f"Extraction failed: {e}"]
        res.seconds = time.time() - t0
        return res
    if usage:
        res.tokens_in += usage.get("input_tokens") or 0
        res.tokens_out += usage.get("output_tokens") or 0
    (d / "extracted_spec.json").write_text(spec.model_dump_json(indent=1))
    res.extracted_dims = len(spec.dimensions)
    if drawing_spec is not None:
        res.extraction_recall = round(extraction_recall(gt_spec, spec), 3)
        gt_m = Measurer(gt_shape)
        checks = [check_dimension(x, gt_m) for x in spec.dimensions]
        res.extracted_correct = sum(c.passed for c in checks)
        res.notes += [f"Misread {c.dim_id} {c.label}: wrote {c.expected}, reference part measures {c.measured or 'nothing'}"
                      for c in checks if not c.passed][:8]

    issues = readiness(spec)
    if case.expect == "refusal":
        res.outcome = "refused" if issues else "failed"
        res.passed = bool(issues)
        if not issues:
            res.notes.append(f"Invented {len(spec.dimensions)} dimensions from an input with no scale.")
        res.seconds = time.time() - t0
        return res
    if issues:
        res.outcome = "blocked"
        res.notes += [f"Blocked: {i}" for i in issues]
        res.seconds = time.time() - t0
        return res

    recs = generate(spec, [inp], planner, d, max_attempts=1 if agent == "mutant" else attempts,
                    title=case.title, material=case.material)
    res.attempts = len(recs)
    for r in recs:
        if r.llm_usage:
            res.tokens_in += r.llm_usage.get("input_tokens") or 0
            res.tokens_out += r.llm_usage.get("output_tokens") or 0
    if isinstance(planner, StaticPlanner):
        res.mutation = planner.mutation
    (d / "attempts.json").write_text(json.dumps([r.model_dump(mode="json") for r in recs], indent=1))
    last = recs[-1] if recs else None
    res.self_verified = bool(last and last.passed)
    if last and last.plan is not None and not last.build_error:
        shape = build(last.plan)
        gt_report = verify(shape, gt_spec, None)
        res.gt_total = len(gt_report.dimensions)
        res.gt_pass = sum(1 for c in gt_report.dimensions if c.passed)
        res.iou = round(iou(shape, gt_shape), 5)
        m, g = Measurer(shape), Measurer(gt_shape)
        res.bbox_error_mm = round(max(abs(m.size[i] - g.size[i]) for i in range(3)), 4)
        res.holes_found = len(m.holes)
        res.notes += [f"Ground truth: {c.dim_id} {c.label}: expected {c.expected}, measured {c.measured or 'nothing'}"
                      for c in gt_report.dimensions if not c.passed][:8]
        (d / "generated.svg").write_text(to_svg(make_sheet(shape, spec, last.report, mode="deliverable", title=case.title)))
        res.files["generated"] = str((d / "generated.svg").relative_to(out))
        if "stl" in last.files:
            res.files["stl"] = str(Path(last.files["stl"].path).relative_to(out))
    elif last:
        res.notes.append(last.plan_error or last.build_error or "No model built.")
    gt_ok = res.gt_total > 0 and res.gt_pass == res.gt_total
    res.passed = res.self_verified and gt_ok
    res.confident_wrong = res.self_verified and not gt_ok
    res.outcome = "verified" if res.self_verified else "failed"
    res.seconds = round(time.time() - t0, 2)
    return res


def run_bench(agent: str = "oracle", case_names: list[str] | None = None, variants: list[str] | None = None,
              out_root: Path = Path("data/bench"), attempts: int = 3) -> tuple[Path, list[CaseResult]]:
    if agent not in AGENTS:
        raise ValueError(f"agent must be one of {AGENTS}")
    cases = [BY_NAME[n] for n in case_names] if case_names else CASES
    variants = variants or ["drawing"]
    out = (out_root / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{agent}").resolve()
    out.mkdir(parents=True, exist_ok=True)
    results: list[CaseResult] = []
    for case in cases:
        for v in (["picture"] if case.picture_only else variants):
            try:
                results.append(run_case(case, v, agent, out, attempts))
            except Exception as e:  # one broken case must not hide the others
                results.append(CaseResult(case=case.name, variant=v, expect=case.expect, outcome="error",
                                          notes=[f"{type(e).__name__}: {e}"]))
    summary = summarize(agent, results)
    (out / "report.json").write_text(json.dumps({"summary": summary, "results": [asdict(r) for r in results]}, indent=1))
    (out / "report.html").write_text(render_html(summary, results))
    return out, results


def summarize(agent: str, results: list[CaseResult]) -> dict:
    model_cases = [r for r in results if r.expect == "model"]
    ious = [r.iou for r in model_cases if r.iou is not None]
    recalls = [r.extraction_recall for r in model_cases if r.extraction_recall is not None]
    read = [(r.extracted_correct, r.extracted_dims) for r in model_cases if r.extracted_correct is not None]
    s = {
        "agent": agent,
        "cases": len(results),
        "passed": sum(r.passed for r in results),
        "confident_wrong": sum(r.confident_wrong for r in results),
        "refusals_correct": sum(1 for r in results if r.expect == "refusal" and r.passed),
        "refusal_cases": sum(1 for r in results if r.expect == "refusal"),
        "mean_iou": round(sum(ious) / len(ious), 4) if ious else None,
        "mean_extraction_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "read_correct": sum(c for c, _ in read),
        "read_total": sum(t for _, t in read),
        "tokens_in": sum(r.tokens_in for r in results),
        "tokens_out": sum(r.tokens_out for r in results),
        "seconds": round(sum(r.seconds for r in results), 1),
    }
    if agent == "mutant":
        s["mutations_caught"] = sum(1 for r in model_cases if r.mutation and not r.self_verified)
        s["mutations"] = sum(1 for r in model_cases if r.mutation)
    return s


def render_html(summary: dict, results: list[CaseResult]) -> str:
    e = html.escape
    agent = summary["agent"]
    if agent == "mutant":
        headline = f"{summary.get('mutations_caught', 0)} of {summary.get('mutations', 0)} deliberate errors caught by verification"
    else:
        headline = f"{summary['passed']} of {summary['cases']} cases passed"
    rows = []
    for r in results:
        status = ("caught" if agent == "mutant" and r.mutation and not r.self_verified else
                  "PASS" if r.passed else "CONFIDENT WRONG" if r.confident_wrong else r.outcome.upper())
        cls = "ok" if r.passed or status == "caught" else "bad"
        rows.append(f"<tr class='{cls}'><td><a href='#{e(r.case)}-{e(r.variant)}'>{e(r.case)}</a></td><td>{e(r.variant)}</td>"
                    f"<td>{e(status)}</td><td>{r.gt_pass}/{r.gt_total}</td><td>{'' if r.iou is None else f'{r.iou:.4f}'}</td>"
                    f"<td>{'' if r.extracted_correct is None else f'{r.extracted_correct}/{r.extracted_dims}'}</td><td>{r.attempts}</td>"
                    f"<td>{r.tokens_in + r.tokens_out}</td><td>{r.seconds:.1f}s</td></tr>")
    details = []
    for r in results:
        imgs = (f"<figure><img src='{e(r.files['input'])}'><figcaption>Input the agent saw "
                f"(<a href='{e(r.files.get('input_file', r.files['input']))}'>file</a>, pixels as stored)</figcaption></figure>"
                if "input" in r.files else "")
        if "generated" in r.files:
            imgs += f"<figure><img src='{e(r.files['generated'])}'><figcaption>Generated model (measured)</figcaption></figure>"
        notes = "".join(f"<li>{e(n)}</li>" for n in r.notes)
        extra = f"<p>Mutation: {e(r.mutation)}</p>" if r.mutation else ""
        details.append(f"<section id='{e(r.case)}-{e(r.variant)}'><h2>{e(r.case)} · {e(r.variant)}</h2>{extra}"
                       f"<p>Self-verified: {r.self_verified} · ground truth {r.gt_pass}/{r.gt_total} · IoU {r.iou} · "
                       f"bbox error {r.bbox_error_mm} mm · holes {r.holes_found}/{r.holes_expected} · "
                       f"read {r.extracted_dims} dims, {r.extracted_correct} hold on the reference part "
                       f"(literal recall {r.extraction_recall})</p>"
                       f"<ul>{notes}</ul><div class='figs'>{imgs}</div></section>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Build benchmark</title><style>
:root {{ --bg:#f6f6f4; --fg:#1d1d1b; --muted:#6b6b66; --ok:#e4f3e6; --bad:#fbe4e1; --line:#e2e1dc; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#161615; --fg:#ecebe6; --muted:#9b9a93; --ok:#1b3020; --bad:#3a1f1b; --line:#33332f; }} }}
body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; max-width:1200px; margin:auto; }}
table {{ border-collapse:collapse; width:100%; }} td, th {{ border-bottom:1px solid var(--line); padding:6px; text-align:left; }}
tr.ok td {{ background:var(--ok); }} tr.bad td {{ background:var(--bad); }} .muted {{ color:var(--muted); }}
.figs {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }} figure {{ margin:0; }} img {{ width:100%; border:1px solid var(--line); background:#fff; }}
section {{ margin-top:28px; }} @media (max-width:720px) {{ .figs {{ grid-template-columns:1fr; }} }}
</style></head><body>
<h1>Build benchmark: {e(agent)}</h1>
<p><strong>{e(headline)}</strong>. Confident-wrong (passed its own check, failed ground truth): {summary['confident_wrong']}.
Correct refusals: {summary['refusals_correct']}/{summary['refusal_cases']}. Mean IoU {summary['mean_iou']} ·
dimensions read correctly {summary.get('read_correct')}/{summary.get('read_total')} · tokens {summary['tokens_in']} in / {summary['tokens_out']} out · {summary['seconds']}s.</p>
<p class="muted">Ground truth = dimensions derived from the reference model, measured on the generated model at 0.01 mm.
IoU = shared volume / combined volume with the reference. Read correctly = the agent's own dimensions that hold on the
reference part (a misread fails here even if the agent's model then matches what it misread).</p>
<table><thead><tr><th>Case</th><th>Input</th><th>Result</th><th>Ground truth</th><th>IoU</th><th>Read correctly</th><th>Attempts</th><th>Tokens</th><th>Time</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
{''.join(details)}
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", default="oracle", choices=AGENTS)
    ap.add_argument("--cases", default="", help="comma-separated case names (default: all)")
    ap.add_argument("--variants", default="drawing", help="comma-separated: " + ",".join(VARIANTS))
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--out", default="data/bench")
    a = ap.parse_args(argv)
    names = [c for c in a.cases.split(",") if c] or None
    variants = [v for v in a.variants.split(",") if v in VARIANTS] or ["drawing"]
    out, results = run_bench(a.agent, names, variants, Path(a.out), a.attempts)
    s = summarize(a.agent, results)
    for r in results:
        flag = "PASS" if r.passed else ("CONFIDENT-WRONG" if r.confident_wrong else r.outcome)
        print(f"{r.case:22s} {r.variant:8s} {flag:16s} gt {r.gt_pass}/{r.gt_total} iou {r.iou} {('; ' + r.notes[0]) if r.notes and not r.passed else ''}")
    print(json.dumps(s))
    print(f"report: {out / 'report.html'}")
    return 0

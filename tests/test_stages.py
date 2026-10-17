from app.pipeline import dfm, drawing, geometry, intake, quote
from app.schemas import DfmInput, IntakeInput, JobSpec, QuoteInput, UploadedFile


def _intake(tmp_path, spec, rate_card, names=("part.step",)):
    files = []
    for i, n in enumerate(names):
        p = tmp_path / n
        p.write_bytes(b"ISO-10303-21;\n" if n.endswith(".step") else b"%PDF-1.4\n" if n.endswith(".pdf") else b"x")
        files.append(UploadedFile(id=str(i), original_name=n, path=str(p), sha256="0", size_bytes=1))
    return intake.run(IntakeInput(files=files, spec=spec,
                                  known_material_ids=[m.id for m in rate_card.materials],
                                  known_finish_ids=[f.id for f in rate_card.finishes]))


def test_intake_flags_missing_without_guessing(tmp_path, rate_card):
    r = _intake(tmp_path, JobSpec(), rate_card, names=("part.step", "part.sldprt"))
    ids = {q.id for q in r.open_questions}
    assert {"intake.material", "intake.quantity", "intake.no_drawing", "intake.deadline"} <= ids
    assert r.spec.material_id is None  # not defaulted
    native = next(f for f in r.files if f.original_name == "part.sldprt")
    assert not native.supported and "STEP" in native.note


def test_intake_sniffs_content_over_extension(tmp_path, rate_card):
    p = tmp_path / "drawing.bin"
    p.write_bytes(b"%PDF-1.7 ...")
    assert intake.detect_kind(str(p), "drawing.bin") == "pdf"


def _pipeline(step_dir, rate_card, spec):
    g = geometry.run(step_dir / "bracket.step")
    d = drawing.run(None)
    rep = dfm.run(DfmInput(geometry=g, drawing=d, spec=spec, rate_card=rate_card))
    q = quote.run(QuoteInput(geometry=g, drawing=d, spec=spec, dfm=rep, rate_card=rate_card))
    return g, rep, q


def test_dfm_flags_sharp_corners_and_lists_skipped(step_dir, rate_card):
    _, rep, _ = _pipeline(step_dir, rate_card, JobSpec(material_id="al6061", quantities=[1]))
    rules = {f.rule: f for f in rep.flags}
    assert rules["internal_corners"].severity == "critical"
    assert "deep_pockets" in rep.checks_skipped and "undercuts" in rep.checks_skipped
    assert all(f.evidence for f in rep.flags)


def test_dfm_thin_wall_needs_material(step_dir, rate_card):
    g = geometry.run(step_dir / "thin.step")
    no_mat = dfm.run(DfmInput(geometry=g, drawing=drawing.run(None), spec=JobSpec(), rate_card=rate_card))
    assert "thin_walls" in no_mat.checks_skipped
    with_mat = dfm.run(DfmInput(geometry=g, drawing=drawing.run(None), spec=JobSpec(material_id="al6061"), rate_card=rate_card))
    assert any(f.rule == "thin_walls" for f in with_mat.flags)


def test_dfm_tight_tolerance_beyond_capability(step_dir, rate_card):
    _, rep, _ = _pipeline(step_dir, rate_card, JobSpec(material_id="al6061", quantities=[1], tightest_tolerance_mm=0.01))
    f = next(f for f in rep.flags if f.rule == "tight_tolerances")
    assert f.severity == "critical"


def test_quote_blocks_instead_of_guessing(step_dir, rate_card):
    _, _, q = _pipeline(step_dir, rate_card, JobSpec(quantities=[10]))
    assert q.status == "blocked" and not q.quantities
    assert any(oq.field == "material_id" for oq in q.open_questions)


def test_quote_lines_trace_to_evidence(step_dir, rate_card):
    _, _, q = _pipeline(step_dir, rate_card, JobSpec(material_id="al6061", quantities=[1, 50], finish_ids=["anodize-clear"]))
    assert q.status == "draft" and [qq.quantity for qq in q.quantities] == [1, 50]
    for qq in q.quantities:
        for line in qq.lines:
            assert line.evidence, line.id
            assert line.formula
            assert 0 <= line.low <= line.high
        assert qq.total_low == round(sum(l.low for l in qq.lines), 2) or abs(qq.total_low - sum(l.low for l in qq.lines)) < 0.05
    q1, q50 = q.quantities
    assert q50.unit_high < q1.unit_high  # setup/programming amortise


def test_quote_material_uses_plate_stock(step_dir, rate_card):
    _, _, q = _pipeline(step_dir, rate_card, JobSpec(material_id="al6061", quantities=[1]))
    mat = next(l for l in q.quantities[0].lines if l.category == "material")
    # 20 mm part + 3 mm allowance -> next stocked plate is 25.4 mm
    assert "25.4 mm plate" in mat.assumptions[0]


def test_minimum_order_top_up(step_dir, rate_card):
    rate_card.min_order_charge = 100000
    _, _, q = _pipeline(step_dir, rate_card, JobSpec(material_id="al6061", quantities=[1]))
    qq = q.quantities[0]
    assert any(l.category == "minimum" for l in qq.lines)
    assert qq.total_low == 100000

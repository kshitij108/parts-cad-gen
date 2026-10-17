"""Quote stage: deterministic cost build-up from geometry + the shop's rate card.

No number here is invented. Each line cites the rate-card entries and geometry
values it used, shows its formula, and gives a low/high range. Uncertainty in
time estimates comes from the shop's own calibration factors, not constants in
this file. If an input needed for a line is missing, the quote is blocked with
an open question instead of guessing.
"""

from __future__ import annotations

import math

from app.pipeline.dfm import tightest_tolerance, tool_axis
from app.schemas import (
    Evidence,
    Machine,
    OpenQuestion,
    QuantityQuote,
    QuoteDraft,
    QuoteInput,
    QuoteLine,
)


def _money(x: float, cur: str) -> str:
    return f"{cur} {x:,.2f}"


def _pick_machine(inp: QuoteInput) -> Machine | None:
    part = sorted([inp.geometry.bbox_mm.x, inp.geometry.bbox_mm.y, inp.geometry.bbox_mm.z])
    fits = [m for m in inp.rate_card.machines
            if all(p <= e for p, e in zip(part, sorted([m.envelope_mm.x, m.envelope_mm.y, m.envelope_mm.z])))]
    return min(fits, key=lambda m: m.hourly_rate) if fits else None


def run(inp: QuoteInput) -> QuoteDraft:
    g, rc, spec = inp.geometry, inp.rate_card, inp.spec
    cur = rc.currency
    questions: list[OpenQuestion] = []
    material = rc.material(spec.material_id)
    machine = _pick_machine(inp)

    if material is None:
        questions.append(OpenQuestion(id="quote.material", field="material_id", question="Material is required to price stock and cutting time."))
    if not spec.quantities:
        questions.append(OpenQuestion(id="quote.quantity", field="quantities", question="At least one quantity is required."))
    if machine is None:
        questions.append(OpenQuestion(id="quote.machine", field="machines", question="No machine on the rate card fits this part."))
    if questions:
        return QuoteDraft(status="blocked", currency=cur, quantities=[], machine_id=None, assumptions=[], open_questions=questions)

    cal = rc.calibration
    lab = rc.labor
    assumptions: list[str] = []
    mref = f"rate_card.machines[{machine.id}]"
    matref = f"rate_card.materials[{material.id}]"
    ev_rate = Evidence(source="rate_card", ref=f"{mref}.hourly_rate", detail=f"{_money(machine.hourly_rate, cur)}/h")
    ev_cal = Evidence(source="rate_card", ref="rate_card.calibration", detail=f"×{cal.multiplier} (range ×{cal.low_factor}–×{cal.high_factor})")
    assumptions.append(f"Machine: {machine.name} (cheapest machine on the rate card whose envelope fits the part).")

    # --- Stock -----------------------------------------------------------
    a = material.stock_allowance_mm
    dims = sorted([g.bbox_mm.x + a, g.bbox_mm.y + a, g.bbox_mm.z + a])
    plate = next((t for t in sorted(material.plate_thicknesses_mm) if t >= dims[0]), None)
    if plate is not None:
        stock_note = f"{plate:g} mm plate (next stocked thickness ≥ {dims[0]:.1f} mm), cut to {dims[1]:.1f} × {dims[2]:.1f} mm"
        dims[0] = plate
    else:
        stock_note = f"cut-to-size block {dims[0]:.1f} × {dims[1]:.1f} × {dims[2]:.1f} mm"
        if material.plate_thicknesses_mm:
            assumptions.append(f"No stocked plate thick enough for {material.name}; priced as a cut-to-size block.")
    stock_cm3 = dims[0] * dims[1] * dims[2] / 1000
    stock_kg = stock_cm3 * material.density_g_cm3 / 1000
    material_per_part = stock_kg * material.price_per_kg
    assumptions.append(f"Stock: {stock_note} (bbox + {a:g} mm allowance per dimension). No remnant credit.")

    # --- Cutting time ----------------------------------------------------
    part_cm3 = g.volume_mm3 / 1000
    removal_cm3 = max(0.0, stock_cm3 - part_cm3)
    rough_min = removal_cm3 / material.roughing_cm3_per_min
    area_cm2 = g.surface_area_mm2 / 100
    finish_min = area_cm2 / material.finishing_cm2_per_min
    band, _ = tightest_tolerance(inp.drawing, spec)
    tight = band is not None and band < machine.standard_tolerance_mm
    tight_factor = lab.tight_tolerance_inspection_factor if tight else 1.0
    finish_min *= tight_factor
    cut_min = (rough_min + finish_min) * cal.multiplier
    if tight:
        assumptions.append(f"Tolerance band {band:g} mm is below standard {machine.standard_tolerance_mm:g} mm: finishing and inspection ×{tight_factor:g}.")

    # --- Setups ----------------------------------------------------------
    axis, axis_why = tool_axis(g)
    off_axis = {(h.axis.x, h.axis.y, h.axis.z) for h in g.hole_candidates
                if abs(h.axis.x * axis.x + h.axis.y * axis.y + h.axis.z * axis.z) < 0.999}
    setups = 2 + len(off_axis)
    assumptions.append(f"Setups: {setups} (top + bottom, plus {len(off_axis)} for off-axis holes). Tool axis from {axis_why}. High case allows one extra.")

    feature_count = g.feature_count
    prog_hours = lab.programming_hours_base + lab.programming_hours_per_feature * feature_count
    insp_hours_per_part = lab.inspection_minutes_per_part * tight_factor / 60

    quantities: list[QuantityQuote] = []
    for qty in sorted(set(spec.quantities)):
        p = f"q{qty}"
        lines: list[QuoteLine] = []

        mat = material_per_part * qty
        lines.append(QuoteLine(
            id=f"{p}.material", category="material", label=f"Stock: {material.name}", basis="per_part",
            low=mat, high=mat,
            formula=f"{qty} × {stock_kg:.3f} kg × {_money(material.price_per_kg, cur)}/kg",
            evidence=[Evidence(source="geometry", ref="geometry.bbox_mm", detail=f"{g.bbox_mm.x:g} × {g.bbox_mm.y:g} × {g.bbox_mm.z:g} mm"),
                      Evidence(source="rate_card", ref=f"{matref}.price_per_kg", detail=f"{_money(material.price_per_kg, cur)}/kg"),
                      Evidence(source="rate_card", ref=f"{matref}.density_g_cm3", detail=f"{material.density_g_cm3} g/cm³")],
            assumptions=[stock_note],
        ))

        setup_cost = lab.setup_hours_per_setup * machine.hourly_rate
        lines.append(QuoteLine(
            id=f"{p}.setup", category="setup", label="Machine setup", basis="per_lot",
            low=setups * setup_cost, high=(setups + 1) * setup_cost,
            formula=f"{setups}–{setups + 1} setups × {lab.setup_hours_per_setup:g} h × {_money(machine.hourly_rate, cur)}/h",
            evidence=[Evidence(source="rate_card", ref="rate_card.labor.setup_hours_per_setup", detail=f"{lab.setup_hours_per_setup} h"), ev_rate],
        ))

        prog = prog_hours * machine.hourly_rate
        lines.append(QuoteLine(
            id=f"{p}.programming", category="programming", label="CAM programming", basis="per_lot",
            low=prog * cal.low_factor, high=prog * cal.high_factor,
            formula=f"({lab.programming_hours_base:g} h + {feature_count} features × {lab.programming_hours_per_feature:g} h) × {_money(machine.hourly_rate, cur)}/h × calibration",
            evidence=[Evidence(source="rate_card", ref="rate_card.labor.programming_hours_base", detail=f"{lab.programming_hours_base} h"),
                      Evidence(source="geometry", ref="geometry.feature_count", detail=f"{feature_count} feature candidates"), ev_rate, ev_cal],
        ))

        mach = cut_min / 60 * machine.hourly_rate * qty
        lines.append(QuoteLine(
            id=f"{p}.machining", category="machining", label="Machining", basis="per_part",
            low=mach * cal.low_factor, high=mach * cal.high_factor,
            formula=(f"{qty} × ({removal_cm3:.1f} cm³ ÷ {material.roughing_cm3_per_min:g} cm³/min"
                     f" + {area_cm2:.1f} cm² ÷ {material.finishing_cm2_per_min:g} cm²/min{' × ' + format(tight_factor, 'g') if tight else ''})"
                     f" × {cal.multiplier:g} = {cut_min:.1f} min/part × {_money(machine.hourly_rate, cur)}/h"),
            evidence=[Evidence(source="geometry", ref="geometry.volume_mm3", detail=f"{part_cm3:.1f} cm³ part, {removal_cm3:.1f} cm³ removed"),
                      Evidence(source="geometry", ref="geometry.surface_area_mm2", detail=f"{area_cm2:.1f} cm²"),
                      Evidence(source="rate_card", ref=f"{matref}.roughing_cm3_per_min", detail=f"{material.roughing_cm3_per_min} cm³/min"),
                      Evidence(source="rate_card", ref=f"{matref}.finishing_cm2_per_min", detail=f"{material.finishing_cm2_per_min} cm²/min"),
                      ev_rate, ev_cal],
            assumptions=["Removal-rate model: volume removed + surface finished. Not a toolpath simulation."],
        ))

        insp = insp_hours_per_part * lab.inspection_hourly_rate * qty
        lines.append(QuoteLine(
            id=f"{p}.inspection", category="inspection", label="Inspection", basis="per_part",
            low=insp, high=insp,
            formula=f"{qty} × {lab.inspection_minutes_per_part:g} min{' × ' + format(tight_factor, 'g') if tight else ''} × {_money(lab.inspection_hourly_rate, cur)}/h",
            evidence=[Evidence(source="rate_card", ref="rate_card.labor.inspection_minutes_per_part", detail=f"{lab.inspection_minutes_per_part} min"),
                      Evidence(source="rate_card", ref="rate_card.labor.inspection_hourly_rate", detail=f"{_money(lab.inspection_hourly_rate, cur)}/h")],
        ))

        finish_lead = 0
        for fid in spec.finish_ids:
            f = rc.finish(fid)
            if f is None:
                continue  # intake already raised an open question
            cost = f.lot_charge + f.per_part * qty
            finish_lead = max(finish_lead, f.lead_days)
            lines.append(QuoteLine(
                id=f"{p}.finishing.{f.id}", category="finishing", label=f"Finish: {f.name}" + (f" ({f.vendor})" if f.vendor else ""),
                basis="per_lot", low=cost, high=cost,
                formula=f"{_money(f.lot_charge, cur)} lot + {qty} × {_money(f.per_part, cur)}",
                evidence=[Evidence(source="rate_card", ref=f"rate_card.finishes[{f.id}]", detail=f"lot {f.lot_charge}, per part {f.per_part}, {f.lead_days} days")],
            ))

        sub_low = sum(l.low for l in lines)
        sub_high = sum(l.high for l in lines)
        m = rc.margin_pct / 100
        lines.append(QuoteLine(
            id=f"{p}.margin", category="margin", label=f"Margin ({rc.margin_pct:g}%)", basis="per_lot",
            low=sub_low * m, high=sub_high * m,
            formula=f"{rc.margin_pct:g}% × subtotal ({_money(sub_low, cur)}–{_money(sub_high, cur)})",
            evidence=[Evidence(source="rate_card", ref="rate_card.margin_pct", detail=f"{rc.margin_pct}%")],
        ))
        total_low, total_high = sub_low * (1 + m), sub_high * (1 + m)
        if total_low < rc.min_order_charge:
            top_low = rc.min_order_charge - total_low
            top_high = max(0.0, rc.min_order_charge - total_high)
            lines.append(QuoteLine(
                id=f"{p}.minimum", category="minimum", label="Minimum order top-up", basis="per_lot",
                low=top_low, high=top_high,
                formula=f"{_money(rc.min_order_charge, cur)} minimum − computed total",
                evidence=[Evidence(source="rate_card", ref="rate_card.min_order_charge", detail=_money(rc.min_order_charge, cur))],
            ))
            total_low, total_high = total_low + top_low, total_high + top_high

        machine_hours_high = (cut_min * cal.high_factor * qty) / 60 + (setups + 1) * lab.setup_hours_per_setup
        lead = rc.base_lead_days + finish_lead + math.ceil(machine_hours_high / rc.shop_hours_per_day)

        for l in lines:
            l.low, l.high = round(l.low, 2), round(l.high, 2)
        quantities.append(QuantityQuote(
            quantity=qty, lines=lines,
            total_low=round(total_low, 2), total_high=round(total_high, 2),
            unit_low=round(total_low / qty, 2), unit_high=round(total_high / qty, 2),
            lead_days=lead,
        ))

    assumptions.append(f"Lead time = {rc.base_lead_days} base days + longest finish lead + machine hours (high case) ÷ {rc.shop_hours_per_day:g} h/day.")
    if inp.drawing.status != "read":
        assumptions.append("Drawing was not read automatically; tolerances and notes come only from what the shop entered.")
    if any(f.severity == "critical" for f in inp.dfm.flags):
        assumptions.append("Critical DFM flags are open. Prices assume the part is made as modelled; resolve the flags before sending.")

    return QuoteDraft(status="draft", currency=cur, quantities=quantities, machine_id=machine.id,
                      assumptions=assumptions, open_questions=[])

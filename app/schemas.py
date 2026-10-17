"""Structured contracts between pipeline stages.

Every stage takes one of these models as input and returns one as output. The
runner validates each output before the next stage sees it, so a malformed
result stops the pipeline instead of flowing downstream.

Rule: stages that produce numbers are deterministic code. LLM-backed steps
(drawing extraction, explanations) may only cite numbers computed here.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Shared building blocks
# --------------------------------------------------------------------------

EvidenceSource = Literal["geometry", "drawing", "rate_card", "intake", "assumption"]


class Evidence(BaseModel):
    """Where a number or claim came from, so a human can check it."""

    source: EvidenceSource
    ref: str  # e.g. "rate_card.machines[vf2].hourly_rate", "geometry.volume_mm3"
    detail: str  # human-readable value, e.g. "$95.00/h"


class OpenQuestion(BaseModel):
    """Something we refuse to guess. Blocking questions stop approval."""

    id: str
    field: str
    question: str
    ask: Literal["shop", "customer"] = "shop"
    blocking: bool = True


# --------------------------------------------------------------------------
# Intake
# --------------------------------------------------------------------------

FileKind = Literal["step", "pdf", "iges", "parasolid", "solidworks", "dxf", "image", "unknown"]
Process = Literal["cnc_mill_3axis"]  # Phase 1 is deliberately one process.


class UploadedFile(BaseModel):
    id: str
    original_name: str
    path: str
    sha256: str
    size_bytes: int


class IdentifiedFile(BaseModel):
    file_id: str
    original_name: str
    kind: FileKind
    role: Literal["model", "drawing", "other"]
    supported: bool
    note: str | None = None


class JobSpec(BaseModel):
    """What the shop user told us. None means 'not stated' — never defaulted."""

    process: Process = "cnc_mill_3axis"
    material_id: str | None = None
    quantities: list[int] = Field(default_factory=list)
    finish_ids: list[str] = Field(default_factory=list)
    deadline: date | None = None
    tightest_tolerance_mm: float | None = None  # shop-entered band until drawing extraction lands
    customer_notes: str | None = None


class IntakeInput(BaseModel):
    files: list[UploadedFile]
    spec: JobSpec
    known_material_ids: list[str]
    known_finish_ids: list[str]


class IntakeResult(BaseModel):
    files: list[IdentifiedFile]
    spec: JobSpec
    model_file_id: str | None
    drawing_file_id: str | None
    open_questions: list[OpenQuestion]


# --------------------------------------------------------------------------
# Geometry (deterministic, from STEP)
# --------------------------------------------------------------------------


class Vec3(BaseModel):
    x: float
    y: float
    z: float


class HoleCandidate(BaseModel):
    """A full-revolution concave cylinder. A candidate, not a recognised feature."""

    diameter_mm: float
    depth_mm: float
    axis: Vec3
    center: Vec3


class ConcaveRadius(BaseModel):
    """A partial concave cylinder, typically an internal fillet / pocket corner."""

    radius_mm: float
    axis: Vec3


class SharpInternalEdge(BaseModel):
    """A concave straight edge between two planar faces (no fillet)."""

    length_mm: float
    direction: Vec3
    midpoint: Vec3


class WallThickness(BaseModel):
    method: Literal["ray_sampling"] = "ray_sampling"
    samples: int
    min_mm: float | None
    p05_mm: float | None
    note: str = "Estimate from inward rays at sampled face points; may miss walls between samples."


class GeometrySummary(BaseModel):
    units: Literal["mm"] = "mm"
    solid_count: int
    bbox_mm: Vec3  # axis-aligned extents in the file's coordinate system
    volume_mm3: float
    surface_area_mm2: float
    face_type_counts: dict[str, int]
    hole_candidates: list[HoleCandidate]
    concave_radii: list[ConcaveRadius]
    sharp_internal_edges: list[SharpInternalEdge]
    wall_thickness: WallThickness
    warnings: list[str] = Field(default_factory=list)

    @property
    def feature_count(self) -> int:
        return len(self.hole_candidates) + len(self.concave_radii) + len(self.sharp_internal_edges)


# --------------------------------------------------------------------------
# Drawing extraction (LLM-backed later; stubbed now)
# --------------------------------------------------------------------------


class ToleranceCallout(BaseModel):
    text: str  # as read, e.g. "Ø10 H7" or "25.00 ±0.02"
    band_mm: float  # total tolerance band, e.g. 0.04 for ±0.02
    linked_feature: str | None = None  # None = could not tie it to model geometry
    confidence: float = Field(ge=0, le=1)


class DrawingExtraction(BaseModel):
    status: Literal["read", "not_implemented", "failed", "no_drawing"]
    material_text: str | None = None
    finish_text: str | None = None
    general_tolerance_mm: float | None = None
    tolerances: list[ToleranceCallout] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Rate card (per shop — nothing generic baked in)
# --------------------------------------------------------------------------


class Machine(BaseModel):
    id: str
    name: str
    axes: int = 3
    envelope_mm: Vec3
    hourly_rate: float
    standard_tolerance_mm: float  # band held without special effort
    best_tolerance_mm: float  # tightest band the shop will commit to


class Material(BaseModel):
    id: str
    name: str
    density_g_cm3: float
    price_per_kg: float
    stock_allowance_mm: float = 3.0  # added to each bbox dimension
    plate_thicknesses_mm: list[float] = Field(default_factory=list)  # empty = cut to size
    roughing_cm3_per_min: float  # shop's typical removal rate in this material
    finishing_cm2_per_min: float  # shop's typical finishing coverage rate
    min_wall_mm: float


class Finish(BaseModel):
    id: str
    name: str
    vendor: str | None = None
    lot_charge: float = 0.0
    per_part: float = 0.0
    lead_days: int = 0


class Labor(BaseModel):
    setup_hours_per_setup: float
    programming_hours_base: float
    programming_hours_per_feature: float
    inspection_minutes_per_part: float
    inspection_hourly_rate: float
    tight_tolerance_inspection_factor: float = 1.5


class TimeCalibration(BaseModel):
    """Multipliers on estimated machine time. Tuned from the shop's own history."""

    multiplier: float = 1.0
    low_factor: float = 0.8
    high_factor: float = 1.35


class DfmThresholds(BaseModel):
    min_tool_diameter_mm: float = 1.0
    hole_depth_ratio_warn: float = 4.0
    hole_depth_ratio_critical: float = 10.0


class RateCard(BaseModel):
    shop_name: str
    currency: str = "USD"
    machines: list[Machine]
    materials: list[Material]
    finishes: list[Finish] = Field(default_factory=list)
    labor: Labor
    margin_pct: float
    min_order_charge: float = 0.0
    base_lead_days: int = 5
    shop_hours_per_day: float = 8.0
    calibration: TimeCalibration = Field(default_factory=TimeCalibration)
    dfm: DfmThresholds = Field(default_factory=DfmThresholds)

    def material(self, material_id: str | None) -> Material | None:
        return next((m for m in self.materials if m.id == material_id), None)

    def finish(self, finish_id: str) -> Finish | None:
        return next((f for f in self.finishes if f.id == finish_id), None)


# --------------------------------------------------------------------------
# DFM
# --------------------------------------------------------------------------

Severity = Literal["info", "warn", "critical"]


class DfmInput(BaseModel):
    geometry: GeometrySummary
    drawing: DrawingExtraction
    spec: JobSpec
    rate_card: RateCard


class DfmFlag(BaseModel):
    id: str
    rule: str
    severity: Severity
    title: str
    measured: str
    threshold: str
    reason: str  # deterministic, template-built
    suggested_fix: str
    evidence: list[Evidence]
    explanation: str | None = None  # optional LLM rewording; never adds numbers


class DfmReport(BaseModel):
    flags: list[DfmFlag]
    checks_run: list[str]
    checks_skipped: dict[str, str]  # check -> why it did not run


# --------------------------------------------------------------------------
# Quote
# --------------------------------------------------------------------------


class QuoteInput(BaseModel):
    geometry: GeometrySummary
    drawing: DrawingExtraction
    spec: JobSpec
    dfm: DfmReport
    rate_card: RateCard


class QuoteLine(BaseModel):
    id: str  # stable within a quote, e.g. "q10.machining"
    category: Literal["material", "setup", "programming", "machining", "inspection", "finishing", "margin", "minimum"]
    label: str
    basis: Literal["per_part", "per_lot"]
    low: float  # lot total, in rate card currency
    high: float
    formula: str  # e.g. "2 setups × 0.75 h × $95.00/h"
    evidence: list[Evidence]
    assumptions: list[str] = Field(default_factory=list)


class QuantityQuote(BaseModel):
    quantity: int
    lines: list[QuoteLine]
    total_low: float
    total_high: float
    unit_low: float
    unit_high: float
    lead_days: int


class QuoteDraft(BaseModel):
    status: Literal["draft", "blocked"]
    currency: str
    quantities: list[QuantityQuote]
    machine_id: str | None
    assumptions: list[str]
    open_questions: list[OpenQuestion]

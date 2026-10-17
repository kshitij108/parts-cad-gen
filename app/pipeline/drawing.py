"""Drawing extraction: PDF -> tolerances, material, finish, notes.

This is the one stage that will be LLM/vision-backed. It sits behind the
`DrawingExtractor` protocol so the real implementation can be dropped in
without touching the runner. Until then, the stub refuses to guess: it returns
`not_implemented` plus questions the shop must answer by reading the drawing.

Contract for the real implementation:
- Return only what is on the drawing. Every callout gets a confidence.
- A tolerance that can't be tied to a model feature stays `linked_feature=None`.
- Never send customer files to a provider without zero-data-retention terms.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.schemas import DrawingExtraction, OpenQuestion


class DrawingExtractor(Protocol):
    def extract(self, pdf_path: Path) -> DrawingExtraction: ...


class StubExtractor:
    def extract(self, pdf_path: Path) -> DrawingExtraction:
        return DrawingExtraction(
            status="not_implemented",
            open_questions=[
                OpenQuestion(id="drawing.tightest_tolerance", field="tolerance", blocking=False,
                             question="Drawing not read automatically yet. What is the tightest tolerance band on the drawing (mm)?"),
                OpenQuestion(id="drawing.material_check", field="material_id", blocking=False,
                             question="Confirm the material on the drawing title block matches the job material."),
                OpenQuestion(id="drawing.finish_check", field="finish_ids", blocking=False,
                             question="Confirm finish/plating notes on the drawing match the job finishes."),
            ],
        )


def run(pdf_path: Path | None, extractor: DrawingExtractor | None = None) -> DrawingExtraction:
    if pdf_path is None:
        return DrawingExtraction(status="no_drawing")
    try:
        return (extractor or StubExtractor()).extract(pdf_path)
    except Exception as e:  # extraction must never take the pipeline down
        return DrawingExtraction(
            status="failed",
            open_questions=[OpenQuestion(id="drawing.failed", field="drawing", blocking=False,
                                         question=f"Drawing could not be read ({type(e).__name__}). Review it manually.")],
        )

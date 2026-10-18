# Phase 2: building models from sketches, photos and drawings

The rule from the spec drives the whole design: **a pretty model with wrong dimensions is worse than none.** So the model never grades its own work. Generation is split into steps, and the step that decides whether a model is right is plain code that never sees how the plan was written.

## The four steps

1. **Read.** Claude reads the inputs and writes a *dimension spec*: every dimension the part must meet, as written on the input, with a confidence for each. Missing or unreadable values become open questions; none are guessed. A photo with no dimensions and no reference object gets no dimensions at all.
2. **Confirm.** A person edits and confirms the spec on the build page. A deterministic gate refuses confirmation if any of these hold:
   - there is no scale
   - an overall size is missing
   - a dimension can't be measured as written
   - a blocking question is still open
3. **Plan and build.** Claude (or a person) writes a *build plan* in a small feature language: stock, then holes, pockets, bosses and box cuts. A deterministic builder turns it into an OpenCascade solid. No free-form code is ever executed.
4. **Verify.** `verify.py` measures the solid against the confirmed spec. A model passes only if all of the following hold:
   - every dimension is within **0.01 mm** of nominal (the model is ideal geometry, so drawing tolerances go to DFM and quoting, not to this check)
   - it is one valid, closed solid that survives a STEP round trip
   - every hole the plan drills survives as a complete hole
   - no hole exists that the confirmed dimensions don't mention

   If verification fails, the measured failures (for example "D11 hole_position: expected 10.000, measured 10.500") are sent back to the planner for the next attempt. Failed models get a 3D preview for debugging, but no STEP file or drawing.

A person then accepts the verified model. Only then can it be sent to check + quote. A DFM flag that has an automatic fix (sharp inside corners) can be applied from the quote job: it edits the plan, adds the new radius to the spec, rebuilds, re-verifies, and asks for acceptance again.

## How dimensions are measured

Each dimension has a *kind* that tells the checker how to measure it. All coordinates are mm from the part's minimum corner.

| Kind | Measured as |
|---|---|
| `overall` | Bounding-box extent along an axis. |
| `hole_diameter`, `hole_position`, `hole_depth`, `hole_count` | The hole whose axis passes nearest the given point. Counterbores are told apart by diameter. A hole is "through" if both of its ends are open. |
| `depth` | From the part's bounding face to the first material along a line, e.g. a pocket floor. |
| `material_width` / `gap_width` | Length of solid material or empty space along an axis through a point: walls, edge distances, pocket and slot sizes. |
| `radius` | The nearest cylindrical face with the given axis: fillets, corner radii, bosses. |

The checker reports how it measured each value. A wrongly placed probe point fails with a readable reason ("the point is in empty space, not material"), so the person can correct the spec.

## Testing generation

Three layers, from cheapest to most realistic:

1. **Unit and flow tests** (`pytest`, offline, about 25 s):
   - Builder volumes match hand calculations for every operation.
   - Each of 8 reference parts passes its own ground truth.
   - **All 44 deliberate errors are caught.** Each reference part is tried with every applicable mistake: a hole moved 0.5 mm, a diameter 0.2 mm small, a through hole made blind, pockets deeper or longer, a radius changed, a step deeper, stock larger.
   - Web flows run end to end with offline stand-ins for Claude.
2. **Benchmark harness** (`python -m app.build.bench`). It renders each reference part as a drawing, a hand-sketch-style drawing, or a bare picture. It hands the agent only that image, then scores the result against the reference, not against the agent's own reading.
   - `oracle`: correct spec and plan. Must score 100%; this proves the harness.
   - `mutant`: one deliberate error per part. Every error must be caught.
   - `live-plan`: correct spec, Claude plans. Measures planning alone.
   - `live`: Claude reads and plans. End to end.

   The headline failure is **confident wrong**: a model that passes its own check but fails the reference, meaning the drawing was misread. Each report also shows IoU (shared volume ÷ combined volume) and how many reference dimensions the extractor recovered.
3. **Your own parts.** Upload a sketch or drawing on the Builds page. To add a reference case, add a plan (and any extra dimensions) to `app/build/bench/cases.py`. The oracle test fails loudly if an extra dimension doesn't hold on the reference model.

## Limits

- Prismatic 3-axis parts only. No revolved/turned features, threads, arbitrary edge fillets or chamfers, or angled holes yet.
- Depths are measured from the part's bounding face. A pocket on a lower step is dimensioned as the depth from the top, plus the step.
- Verification checks the confirmed dimensions, not intent. Features no dimension covers are listed under "Check by eye", and acceptance stays a human decision.

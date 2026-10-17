# Phase 1 scope decisions

These are changes to the original spec, made after reviewing it. The original goals stand: check and quote parts from files people already have, show reasoning, flag instead of guessing, get human sign-off, use per-shop pricing, and keep a full trail.

## Decisions

| Area | Decision | Why |
|---|---|---|
| Process | **3-axis CNC milling only.** Sheet metal and 3D printing come later. | Each process needs its own costing model and DFM rules. Doing one well beats doing three badly. |
| Who computes numbers | **Deterministic code** computes geometry, DFM rules and quote maths. LLMs only read drawings, choose questions and reword explanations. They never produce a number. | "Every number traces to a rate-card entry" can only be guaranteed this way. |
| Feature recognition | Candidates only: hole candidates (full concave cylinders), internal radii (partial concave cylinders), sharp internal edges (concave plane/plane edges). Pockets and undercuts are listed as **checks not run**. | Recognising features from a solid model is a hard problem. A clean report must never be mistaken for a clean part. |
| Quote ranges | Each line has a low and high value. Time uncertainty comes from the shop's `calibration` factors. The reviewer chooses the final amount. | Ranges avoid presenting one confident wrong number. Calibration belongs to the shop and can be tuned from its own history. |
| Rate card | Per machine: envelope, rate, standard and best tolerance. Per material: price/kg, density, plate stock, removal and finishing rates, minimum wall. Finishes have a lot charge, per-part cost and lead time. Also: labour, margin, minimum order charge, quantity breaks, DFM thresholds. | The quote can only be as good as the rate card. |
| STEP vs drawing | The drawing governs tolerances and notes; the model governs geometry. Conflicts become open questions. A tolerance not tied to a model feature stays `linked_feature=None`. | Neither file silently overrides the other. |
| Who gets questions | The **shop user**. Each question is tagged `ask: shop` or `ask: customer` so the shop knows which to forward. | In the shop flow, the customer has already sent their request for quote. |
| Unsupported files | IGES, Parasolid, SolidWorks, DXF and images are detected and rejected with a clear message asking for STEP. | Fails loudly instead of misreading the file. |
| Export | Quote as CSV. Approved package as JSON. ERP integration (ProShop, JobBOSS, E2) comes later. | A quote that has to be retyped loses the time saved. |

## Confidentiality

- Originals are stored under random names in a private per-job folder, are never served statically, and are deleted with the job.
- No customer file goes to a model provider unless that provider has zero-data-retention terms. The drawing extractor is stubbed until this is in place.
- **ITAR/EAR-controlled drawings are out of scope** until a compliant hosting path exists. Shops must not upload them.
- Per-shop calibration from a shop's own history is allowed. Data never crosses between shops.

## Phase 1 success criteria (revised)

1. A shop uploads STEP (+ PDF) and gets a reviewable DFM report and draft quote in minutes.
2. Every quote line shows its formula and the rate-card or geometry values behind it.
3. A human approves, edits (with a reason) or rejects each line before the package is frozen.
4. **Accuracy:** on N of the shop's past jobs, the draft range contains the shop's actual quote at least X% of the time. N and X are agreed with the pilot shop.
5. **Noise:** a reviewing machinist judges DFM false positives acceptable.

## Competitive note

Paperless Parts sells close to this to job shops. Xometry, Protolabs and aPriori overlap in parts. The intended differentiators are reasoning a reviewer can check on every line, calibration to the shop's own pricing history, and (Phase 3) the designer-to-shop handoff.

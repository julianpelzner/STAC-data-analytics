# Decision Records (ADRs)

This folder holds **Architecture Decision Records** — one short file per
meaningful decision, capturing *why* it was made and what was rejected.

The README, [`data_model.md`](../data_model.md), and [`ARCHITECTURE.md`](../ARCHITECTURE.md)
describe **what** this project is and how it currently works. These ADRs capture
the **why** behind the non-obvious calls — the context, the alternatives, and
the trade-offs — so the reasoning survives after the moment has passed. That
reasoning is exactly what lets me defend a design decision later without having
to reverse-engineer it from the code.

---

## How to add one

1. Copy [`0000-adr-template.md`](0000-adr-template.md) to the next number:
   `NNNN-short-slug.md` (zero-padded, e.g. `0004-grain-per-item-region.md`).
2. Fill it in — **Context** and **Alternatives considered** matter most; those
   are the parts that don't survive in code or git history.
3. Add a row to the [Index](#index) below.
4. Keep it short. An ADR is a page, not an essay. One decision per file.

**Write an ADR when** a choice has real alternatives and future-you would ask
"why did I do it this way?" — tooling picks, grain/schema contracts, cost
guardrails, auth models, scope cuts. **Skip it** for reversible, obvious, or
purely mechanical choices.

## Conventions

- **Numbers** are assigned once and never reused, even if an ADR is later
  deprecated. They order by *when decided*, not by topic.
- **Status** moves forward only: `Proposed → Accepted`, and later
  `Superseded by ADR-NNNN` or `Deprecated`. Don't delete or rewrite a
  superseded ADR — supersede it with a new one and link the two, so the history
  of the thinking stays intact.
- ADRs are **immutable once accepted**, apart from a status change or a dated
  note appended at the bottom.

---

## Index

| # | Title | Status | Relates to |
|---|---|---|---|
| [0001](0001-metadata-only-ingestion.md) | Pull STAC metadata only, not imagery | Accepted | Q1, Q3, ingestion |

---

## Backlog — decisions worth capturing

Decisions already baked into the repo that would be good to write up (so this
isn't starting from a blank page). Pulled from the README, `CLAUDE.md`, and
`data_model.md` — delete a row once it has an ADR, or drop it if it doesn't
warrant one:

- ~~**Metadata-only STAC ingestion**~~ — captured in [ADR-0001](0001-metadata-only-ingestion.md).
- **Grain = one row per `(item_id, region)`** — the same scene can appear under
  multiple AOIs on purpose. A schema contract worth justifying.
- **Synthetic demand + real supply split** — the generator never decides order
  success; success/fulfillment come from real satellite conditions. The honesty
  backbone of the case studies (see `data_model.md` §1).
- **ADC auth, never a key file** — `gcloud auth application-default login`
  rather than a service-account key. Security posture decision.
- **Committing a stratified ~500-row sample** (`data/raw_sample/`) while
  gitignoring full pulls — reproducibility-without-credentials vs. repo size.
- **Omni + Looker Studio as the BI layer** — a trial tool plus a permanent
  public link. Trade-off between capability and a durable portfolio artifact.
- **Per-layer testing** (pytest for ingestion, dbt tests for models, CI on
  every push) rather than validating only in the BI layer at the end.

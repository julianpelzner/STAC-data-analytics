# ADR NNNN: <short decision title, in the imperative — e.g. "Pull metadata only, not imagery">

- **Status:** Proposed <!-- Proposed | Accepted | Superseded by [ADR-NNNN](NNNN-...md) | Deprecated -->
- **Date:** YYYY-MM-DD
- **Deciders:** <who made the call — for a solo project, just you>
- **Relates to:** <business question(s), other ADRs, or docs this touches — e.g. Q1, [ADR-0003](0003-...md), [data_model.md](../data_model.md)>

---

## Context

What's the situation that forces a decision? State the problem, the constraints
(cost, free-tier limits, time, skills being demonstrated), and any facts that
were true *at the time*. Write enough that future-you can reconstruct the
pressure without re-reading the whole repo. This is the single most valuable
section — it's the part that evaporates from memory.

## Decision

What did you decide to do? One or two sentences, stated plainly and in the
present tense ("We land raw metadata in GCS before loading to BigQuery").

## Alternatives considered

The options you rejected, and *why* — this is what makes the ADR worth keeping.

| Option | Why not |
|---|---|
| <alternative A> | <the reason it lost> |
| <alternative B> | <the reason it lost> |

## Consequences

What does this decision buy you, and what does it cost or rule out? Include the
bad parts honestly — the follow-on work created, the flexibility given up, the
thing you'll have to revisit later.

- **Positive:** <...>
- **Negative / trade-off:** <...>
- **Follow-ups:** <any new work this creates — link issues/ADRs if they exist>

## Notes

Optional: links, references, benchmarks, a snippet, or a dated update if the
decision's status later changes (add a line rather than rewriting history).

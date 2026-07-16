# Technical decisions

The canonical selected decisions live under
[`../.untaped/orchestration/`](../.untaped/orchestration/). Agents start with:

```sh
untaped-orchestration brief --format json
```

The committed [decision view](../.untaped/orchestration/views/decisions.md) is
generated human output, not canonical tool input. The store contains only six
selected tool-architecture decisions. The complete
[v1 design](superpowers/specs/2026-07-09-orchestration-v1-design.md) remains the
normative authority for every non-selected section.

Exact selection provenance is recorded in
[`orchestration-adoption/decision-sources.toml`](orchestration-adoption/decision-sources.toml).
Independent review evidence will be committed as
`orchestration-adoption/review.md` after acceptance.

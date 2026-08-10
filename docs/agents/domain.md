# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- **`docs/decisions/README.md`** — the ADR index. Then read the ADRs that touch the area you're about to work in.
- **`docs/index.md`** — the doc-set entry point, with a task→doc table.
- **`CLAUDE.md`** at the repo root — project vocabulary and the standing constraints.

There is **no root `CONTEXT.md`** and **no `docs/adr/`** directory in this repo, and none is planned. Domain vocabulary lives in `CLAUDE.md` and `docs/index.md`; decisions live in `docs/decisions/`. Do not treat the absence of a `CONTEXT.md` as a gap to fill.

## File structure

```
/
├── CLAUDE.md                  — project instructions + vocabulary
└── docs/
    ├── index.md               — doc-set entry point
    └── decisions/
        ├── README.md          — ADR index (status per ADR)
        ├── 001-*.md           — three-digit numbering
        └── … through 016-*.md
```

## Use the project's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as it appears in `CLAUDE.md`, `docs/index.md`, and the ADRs. Don't drift to synonyms — several terms here are load-bearing and a near-miss changes the meaning (e.g. `mask_dice` vs the dead `dice` field; `nmi_gain` vs `nmi`; "minimally preprocessed" vs "raw DICOM" input).

If the concept you need isn't documented anywhere, that's a signal — either you're inventing language the project doesn't use (reconsider) or there's a real gap (say so).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR 007 (…) — but worth reopening because…_

Cite ADRs as "ADR 007" (space, three digits), matching `docs/decisions/README.md`. Check the ADR's **Status** column before relying on it — some are `Proposed` and describe infrastructure that was never built (e.g. ADR 014's Cloudflare tunnel).

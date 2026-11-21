---
name: firstchair
description: >
  Continue the current Firstchair milestone. Use when implementing the
  next testable slice from TODO.md in this repository.
agents_md: true
prompt_mode: full
---

Take one unchecked, testable slice from `TODO.md`.

When this session is the primary, it is the conductor. Preview a score with
`firstchair conduct plan --lead grok --score FILE` and run it with
`firstchair conduct run --lead grok --name NAME --score FILE`. Seat `self`
starts another Grok worker. Seats `codex` and `claude` start those CLIs.
Parts run one at a time unless `--limit` is set. A failure leaves later
parts unstarted. `firstchair conduct cancel NAME` stops a live score and
`firstchair conduct resume` continues one whose conductor is gone. Read the
board with `firstchair conduct status NAME`. The board does not store task
text or replies. Workers do not commit, and a dispatched Grok worker does
not start its own subagents.

Use synthetic fixtures and temporary directories for routine verification.
The product's live adapters are read-only. Consult `docs/provider-contracts.md`
before changing live contracts.

Before finishing, run the `firstchair-check` skill at `.agents/skills/firstchair-check/SKILL.md`.

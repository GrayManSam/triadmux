---
name: provider-fixture
description: Add or review an offline Firstchair provider fixture or parser. Use when introducing or checking an invented fixture, a fail-closed parser, or a parser diff.
---

# Provider fixture

1. Pin the provider version on the parser. Accept only that version.
2. Decode provider output with `strict_json.load_json` in `src/firstchair/strict_json.py`.
3. Raise `ProviderError` with a code already used by `src/firstchair/claude_json.py` or `src/firstchair/codex_jsonl.py`. Do not add a code.
4. Use invented fixtures only. Do not copy transcripts, account data, or credentials.
5. Keep fixture verification offline. The product has read-only live adapters.
   Verify changed parser fields against `docs/provider-contracts.md` before
   wiring them into a live workflow.
6. Run the existing check skill at `.agents/skills/firstchair-check/SKILL.md`.

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY = "docs/engineering/agent-policy.md"
LEGACY_CLAIMS = (
    "worktrees/triad-mux/",
    "live adapters stay disabled",
    "live providers disabled",
    "live adapter is still disabled",
)


def stale_claim(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    return any(claim in normalized for claim in LEGACY_CLAIMS)


class AgentPolicyTests(unittest.TestCase):
    def test_policy_is_reachable_and_has_all_rules(self):
        self.assertIn(f"({POLICY})", (ROOT / "AGENTS.md").read_text())
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text().splitlines())
        rules = re.findall(r"^\*\*R-(\d{3})\*\*", (ROOT / POLICY).read_text(), re.MULTILINE)
        self.assertEqual(rules, [f"{number:03}" for number in range(1, 31)])

    def test_stale_claim_detection(self):
        for claim in LEGACY_CLAIMS:
            with self.subTest(claim=claim):
                self.assertTrue(stale_claim(claim))
        self.assertTrue(stale_claim("live adapters\nstay disabled"))
        self.assertFalse(stale_claim("The product uses read-only live adapters."))

    def test_active_instructions_have_no_stale_claims(self):
        active = (
            "AGENTS.md",
            "CLAUDE.md",
            ".claude/skills/orchestrate/SKILL.md",
            ".grok/agents/firstchair.md",
            ".grok/plugins/firstchair/agents/contract-reviewer.md",
            ".grok/plugins/firstchair/skills/provider-fixture/SKILL.md",
        )
        for path in active:
            with self.subTest(path=path):
                self.assertFalse(stale_claim((ROOT / path).read_text()), path)

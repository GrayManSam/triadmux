"""Account rotation: which signed-in account runs the next stage, and why."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from firstchair.live import LiveSession
from firstchair.provider import ProviderError
from firstchair.quota import Quota, Window, parse_claude_stream, parse_codex_rate_limits, read_record
from firstchair.rotation import (
    Policy, Rotation, account_key, clear_cooldowns, rank, read_ledger, read_policy, record, standing,
    store_quota,
)
from firstchair.usage import Reply, Usage


class Seat:
    """An invented account that replies from a script and never starts a process."""

    def __init__(self, provider, profile=None, replies=()):
        self.provider, self.profile, self.version = provider, profile, "1.0"
        self.workspace = None
        self.replies, self.prompts = list(replies), []

    def ask(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def measured(text, tokens):
    return Reply(text, Usage("measured", tokens, 0))


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="firstchair-rotation-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.ledger = self.temp / "account-ledger.json"
        self.now = 1_000_000.0

    def clock(self):
        return self.now

    def order(self, seats, policy, preferred="codex", sticky=None):
        usable, report = rank(seats, preferred, sticky or {}, read_ledger(self.ledger), policy, self.now)
        return [account_key(seat.provider, seat.profile) for seat in usable], report

    def test_the_least_used_ready_account_of_the_preferred_provider_goes_first(self):
        policy = Policy()
        seats = [Seat("claude", "x"), Seat("codex", "a"), Seat("codex", "b"), Seat("codex", "c")]
        record(self.ledger, "codex:a", policy, now=self.now - 10, usage=Usage("measured", 900, 100))
        record(self.ledger, "codex:b", policy, now=self.now - 20, usage=Usage("measured", 10, 5))
        record(self.ledger, "claude:x", policy, now=self.now - 5, usage=Usage("measured", 1, 1))
        names, report = self.order(seats, policy)
        self.assertEqual(names, ["codex:c", "codex:b", "codex:a", "claude:x"])
        self.assertEqual({item["account"]: item["rank"] for item in report},
                         {"codex:c": 1, "codex:b": 2, "codex:a": 3, "claude:x": 4})
        self.assertEqual(report[1]["tokens"], 1000)
        # Another role prefers its own provider.
        self.assertEqual(self.order(seats, policy, preferred="claude")[0][0], "claude:x")

    def test_the_account_in_use_stays_until_it_reaches_its_budget(self):
        policy = Policy(token_limit=1000)
        seats = [Seat("codex", "a"), Seat("codex", "b")]
        record(self.ledger, "codex:a", policy, now=self.now - 10, usage=Usage("measured", 600, 0))
        names, _ = self.order(seats, policy, sticky={"codex": "codex:a"})
        self.assertEqual(names, ["codex:a", "codex:b"])
        record(self.ledger, "codex:a", policy, now=self.now - 5, usage=Usage("measured", 400, 0))
        names, report = self.order(seats, policy, sticky={"codex": "codex:a"})
        self.assertEqual(names, ["codex:b", "codex:a"])
        self.assertEqual(report[0]["status"], "over_budget")
        # Every account over budget: the least used one still runs the stage.
        record(self.ledger, "codex:b", policy, now=self.now - 1, usage=Usage("measured", 5000, 0))
        self.assertEqual(self.order(seats, policy)[0], ["codex:a", "codex:b"])

    def test_call_limit_covers_accounts_whose_tokens_are_not_measured(self):
        policy = Policy(call_limit=2)
        seats = [Seat("grok", "a"), Seat("grok", "b")]
        for _ in range(2):
            record(self.ledger, "grok:a", policy, now=self.now - 1, usage=Usage())
        names, report = self.order(seats, policy, preferred="grok", sticky={"grok": "grok:a"})
        self.assertEqual(names, ["grok:b", "grok:a"])
        self.assertEqual((report[0]["calls"], report[0]["unmeasured_calls"], report[0]["tokens"]), (2, 2, 0))

    def test_usage_failure_rests_an_account_until_the_cooldown_ends(self):
        policy = Policy(cooldown_seconds=600)
        seats = [Seat("codex", "a"), Seat("codex", "b")]
        record(self.ledger, "codex:a", policy, now=self.now, error="rate_limited")
        names, report = self.order(seats, policy)
        self.assertEqual(names, ["codex:b"])
        self.assertEqual((report[0]["status"], report[0]["cooldown_seconds"], report[0]["last_error"]),
                         ("cooling_down", 600, "rate_limited"))
        self.assertNotIn("rank", report[0])
        self.now += 601
        self.assertEqual(self.order(seats, policy)[0], ["codex:a", "codex:b"])
        # A failure that is not about usage does not rest the account.
        record(self.ledger, "codex:b", policy, now=self.now, error="authorization_failed")
        self.assertEqual(standing("codex:b", read_ledger(self.ledger), policy, self.now)["status"], "ready")
        # A success ends a cooldown early, and clearing is explicit.
        record(self.ledger, "codex:b", policy, now=self.now, error="provider_timeout")
        record(self.ledger, "codex:a", policy, now=self.now, error="rate_limited")
        self.assertEqual(clear_cooldowns(self.ledger, ["codex:a"]), ["codex:a"])
        self.assertEqual(self.order(seats, policy)[0], ["codex:a"])
        record(self.ledger, "codex:b", policy, now=self.now, usage=Usage())
        self.assertEqual(self.order(seats, policy)[0], ["codex:a", "codex:b"])
        self.assertEqual(clear_cooldowns(self.ledger), [])

    def test_usage_leaves_the_window(self):
        policy = Policy(window_seconds=100, token_limit=50)
        record(self.ledger, "codex", policy, now=self.now, usage=Usage("measured", 60, 0))
        self.assertEqual(standing("codex", read_ledger(self.ledger), policy, self.now + 50)["status"], "over_budget")
        self.assertEqual(standing("codex", read_ledger(self.ledger), policy, self.now + 101)["tokens"], 0)
        record(self.ledger, "codex", policy, now=self.now + 101, usage=Usage("measured", 1, 0))
        self.assertEqual(len(read_ledger(self.ledger)["codex"]["events"]), 1)

    def test_a_ledger_that_cannot_be_trusted_is_refused(self):
        for label, payload in (
            ("not json", "{"), ("list", "[]"), ("version", '{"schema_version": 2, "accounts": {}}'),
            ("entry", '{"schema_version": 1, "accounts": {"codex": []}}'),
            ("event", '{"schema_version": 1, "accounts": {"codex": {"events": [[1, -5]]}}}'),
            ("bool", '{"schema_version": 1, "accounts": {"codex": {"events": [[1, true]]}}}'),
            ("cooldown", '{"schema_version": 1, "accounts": {"codex": {"events": [], "cooldown_until": "soon"}}}'),
        ):
            with self.subTest(label=label):
                self.ledger.write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "invalid account ledger"):
                    read_ledger(self.ledger)
                with self.assertRaisesRegex(ValueError, "invalid account ledger"):
                    record(self.ledger, "codex", Policy(), now=self.now, usage=Usage())
                self.assertEqual(self.ledger.read_text(encoding="utf-8"), payload)

    def test_policy_rejects_values_that_would_never_or_always_switch_by_accident(self):
        for bad in (
            {"token_limit": 0}, {"token_limit": True}, {"call_limit": -1}, {"call_limit": 1.5},
            {"window_seconds": 0}, {"window_seconds": float("inf")}, {"cooldown_seconds": float("nan")},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Policy(**bad)
        policy = Policy(token_limit=10, call_limit=3, window_seconds=60, cooldown_seconds=30)
        self.assertEqual(read_policy(json.loads(json.dumps(policy.as_record()))), policy)
        for bad in (None, {}, {**policy.as_record(), "extra": 1}, {**policy.as_record(), "token_limit": "10"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                read_policy(bad)

    def test_a_run_moves_to_the_next_account_before_the_first_is_refused(self):
        policy = Policy(token_limit=100)
        first = Seat("codex", "a", [measured("plan", 150)])
        second = Seat("codex", "b", [measured("review", 150)])
        third = Seat("codex", "c", [measured("result", 10)])
        session = LiveSession(first, first, (second, third), rotation=Rotation(self.ledger, policy, self.clock))
        self.assertEqual(session.synthesize(session.review(session.plan("task"))), "result")
        self.assertEqual([len(seat.prompts) for seat in (first, second, third)], [1, 1, 1])
        self.assertEqual([session.stages[stage]["profile"] for stage in ("plan", "review", "synthesize")],
                         ["a", "b", "c"])
        self.assertEqual(session.exhausted, [])
        details = session.details()
        self.assertEqual(details["rotation"], policy.as_record())
        self.assertEqual([item["account"] for item in details["selection"]], ["codex:a", "codex:b", "codex:c"])
        review = details["selection"][1]["candidates"]
        self.assertEqual([(item["account"], item["status"]) for item in review],
                         [("codex:a", "over_budget"), ("codex:b", "ready"), ("codex:c", "ready")])
        self.assertNotIn("task", json.dumps(details["selection"]))
        # The ledger carries the standing into the next run.
        later = LiveSession(Seat("codex", "a"), Seat("codex", "a"), (Seat("codex", "c", [measured("p", 1)]),),
                            rotation=Rotation(self.ledger, policy, self.clock))
        self.assertEqual(later.plan("task"), "p")
        self.assertEqual(later.stages["plan"]["profile"], "c")

    def test_a_refused_account_rests_and_the_run_and_the_next_run_skip_it(self):
        policy = Policy(cooldown_seconds=900)
        spent = Seat("claude", "a", [ProviderError("rate_limited")])
        spare = Seat("claude", "b", [measured("plan", 5), measured("review", 5), measured("result", 5)])
        session = LiveSession(spent, spent, (spare,), rotation=Rotation(self.ledger, policy, self.clock))
        self.assertEqual(session.synthesize(session.review(session.plan("task"))), "result")
        self.assertEqual((len(spent.prompts), len(spare.prompts)), (1, 3))
        self.assertEqual(session.selections[0]["account"], "claude:b")
        self.assertEqual(standing("claude:a", read_ledger(self.ledger), policy, self.now)["status"], "cooling_down")
        rested = Seat("claude", "a")
        later = LiveSession(rested, rested, (Seat("claude", "b", [measured("p", 1)]),),
                            rotation=Rotation(self.ledger, policy, self.clock))
        later.plan("task")
        self.assertEqual(rested.prompts, [])
        # Every account resting: the stage fails as out of usage and nothing is asked.
        record(self.ledger, "claude:b", policy, now=self.now, error="rate_limited")
        idle = Seat("claude", "b")
        stuck = LiveSession(Seat("claude", "a"), idle, (), rotation=Rotation(self.ledger, policy, self.clock))
        with self.assertRaisesRegex(ProviderError, "^rate_limited$"):
            stuck.plan("task")
        self.assertEqual(idle.prompts, [])

    def reading(self, *windows, allowed=True, age=0):
        return Quota(tuple(Window(*window) for window in windows), allowed, self.now - age)

    def test_real_usage_moves_work_at_the_switch_point_and_rests_a_full_account(self):
        policy = Policy(switch_percent=80)
        seats = [Seat("codex", "a"), Seat("codex", "b"), Seat("codex", "c"), Seat("codex", "d")]
        soon, later = int(self.now) + 600, int(self.now) + 86400
        store_quota(self.ledger, {
            "codex:a": self.reading((85, 300, soon), (20, 10080, later)),
            "codex:b": self.reading((40, 300, soon), (60, 10080, later)),
            "codex:c": self.reading((10, 300, soon), (100, 10080, later)),
            "codex:d": self.reading((30, 300, soon), (5, 10080, later)),
        })
        names, report = self.order(seats, policy, sticky={"codex": "codex:a"})
        # d has the most room. a is past the switch point. c is full for the week.
        self.assertEqual(names, ["codex:d", "codex:b", "codex:a"])
        by_name = {item["account"]: item for item in report}
        self.assertEqual(
            {name: (item["status"], item["used_percent"]) for name, item in by_name.items()},
            {"codex:a": ("over_budget", 85), "codex:b": ("ready", 60),
             "codex:c": ("exhausted", 100), "codex:d": ("ready", 30)},
        )
        self.assertEqual(by_name["codex:c"]["resets_in_seconds"], 86400)
        self.assertEqual(by_name["codex:a"]["usage_source"], "provider")
        # The five-hour window resets: a is ready again and, being in use, stays first.
        self.now += 601
        names, report = self.order(seats, policy, sticky={"codex": "codex:a"})
        self.assertEqual(names[0], "codex:a")
        self.assertEqual(report[0]["used_percent"], 20)
        # After the weekly reset c is usable again without a new reading.
        self.now += 86400
        self.assertIn("codex:c", self.order(seats, policy)[0])

    def test_an_account_with_no_reading_goes_after_accounts_that_have_one(self):
        policy = Policy()
        store_quota(self.ledger, {"codex:b": self.reading((70, 300, int(self.now) + 900))})
        names, _ = self.order([Seat("codex", "silent"), Seat("codex", "b")], policy)
        self.assertEqual(names, ["codex:b", "codex:silent"])
        # Grok cannot be asked, so its accounts are not held back for having no reading.
        record(self.ledger, "grok:b", policy, now=self.now - 1, usage=Usage())
        names, _ = self.order([Seat("grok", "b"), Seat("grok", "fresh")], policy, preferred="grok")
        self.assertEqual(names, ["grok:fresh", "grok:b"])

    def test_a_provider_refusal_rests_the_account_until_its_window_resets(self):
        policy = Policy()
        seats = [Seat("claude", "a"), Seat("claude", "b")]
        store_quota(self.ledger, {
            "claude:a": self.reading((97, 300, int(self.now) + 900), allowed=False),
            "claude:b": self.reading(allowed=False),
        })
        names, report = self.order(seats, policy, preferred="claude")
        self.assertEqual(names, [])
        self.assertEqual([item["status"] for item in report], ["exhausted", "exhausted"])
        self.assertEqual(report[0]["resets_in_seconds"], 900)
        # Without a reset time the refusal only holds while the reading is fresh.
        self.now += policy.quota_max_age + 1
        self.assertEqual(self.order(seats, policy, preferred="claude")[0], ["claude:b"])
        self.now += 900
        self.assertEqual(sorted(self.order(seats, policy, preferred="claude")[0]), ["claude:a", "claude:b"])

    def test_readings_are_asked_for_only_when_missing_or_old(self):
        policy = Policy(quota_max_age=100, costly_quota_max_age=1000)
        asked = []

        def reader(seat):
            asked.append(account_key(seat.provider, seat.profile))
            if seat.profile == "broken":
                return None
            return self.reading((50, 300, int(self.now) + 5000))

        rotation = Rotation(self.ledger, policy, self.clock, reader)
        seats = [Seat("codex", "a"), Seat("codex", "broken"), Seat("claude", "x"), Seat("grok", "g")]
        rotation.choose(seats, seats[0])
        # Only the role's provider is asked.
        self.assertEqual(sorted(asked), ["codex:a", "codex:broken"])
        rotation.choose(seats, seats[0])
        self.assertEqual(len(asked), 2)
        self.now += 101
        rotation.choose(seats, seats[0])
        # The fresh-enough reading is old now. The unreadable account is not asked again.
        self.assertEqual(asked[2:], ["codex:a"])
        rotation.choose(seats, seats[2])
        self.now += 101
        rotation.choose(seats, seats[2])
        # Claude readings cost usage, so they are kept longer.
        self.assertEqual(asked.count("claude:x"), 1)
        self.now += 1000
        rotation.choose(seats, seats[2])
        self.assertEqual(asked.count("claude:x"), 2)
        self.assertEqual(standing("codex:broken", read_ledger(self.ledger), policy, self.now)["usage_source"], "counted")

    def test_a_run_leaves_an_account_when_its_real_usage_passes_the_switch_point(self):
        policy = Policy(switch_percent=90, quota_max_age=1)
        used = {"codex:a": 50, "codex:b": 10}

        def reader(seat):
            return self.reading((used[account_key(seat.provider, seat.profile)], 300, int(self.now) + 5000))

        first = Seat("codex", "a", [measured("plan", 1), measured("result", 1)])
        second = Seat("codex", "b", [measured("review", 1)])
        session = LiveSession(first, first, (second,), rotation=Rotation(self.ledger, policy, self.clock, reader))
        # b has more room, so it plans. Then b fills up and a takes over.
        first.replies, second.replies = [measured("review", 1), measured("result", 1)], [measured("plan", 1)]
        session.plan("task")
        used["codex:b"] = 95
        self.now += 2
        session.review("plan")
        session.synthesize("review")
        self.assertEqual([session.stages[stage]["profile"] for stage in ("plan", "review", "synthesize")],
                         ["b", "a", "a"])
        chosen = session.selections[1]["candidates"]
        self.assertEqual({item["account"]: item["used_percent"] for item in chosen},
                         {"codex:a": 50, "codex:b": 95})

    def test_codex_and_claude_readings_are_parsed_strictly(self):
        codex = {
            "ordinaryUsageAllowed": True, "accountId": "invented-account",
            "rateLimits": {
                "primary": {"usedPercent": 9, "windowDurationMins": 300, "resetsAt": 1795284244},
                "secondary": {"usedPercent": 17, "windowDurationMins": 10080, "resetsAt": None},
            },
        }
        quota = parse_codex_rate_limits(codex, self.now)
        self.assertEqual(quota.windows, (Window(9, 300, 1795284244), Window(17, 10080, None)))
        self.assertTrue(quota.allowed)
        self.assertNotIn("invented-account", json.dumps(quota.as_record()))
        self.assertEqual(read_record(json.loads(json.dumps(quota.as_record()))), quota)
        for bad in (
            None, {}, {"rateLimits": []}, {"rateLimits": {}},
            {"rateLimits": {"primary": {"usedPercent": "9"}}},
            {"rateLimits": {"primary": {"usedPercent": -1}}},
            {"rateLimits": {"primary": {"usedPercent": True}}},
            {"rateLimits": {"primary": {"usedPercent": 5}}, "ordinaryUsageAllowed": "yes"},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_codex_rate_limits(bad, self.now)

        def stream(*messages):
            return "\n".join(json.dumps(message) for message in messages).encode()

        event = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "resetsAt": 1795283400,
            "unifiedWindows": {
                "five_hour": {"utilization": 0.48, "resetsAt": 1795283400, "status": "allowed"},
                "seven_day": {"utilization": 0.27, "resetsAt": 1795766400, "status": "allowed"},
            },
        }}
        quota = parse_claude_stream(stream({"type": "system"}, event, {"type": "result"}), self.now)
        self.assertEqual(quota.windows, (Window(48, 300, 1795283400), Window(27, 10080, 1795766400)))
        self.assertTrue(quota.allowed)
        refused = parse_claude_stream(stream({"type": "result", "is_error": True, "api_error_status": 429}), self.now)
        self.assertEqual((refused.windows, refused.allowed), ((), False))
        rejected = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1795283400}}
        self.assertEqual(parse_claude_stream(stream(rejected), self.now),
                         Quota((Window(100, None, 1795283400),), False, self.now))
        for bad in (
            b"", b"not json", stream({"type": "result"}),
            stream({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {}}}),
            stream({"type": "rate_limit_event", "rate_limit_info": {
                "status": "allowed", "unifiedWindows": {"five_hour": {"utilization": "half"}}}}),
            stream({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": []}}),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_claude_stream(bad, self.now)
        for bad in ({"windows": [], "allowed": None}, {"windows": [[1, 2]], "allowed": None, "observed_at": 1},
                    {"windows": [], "allowed": 1, "observed_at": 1}, {"windows": [], "allowed": None, "observed_at": "now"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                read_record(bad)

    def test_without_rotation_a_run_records_no_selection(self):
        seat = Seat("codex", None, [measured("plan", 5)])
        session = LiveSession(seat, seat)
        session.plan("task")
        self.assertNotIn("selection", session.details())
        self.assertNotIn("rotation", session.details())
        self.assertFalse(self.ledger.exists())


if __name__ == "__main__":
    unittest.main()

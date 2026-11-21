import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import argparse

from firstchair.accounts import account_environment
from firstchair.cli import _fallback_seat, _print_text, _resume_session, main
from firstchair.live import LiveSession, build_command, live_environment
from firstchair.provider import ProviderError
from firstchair.usage import Reply, Usage


TASK = "Plan the sorting of sample cards."
PLAN = "Group the cards by color."
REVIEW = "Check the labels before sorting."
RESULT = "Check the labels, then group the cards by color."

FAKE_PROVIDER = r"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

provider = Path(sys.argv[0]).name
args = sys.argv[1:]
fixture = Path(os.environ["FIRSTCHAIR_TEST_FIXTURE"])
behavior = os.environ.get("FIRSTCHAIR_TEST_BEHAVIOR", "success")

def emit(event):
    print(json.dumps(event))

call = {"provider": provider, "args": args}
if "--version" not in args:
    call["stdin"] = sys.stdin.read()
    if "--prompt-file" in args:
        path = Path(args[args.index("--prompt-file") + 1])
        call["prompt_file"] = path.read_text(encoding="utf-8")
with (fixture / "calls.jsonl").open("a", encoding="utf-8") as log:
    print(json.dumps(call), file=log)

if "--version" in args:
    versions = {
        "codex": "codex-cli 0.171.0",
        "claude": "2.1.329 (Claude Code)",
        "grok": "grok 1.0.77 (000000000000) [stable]",
    }
    if provider == "codex" and behavior == "unsupported":
        versions["codex"] = "codex-cli 0.190.0"
    print(versions[provider])
    sys.exit(0)

if behavior == "timeout":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
    (fixture / "grandchild.pid").write_text(str(child.pid), encoding="utf-8")
    time.sleep(8)
    sys.exit(0)

if provider == "codex":
    if "Reviewer's feedback:" in call["stdin"]:
        text = RESULT
        usage = {"input_tokens": 17, "output_tokens": 7}
    elif "independent reviewer" in call["stdin"]:
        text = REVIEW
        usage = {"input_tokens": 13, "output_tokens": 5}
    else:
        text = PLAN
        usage = {"input_tokens": 11, "output_tokens": 3}
    emit({"type": "thread.started", "thread_id": "fixture-thread"})
    emit({"type": "turn.started"})
    emit({"type": "item.completed",
          "item": {"id": "fixture-item", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": usage})
elif provider == "claude":
    if behavior == "claude_429":
        emit({"type": "result", "subtype": "error_during_execution",
              "is_error": True, "api_error_status": 429, "result": "Fixture error"})
        sys.exit(1)
    emit({"type": "result", "subtype": "success", "is_error": False, "result": PLAN,
          "usage": {"input_tokens": 11, "output_tokens": 3}})
else:
    emit({"type": "text", "data": REVIEW[:9]})
    emit({"type": "text", "data": REVIEW[9:]})
    emit({"type": "usage", "usage": {"input_tokens": 13, "output_tokens": 5}})
    emit({"type": "end", "stopReason": "end_turn"})
"""


class LivePolicyTests(unittest.TestCase):
    def test_read_only_commands(self):
        workspace = Path("sample-workspace")
        prompt_file = workspace / "prompt.txt"

        argv, use_stdin = build_command("codex", None, prompt_file)
        self.assertTrue(use_stdin)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        self.assertEqual(argv[argv.index("--config") + 1], "mcp_servers={}")

        for directory, tools in [(None, ""), (workspace, "Read,Grep,Glob")]:
            with self.subTest(workspace=directory):
                argv, use_stdin = build_command("claude", directory, prompt_file)
                self.assertTrue(use_stdin)
                self.assertEqual(argv[argv.index("--tools") + 1], tools)
                self.assertIn("--strict-mcp-config", argv)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")

        for directory in (None, workspace):
            with self.subTest(workspace=directory):
                argv, use_stdin = build_command("grok", directory, prompt_file)
                self.assertFalse(use_stdin)
                denied = tuple(zip(argv, argv[1:]))
                self.assertIn(("--deny", "mcp__*"), denied)
                self.assertIn(("--deny", "Bash"), denied)
                self.assertIn(("--deny", "run_terminal_command"), denied)
                self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
                self.assertEqual(argv[argv.index("--prompt-file") + 1], str(prompt_file))

    def test_live_environment_filters_credentials_and_unrelated_variables(self):
        homes = {
            "codex": "CODEX_HOME",
            "claude": "CLAUDE_CONFIG_DIR",
            "grok": "GROK_HOME",
        }
        excluded = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY", "FIXTURE_UNRELATED")
        with tempfile.TemporaryDirectory() as directory:
            home = str(Path(directory) / "home")
            settings = {"HOME": home, **dict.fromkeys(excluded, "fixture-value")}
            settings.update({variable: str(Path(directory) / name) for name, variable in homes.items()})
            with patch.dict(os.environ, settings, clear=True):
                for provider, variable in homes.items():
                    with self.subTest(provider=provider):
                        environment = live_environment(provider)
                        self.assertEqual(environment["HOME"], home)
                        self.assertEqual(environment[variable], settings[variable])
                        for name in (*excluded, *(set(homes.values()) - {variable})):
                            self.assertNotIn(name, environment)


@unittest.skipUnless(os.name == "posix", "requires executable Python scripts with POSIX shebangs")
class LiveCLITests(unittest.TestCase):
    def setUp(self):
        self.fixture = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.fixture / "runs"
        constants = f"PLAN = {PLAN!r}\nREVIEW = {REVIEW!r}\nRESULT = {RESULT!r}\n"
        for provider in ("codex", "claude", "grok"):
            script = self.fixture / provider
            script.write_text(f"#!{sys.executable}\n{constants}{FAKE_PROVIDER}", encoding="utf-8")
            script.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {
            "PATH": os.pathsep.join(filter(None, (str(self.fixture), os.environ.get("PATH")))),
            "HOME": str(self.fixture / "home"),
            "CODEX_HOME": str(self.fixture / "codex-home"),
            "CLAUDE_CONFIG_DIR": str(self.fixture / "claude-home"),
            "GROK_HOME": str(self.fixture / "grok-home"),
            "FIRSTCHAIR_TEST_FIXTURE": str(self.fixture),
            "FIRSTCHAIR_TEST_BEHAVIOR": "success",
        }))

    def cli(self, *args, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--state-dir", str(self.state), "--session", self.session, *args])
        self.assertEqual(code, expected, stderr.getvalue() or stdout.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    session = "fixture-session"

    def run_live(self, *options, provider="codex", expected=0):
        return self.cli("run", "--provider", provider, *options, TASK, expected=expected)

    def saved_record(self, stdout):
        record = json.loads(stdout)
        paths = list(self.state.glob("*.json"))
        self.assertEqual([path.name for path in paths], [f"{record['run_id']}.json"])
        self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8")), record)
        return record

    def calls(self):
        path = self.fixture / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_codex_grok_workflow_saves_stages_and_measured_usage(self):
        stdout, _ = self.run_live("--reviewer", "grok")
        record = self.saved_record(stdout)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["result"], RESULT)
        self.assertEqual(
            {stage: (data["provider"], data["text"]) for stage, data in record["stages"].items()},
            {"plan": ("codex", PLAN), "review": ("grok", REVIEW), "synthesize": ("codex", RESULT)},
        )
        self.assertEqual(record["usage"], {
            "provenance": "measured", "input_tokens": 41, "output_tokens": 15,
        })
        plan, review, synthesis = [call for call in self.calls() if "--version" not in call["args"]]
        self.assertEqual([call["provider"] for call in (plan, review, synthesis)], ["codex", "grok", "codex"])
        self.assertIn(TASK, plan["stdin"])
        self.assertIn(PLAN, review["prompt_file"])
        self.assertEqual(review["stdin"], "")
        self.assertIn(REVIEW, synthesis["stdin"])

    def test_text_format_prints_result_section(self):
        stdout, _ = self.run_live("--format", "text")
        self.assertIn(f"## Result\n\n{RESULT}", stdout)

    def test_unsupported_version_is_rejected_before_record_creation(self):
        with patch.dict(os.environ, {"FIRSTCHAIR_TEST_BEHAVIOR": "unsupported"}):
            stdout, stderr = self.run_live(expected=1)
        self.assertEqual(stdout, "")
        self.assertIn("not ready", stderr)
        self.assertIn("unsupported_provider_version", stderr)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.calls(), [{"provider": "codex", "args": ["--version"]}])

    def test_claude_rate_limit_fails_plan_without_stage_output(self):
        with patch.dict(os.environ, {"FIRSTCHAIR_TEST_BEHAVIOR": "claude_429"}):
            stdout, _ = self.run_live(provider="claude", expected=1)
        record = self.saved_record(stdout)
        self.assertEqual((record["status"], record["stage"], record["error"]),
                         ("failed", "plan", "rate_limited"))
        self.assertEqual(record["stages"], {})
        self.assertEqual(record["exhausted"], [{
            "provider": "claude", "profile": None, "error": "rate_limited", "stage": "plan",
        }])
        self.assertNotIn("result", record)

    def test_reviewer_out_of_usage_hands_the_rest_to_the_lead(self):
        with patch.dict(os.environ, {"FIRSTCHAIR_TEST_BEHAVIOR": "claude_429"}):
            stdout, _ = self.run_live("--reviewer", "claude")
        record = self.saved_record(stdout)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["result"], RESULT)
        self.assertEqual(
            {stage: (data["provider"], data["text"]) for stage, data in record["stages"].items()},
            {"plan": ("codex", PLAN), "review": ("codex", REVIEW), "synthesize": ("codex", RESULT)},
        )
        self.assertEqual(record["exhausted"], [{
            "provider": "claude", "profile": None, "error": "rate_limited", "stage": "review",
        }])
        self.assertEqual(record["usage"]["provenance"], "partial")
        model_calls = [call for call in self.calls() if "--version" not in call["args"]]
        self.assertEqual(
            [call["provider"] for call in model_calls],
            ["codex", "claude", "codex", "codex"],
        )

    def test_resume_after_usage_failure_uses_the_added_provider(self):
        with patch.dict(os.environ, {"FIRSTCHAIR_TEST_BEHAVIOR": "claude_429"}):
            stdout, _ = self.run_live(provider="claude", expected=1)
        failed = self.saved_record(stdout)
        before = len(self.calls())
        stdout, _ = self.cli("resume", failed["run_id"], "--provider", "codex")
        record = self.saved_record(stdout)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["result"], RESULT)
        self.assertEqual(
            [data["provider"] for data in record["stages"].values()],
            ["codex", "codex", "codex"],
        )
        self.assertEqual(record["stage_outputs"]["plan"], PLAN)
        new_calls = [call for call in self.calls()[before:] if "--version" not in call["args"]]
        self.assertEqual([call["provider"] for call in new_calls], ["codex", "codex", "codex"])

    def test_resume_retries_a_retired_provider_only_when_asked(self):
        with patch.dict(os.environ, {"FIRSTCHAIR_TEST_BEHAVIOR": "claude_429"}):
            stdout, _ = self.run_live(provider="claude", expected=1)
        failed = self.saved_record(stdout)
        before = len(self.calls())
        # The limit has reset, but the run still treats Claude as retired.
        stdout, _ = self.cli("resume", failed["run_id"], expected=1)
        again = self.saved_record(stdout)
        self.assertEqual((again["status"], again["error"]), ("failed", "rate_limited"))
        self.assertEqual(len(self.calls()), before)
        stdout, _ = self.cli("resume", failed["run_id"], "--retry-exhausted")
        record = self.saved_record(stdout)
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["exhausted"], [])
        self.assertEqual([data["provider"] for data in record["stages"].values()], ["claude"] * 3)

    def test_another_session_cannot_resume_or_recover_a_run(self):
        with patch.dict(os.environ, {"FIRSTCHAIR_TEST_BEHAVIOR": "claude_429"}):
            stdout, _ = self.run_live(provider="claude", expected=1)
        failed = self.saved_record(stdout)
        self.assertEqual(failed["session"], "fixture-session")
        before = len(self.calls())
        self.session = "other-session"
        for command in (("resume", failed["run_id"], "--retry-exhausted"), ("recover", failed["run_id"])):
            _, error = self.cli(*command, expected=1)
            self.assertIn("belongs to session fixture-session", error)
        # Reading is allowed, and the listing names the owner.
        stdout, _ = self.cli("status", failed["run_id"], expected=1)
        self.assertEqual(json.loads(stdout), failed)
        stdout, _ = self.cli("runs")
        self.assertEqual(json.loads(stdout)["runs"][0]["session"], "fixture-session")
        self.assertEqual(len(self.calls()), before)
        self.session = "fixture-session"
        self.cli("resume", failed["run_id"], "--retry-exhausted")

    def test_a_run_without_a_session_makes_one(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {key: value for key, value in os.environ.items() if key != "FIRSTCHAIR_SESSION"}
        with patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(["--state-dir", str(self.state), "run", "--provider", "codex", TASK])
            self.assertEqual(code, 0, stderr.getvalue())
            made = json.loads(stdout.getvalue())["session"]
            self.assertRegex(made, r"^s-[0-9a-f]{12}$")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = main(["--state-dir", str(self.state), "--session", "Bad Id", "run", "--provider", "codex", TASK])
            self.assertEqual(code, 1)
            self.assertIn("session must be", stderr.getvalue())

    def test_rotation_spreads_stages_over_signed_in_accounts_and_resume_keeps_the_rules(self):
        state = self.fixture / "xdg"
        state.mkdir(mode=0o700)
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}))
        # The invented CLIs are not asked for real usage here. test_rotation covers readings.
        self.enterContext(patch("firstchair.cli._read_seat_quota", return_value=None))
        for name in ("a", "b", "c", "d"):
            account_environment("codex", name, create=True)
        account_environment("claude", "unused", create=True)
        stdout, _ = self.run_live("--rotate-accounts", "--account-call-limit", "1", "--dry-run", "--profile", "a")
        planned = json.loads(stdout)
        self.assertEqual(planned["rotation"]["call_limit"], 1)
        self.assertEqual([item["account"] for item in planned["accounts"]],
                         ["codex:a", "codex:b", "codex:c", "codex:d"])
        self.assertFalse((state / "firstchair" / "account-ledger.json").exists())

        stdout, _ = self.run_live("--rotate-accounts", "--account-call-limit", "1", "--profile", "a")
        record = self.saved_record(stdout)
        self.assertEqual(record["status"], "completed")
        # One stage per account: each reaches its budget and the next one takes over.
        self.assertEqual([record["stages"][stage]["profile"] for stage in ("plan", "review", "synthesize")],
                         ["a", "b", "c"])
        self.assertEqual([item["account"] for item in record["selection"]], ["codex:a", "codex:b", "codex:c"])
        self.assertEqual(len(record["roster"]), 4)
        self.assertNotIn(TASK, json.dumps(record["selection"]))
        ledger = json.loads((state / "firstchair" / "account-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(ledger["accounts"]), ["codex:a", "codex:b", "codex:c"])
        self.assertNotIn(TASK, json.dumps(ledger))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["auth", "usage"]), 0)
        shown = {item["account"]: item for item in json.loads(out.getvalue())["accounts"]}
        self.assertEqual(shown["codex:a"]["calls"], 1)
        self.assertEqual((shown["codex:d"]["calls"], shown["claude:unused"]["status"]), (0, "ready"))

        args = argparse.Namespace(
            timeout=None, max_output_bytes=None, max_input_bytes=None, total_timeout=None,
            workspace=None, provider=None, profile=None, reviewer=None, reviewer_profile=None,
            fallback=None, retry_exhausted=False,
        )
        resumed = _resume_session(args, record)
        self.assertEqual(resumed.rotation.policy.call_limit, 1)
        self.assertEqual(len(resumed.seats()), 4)
        self.assertEqual(len(resumed.selections), 3)
        with self.assertRaisesRegex(ValueError, "rotation policy"):
            _resume_session(args, {**record, "rotation": {"call_limit": 1}})
        with self.assertRaisesRegex(ValueError, "too many"):
            _resume_session(args, {key: value for key, value in record.items() if key != "rotation"})

        _, error = self.run_live("--account-call-limit", "1", expected=1)
        self.assertIn("require --rotate-accounts", error)
        _, error = self.run_live("--fallback", "claude", "--fallback", "grok", "--fallback", "codex:b", expected=1)
        self.assertIn("at most 3 providers", error)

    def test_timeout_kills_grandchild(self):
        with patch.dict(os.environ, {"FIRSTCHAIR_TEST_BEHAVIOR": "timeout"}):
            stdout, _ = self.run_live("--timeout", "1", expected=1)
        record = self.saved_record(stdout)
        self.assertEqual((record["status"], record["stage"], record["error"]),
                         ("failed", "plan", "provider_timeout"))
        pid = int((self.fixture / "grandchild.pid").read_text(encoding="utf-8"))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_live_dry_run_reports_read_only_without_record_or_model_calls(self):
        stdout, _ = self.run_live("--dry-run")
        result = json.loads(stdout)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(result["permissions"], "read-only")
        self.assertFalse(self.state.exists())
        calls = self.calls()
        self.assertTrue(calls)
        self.assertTrue(all(call["args"] == ["--version"] for call in calls))

    def test_synthetic_provider_rejects_workspace(self):
        _, stderr = self.run_live("--workspace", str(self.fixture), provider="synthetic", expected=1)
        self.assertIn("--workspace", stderr)
        self.assertIn("require a live --provider", stderr)
        self.assertFalse(self.state.exists())

    def test_doctor_reports_versions_and_enabled_contracts(self):
        stdout, _ = self.cli("doctor")
        providers = json.loads(stdout)["providers"]
        for name, version in {"codex": "0.171.0", "claude": "2.1.329", "grok": "1.0.77"}.items():
            with self.subTest(provider=name):
                provider = providers[name]
                self.assertEqual(provider["version"], version)
                self.assertEqual(provider["contract"], "verified")
                self.assertIs(provider["enabled"], True)
        self.assertCountEqual(self.calls(), [
            {"provider": "codex", "args": ["--version"]},
            {"provider": "claude", "args": ["--version"]},
            {"provider": "grok", "args": ["--no-auto-update", "--version"]},
        ])

    def test_offline_doctor_discovers_providers_without_running_them(self):
        stdout, _ = self.cli("doctor", "--offline")
        providers = json.loads(stdout)["providers"]
        for name in ("codex", "claude", "grok"):
            with self.subTest(provider=name):
                self.assertEqual(providers[name]["discovery"], "installed")
                self.assertIs(providers[name]["enabled"], False)
        self.assertEqual(self.calls(), [])


class _Seat:
    def __init__(self, provider, profile=None, replies=()):
        self.provider = provider
        self.profile = profile
        self.version = "fixture"
        self.replies = list(replies)
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Reply(reply, Usage("measured", 1, 1))


class LiveFailoverTests(unittest.TestCase):
    def test_second_account_finishes_when_the_first_is_out_of_usage(self):
        empty = _Seat("claude", "work", [ProviderError("rate_limited")])
        reserve = _Seat("claude", "personal", ["plan", "review", "result"])
        session = LiveSession(empty, reserve)
        self.assertEqual(session.plan("task"), "plan")
        self.assertEqual(session.review("plan"), "review")
        self.assertEqual(session.synthesize("review"), "result")
        self.assertEqual(len(empty.prompts), 1)
        self.assertEqual(len(reserve.prompts), 3)
        self.assertEqual(session.exhausted, [{
            "provider": "claude", "profile": "work", "error": "rate_limited", "stage": "plan",
        }])

    def test_fallback_flag_names_a_provider_or_a_second_account(self):
        self.assertEqual(_fallback_seat("codex"), ("codex", None))
        self.assertEqual(_fallback_seat("claude:personal"), ("claude", "personal"))
        for bad in ("gemini", "claude:", ":personal", ""):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                _fallback_seat(bad)
        args = argparse.Namespace(
            timeout=None, max_output_bytes=None, max_input_bytes=None, total_timeout=None,
            workspace=None, provider=None, profile=None, reviewer=None, reviewer_profile=None,
            fallback=[("codex", "personal")], retry_exhausted=False,
        )
        roster = [{"provider": "codex", "profile": "work", "roles": ["lead", "reviewer"]}]
        with patch("firstchair.live.profile_home", return_value=Path("/invented/home")):
            resumed = _resume_session(args, {"task": "task", "roster": roster})
        self.assertEqual(
            [(seat.provider, seat.profile) for seat in resumed.seats()],
            [("codex", "work"), ("codex", "personal")],
        )

    def test_roster_records_roles_and_resume_keeps_them(self):
        lead, extra = _Seat("codex"), _Seat("grok")
        session = LiveSession(lead, lead, (extra,))
        self.assertEqual(session.roster(), [
            {"provider": "codex", "profile": None, "roles": ["lead", "reviewer"]},
            {"provider": "grok", "profile": None, "roles": []},
        ])
        args = argparse.Namespace(
            timeout=None, max_output_bytes=None, max_input_bytes=None, total_timeout=None,
            workspace=None, provider="claude", profile=None, reviewer=None, reviewer_profile=None,
            fallback=None, retry_exhausted=False,
        )
        resumed = _resume_session(args, {"task": "task", "roster": session.roster()})
        # The fallback seat does not become the reviewer, and the new provider is an extra.
        self.assertEqual((resumed.lead.provider, resumed.reviewer.provider), ("codex", "codex"))
        self.assertIs(resumed.lead, resumed.reviewer)
        self.assertEqual([seat.provider for seat in resumed.extras], ["grok", "claude"])
        split = LiveSession(_Seat("codex"), _Seat("claude"), (_Seat("grok"),)).roster()
        args.provider = None
        resumed = _resume_session(args, {"task": "task", "roster": split})
        self.assertEqual((resumed.lead.provider, resumed.reviewer.provider), ("codex", "claude"))
        self.assertEqual([seat.provider for seat in resumed.extras], ["grok"])
        for label, roster in (
            ("unknown role", [{"provider": "codex", "profile": None, "roles": ["owner"]}]),
            ("two leads", [{"provider": "codex", "profile": None, "roles": ["lead", "reviewer"]},
                           {"provider": "grok", "profile": None, "roles": ["lead"]}]),
            ("no reviewer", [{"provider": "codex", "profile": None, "roles": ["lead"]}]),
            ("not a list", [{"provider": "codex", "profile": None, "roles": "lead"}]),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "roster"):
                _resume_session(args, {"task": "task", "roster": roster})

    def test_timeout_and_unavailable_also_fall_through(self):
        for code in ("provider_timeout", "provider_unavailable"):
            with self.subTest(code=code):
                first = _Seat("codex", replies=[ProviderError(code)])
                second = _Seat("grok", replies=["kept going"])
                session = LiveSession(first, second)
                self.assertEqual(session.plan("task"), "kept going")
                self.assertEqual(session.stages["plan"]["provider"], "grok")

    def test_authorization_failure_does_not_switch_providers(self):
        lead = _Seat("codex", replies=["plan"])
        reviewer = _Seat("claude", replies=[ProviderError("authorization_failed")])
        session = LiveSession(lead, reviewer)
        session.plan("task")
        with self.assertRaisesRegex(ProviderError, "^authorization_failed$"):
            session.review("plan")
        self.assertEqual(len(lead.prompts), 1)
        self.assertEqual(len(reviewer.prompts), 1)
        self.assertEqual(session.exhausted, [])

    def test_one_remaining_provider_does_every_later_stage(self):
        claude = _Seat("claude", replies=[ProviderError("rate_limited")])
        codex = _Seat("codex", replies=["plan", "review", "result"])
        grok = _Seat("grok", replies=[ProviderError("rate_limited")])
        session = LiveSession(claude, grok, (codex,))
        self.assertEqual(session.synthesize(session.review(session.plan("task"))), "result")
        self.assertEqual(len(claude.prompts), 1)
        self.assertEqual(len(grok.prompts), 1)
        self.assertEqual(len(codex.prompts), 3)

    def test_restore_does_not_retry_a_seat_missing_from_a_bad_exhausted_entry(self):
        session = LiveSession(_Seat("codex"), _Seat("claude"))
        with self.assertRaisesRegex(ValueError, "exhausted"):
            session.restore({"task": "task", "exhausted": [{}]})
        with self.assertRaisesRegex(ValueError, "attempt"):
            session.restore({"task": "task", "attempted": -1})
        with self.assertRaisesRegex(ValueError, "stage"):
            session.restore({"task": "task", "stages": {"plan": {"text": "plan"}}})

    def test_restore_skips_a_named_exhausted_seat_and_keeps_plan_text(self):
        spent = _Seat("codex", replies=["unused"])
        reserve = _Seat("claude", replies=["result", "plan"])
        session = LiveSession(spent, reserve)
        session.restore({
            "task": "keep going",
            "stage_outputs": {"plan": "saved plan"},
            "exhausted": [{"provider": "codex", "profile": None, "error": "rate_limited"}],
        })
        self.assertEqual(session.attempted, 0)
        self.assertEqual(session.plan_text, "saved plan")
        session.synthesize("review")
        self.assertIn("saved plan", reserve.prompts[0])
        self.assertEqual(session.plan("keep going"), "plan")
        self.assertEqual(spent.prompts, [])

    def test_text_summary_skips_an_exhausted_entry_without_an_error(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            _print_text({
                "status": "failed",
                "error": "rate_limited",
                "run_id": "00000000-0000-4000-8000-000000000001",
                "exhausted": [
                    {"provider": "codex"},
                    {"provider": "claude", "error": "rate_limited"},
                ],
            })
        text = stdout.getvalue()
        self.assertIn("Skipped: claude (rate_limited)", text)
        self.assertNotIn("codex", text)

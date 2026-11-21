"""The primary seat runs a score of isolated workers, including its own provider."""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from firstchair import delegation as delegate
from firstchair.cli import main

FAKE_CODEX = """\
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.170.0")
    sys.exit(0)
prompt = sys.stdin.read()
cwd = Path(args[args.index("--cd") + 1])
reply = Path(args[args.index("--output-last-message") + 1])
if args[args.index("--sandbox") + 1] == "workspace-write":
    (cwd / "added.txt").write_text("invented change\\n")
reply.write_text("## Summary\\nfake codex saw " + str(len(prompt)) + " bytes\\n")
print('{"type":"thread.started","thread_id":"fixture"}')
for event in (
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "fake", "type": "agent_message", "text": "Fixture"}},
    {"type": "turn.completed", "usage": {"input_tokens": 42, "output_tokens": 8}},
):
    print(json.dumps(event))
stamps = os.environ.get("FAKE_STAMP_DIR")
if stamps:
    # A part records overlap only when it sees a peer that has started and
    # not ended while this part is itself still running. It then stays
    # running until a peer has recorded the same, so neither can miss the other.
    stamps = Path(stamps)
    mine = stamps / (cwd.name + ".overlap")
    (stamps / (cwd.name + ".start")).write_text("")
    deadline = time.monotonic() + float(os.environ.get("FAKE_PEER_WAIT", "0"))
    while time.monotonic() < deadline:
        peers = [path.stem for path in stamps.glob("*.start") if path.stem != cwd.name]
        if any(not (stamps / (peer + ".end")).exists() for peer in peers):
            mine.write_text("")
        if mine.exists() and any((stamps / (peer + ".overlap")).exists() for peer in peers):
            break
        time.sleep(0.02)
    (stamps / (cwd.name + ".end")).write_text("")
sys.exit(int(os.environ.get("FAKE_WORKER_EXIT", "0")))
"""

FAKE_GROK = """\
import json, sys
from pathlib import Path
args = sys.argv[1:]
if "--no-subagents" not in args:
    sys.exit(2)
prompt = Path(args[args.index("--prompt-file") + 1]).read_text()
cwd = Path(args[args.index("--cwd") + 1])
if "--disallowed-tools" not in args:
    (cwd / "README.md").write_text("edited by invented grok\\n")
print(json.dumps({
    "text": "## Summary\\nfake grok mode " + ("read" if "--disallowed-tools" in args else "write"),
    "stopReason": "end_turn",
}))
"""

FAKE_CLAUDE = """\
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.329 (Claude Code)")
    sys.exit(0)
if "--restricted" not in args or "--permission-prompts" not in args:
    sys.exit(2)
tools = args[args.index("--tools") + 1]
if "Bash" in tools or "WebFetch" in tools:
    sys.exit(2)
mode = args[args.index("--permission-mode") + 1]
prompt = sys.stdin.read()
if mode == "acceptEdits":
    Path("claude-edit.txt").write_text("invented claude edit\\n")
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "fake claude " + mode + " saw " + str(len(prompt)),
    "usage": {"input_tokens": 3, "output_tokens": 5},
}))
sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
"""

SENTINEL = "TASK-SENTINEL-do-not-store"


class ConductorTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="firstchair-conduct-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.repo = self.temp / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("invented project\n")
        self._git("add", "README.md")
        self._git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-q", "-m", "init")
        self.bin = self.temp / "bin"
        self.bin.mkdir()
        for name, body in (("codex", FAKE_CODEX), ("grok", FAKE_GROK), ("claude", FAKE_CLAUDE)):
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n{body}")
            path.chmod(0o755)
        environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FIRSTCHAIR_WORKTREE_ROOT": str(self.temp / "worktrees"),
            "FIRSTCHAIR_SESSION": "fixture-session",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _git(self, *args):
        return subprocess.run(
            ("git", "-C", str(self.repo), *args), check=True, capture_output=True, text=True
        ).stdout

    def _main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["conduct", "--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def _delegate(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["delegate", "--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def _score(self, parts, name="score.json"):
        path = self.temp / name
        path.write_text(json.dumps({"parts": parts}), encoding="utf-8")
        return str(path)

    def _part(self, name, seat, mode="read", task=None, **extra):
        part = {"name": name, "seat": seat, "mode": mode, "task": task or f"{SENTINEL} {name}"}
        part.update(extra)
        return part

    def test_plan_resolves_self_auto_and_named_seats_without_starting_workers(self):
        score = self._score([
            self._part("own-notes", "self"),
            self._part("parser", "auto", timeout=12.5),
            self._part("contract", "claude"),
        ])
        code, output, error = self._main("plan", "--lead", "grok", "--score", score)
        self.assertEqual(code, 0, error)
        planned = json.loads(output)
        self.assertEqual(planned["lead"], "grok")
        self.assertEqual(
            [(part["seat"], part["provider"]) for part in planned["parts"]],
            [("self", "grok"), ("auto", "codex"), ("claude", "claude")],
        )
        self.assertEqual(planned["parts"][1]["timeout_seconds"], 12.5)
        self.assertEqual(planned["parts"][0]["timeout_seconds"], 600)
        self.assertNotIn(SENTINEL, output)
        self.assertFalse((self.repo / ".firstchair").exists())
        self.assertFalse((self.temp / "worktrees").exists())

        claude_lead = self._score([self._part("own", "self"), self._part("other", "auto")], "claude-lead.json")
        code, output, error = self._main("plan", "--lead", "claude", "--score", claude_lead)
        self.assertEqual(code, 0, error)
        providers = [part["provider"] for part in json.loads(output)["parts"]]
        self.assertEqual(providers, ["claude", "grok"])

    def test_plan_rejects_a_score_the_conductor_cannot_run(self):
        cases = {
            "empty": {"parts": []},
            "too many": {"parts": [self._part(f"p{index}", "self") for index in range(9)]},
            "duplicate": {"parts": [self._part("same", "self"), self._part("same", "codex")]},
            "unknown seat": {"parts": [self._part("x", "gemini")]},
            "empty task": {"parts": [self._part("x", "self", task=" ")]},
            "unknown field": {"parts": [{**self._part("x", "self"), "owner": "lead"}]},
            "bad timeout": {"parts": [self._part("x", "self", timeout=1e309)]},
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                score = self._score(payload["parts"], f"{label}.json")
                code, output, error = self._main("plan", "--lead", "grok", "--score", score)
                self.assertEqual(code, 2, output)
                self.assertEqual(output, "")
                self.assertIn("error", error)
                self.assertFalse((self.repo / ".firstchair").exists())

    def test_plan_refuses_a_missing_binary(self):
        score = self._score([self._part("contract", "claude")])
        with mock.patch.object(delegate.shutil, "which", return_value=None):
            code, output, error = self._main("plan", "--lead", "grok", "--score", score)
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("not on PATH", error)

    def test_self_is_the_lead_and_auto_can_fall_back_to_claude(self):
        def only_grok(name, *_args, **_kwargs):
            return "/fake/grok" if name == "grok" else None

        with mock.patch.object(delegate.shutil, "which", side_effect=only_grok):
            self.assertEqual(delegate.resolve_seat("self", "grok", "read"), "grok")
            with self.assertRaisesRegex(delegate.DelegateError, "allow-grok-write"):
                delegate.resolve_seat("self", "grok", "write")
            self.assertEqual(
                delegate.resolve_seat("self", "grok", "write", allow_grok_write=True), "grok",
            )
            with self.assertRaisesRegex(delegate.DelegateError, "codex is not on PATH"):
                delegate.resolve_seat("codex", "grok", "read")

        def only_claude(name, *_args, **_kwargs):
            return "/fake/claude" if name == "claude" else None

        with mock.patch.object(delegate.shutil, "which", side_effect=only_claude):
            self.assertEqual(delegate.select_worker("auto", "codex", "read"), "claude")

    def test_claude_worker_stays_inside_the_restricted_tool_list(self):
        read = delegate.Job(
            provider="claude", mode="read", name="n", repo=Path("/r"), worktree=Path("/w"),
            log_dir=Path("/l"), timeout=1,
        )
        argv, use_stdin = delegate.build_command(read, Path("/p"), Path("/o"))
        self.assertTrue(use_stdin)
        self.assertIn("--restricted", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob")

        write = delegate.Job(
            provider="claude", mode="write", name="n", repo=Path("/r"), worktree=Path("/w"),
            log_dir=Path("/l"), timeout=1, model="sonnet", effort="low",
        )
        argv, _ = delegate.build_command(write, Path("/p"), Path("/o"))
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Grep,Glob,Edit,Write")
        self.assertNotIn("Bash", argv[argv.index("--tools") + 1])
        self.assertIn("--model", argv)
        self.assertIn("--effort", argv)

    def test_score_runs_own_provider_and_the_other_two(self):
        score = self._score([
            self._part("own-notes", "self"),
            self._part("parser", "codex"),
            self._part("contract", "claude"),
        ])
        code, output, error = self._main(
            "run", "--lead", "grok", "--name", "rehearsal", "--score", score,
        )
        self.assertEqual(code, 0, error)
        summary = json.loads(output)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual([part["provider"] for part in summary["parts"]], ["grok", "codex", "claude"])
        self.assertIn("fake grok mode read", summary["parts"][0]["reply"])
        self.assertIn("fake codex saw", summary["parts"][1]["reply"])
        self.assertIn("fake claude dontAsk", summary["parts"][2]["reply"])
        self.assertEqual(summary["parts"][1]["usage"]["input_tokens"], 42)
        self.assertEqual(summary["parts"][2]["usage"], {
            "provenance": "measured", "input_tokens": 3, "output_tokens": 5,
        })
        self.assertNotIn(SENTINEL, output)
        board_path = self.repo / ".firstchair" / "conduct" / "rehearsal" / "board.json"
        board_text = board_path.read_text(encoding="utf-8")
        self.assertNotIn(SENTINEL, board_text)
        self.assertNotIn("reply", board_text)
        board = json.loads(board_text)
        self.assertEqual([part["status"] for part in board["parts"]], ["completed", "completed", "completed"])
        self.assertTrue(all("reply" not in part and "task" not in part for part in board["parts"]))
        self.assertFalse((self.temp / "worktrees" / "repo" / "own-notes").exists())

        code, output, error = self._main("status", "rehearsal")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["status"], "completed")
        self.assertNotIn(SENTINEL, output)

    def test_failure_leaves_later_parts_unstarted(self):
        score = self._score([
            self._part("own-notes", "self"),
            self._part("parser", "codex"),
            self._part("contract", "claude"),
        ])
        with mock.patch.dict(os.environ, {"FAKE_WORKER_EXIT": "3"}):
            code, output, error = self._main(
                "run", "--lead", "grok", "--name", "stopped", "--score", score,
            )
        self.assertEqual(code, 1, error)
        summary = json.loads(output)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(
            [part["status"] for part in summary["parts"]],
            ["completed", "failed", "not_started"],
        )
        self.assertFalse((self.repo / ".firstchair" / "delegations" / "contract").exists())
        self.assertFalse((self.temp / "worktrees" / "repo" / "contract").exists())
        code, output, _ = self._main("status", "stopped")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["parts"][2]["status"], "not_started")

    def _stamps(self):
        stamps = self.temp / "stamps"
        stamps.mkdir()
        return stamps

    def _assert_nothing_started(self, score_name):
        self.assertFalse((self.repo / ".firstchair" / "conduct" / score_name).exists())
        self.assertFalse((self.repo / ".firstchair" / "delegations").exists())
        self.assertFalse((self.temp / "worktrees").exists())

    def test_invalid_limit_flag_creates_no_board(self):
        score = self._score([self._part("one", "codex"), self._part("two", "codex")])
        for value in ("0", "5", "-1", "true", "True", "1.5", "2.0", "two", "", " 2", "1e0"):
            with self.subTest(value=value):
                code, output, error = self._main(
                    "run", "--lead", "grok", "--name", "bad-limit", "--score", score, f"--limit={value}",
                )
                self.assertEqual(code, 2, output)
                self.assertEqual(output, "")
                self.assertIn("limit must be an integer from 1 through 4", error)
                self._assert_nothing_started("bad-limit")

    def test_invalid_score_limit_or_key_creates_no_board(self):
        parts = [self._part("one", "codex"), self._part("two", "codex")]
        cases = {
            "zero": {"parts": parts, "limit": 0},
            "five": {"parts": parts, "limit": 5},
            "bool": {"parts": parts, "limit": True},
            "float": {"parts": parts, "limit": 2.0},
            "text": {"parts": parts, "limit": "2"},
            "null": {"parts": parts, "limit": None},
            "other-key": {"parts": parts, "workers": 2},
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                path = self.temp / f"limit-{label}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                # A valid flag does not rescue a score this conductor refuses.
                for extra in ((), ("--limit", "2")):
                    code, output, error = self._main(
                        "run", "--lead", "grok", "--name", "bad-score", "--score", str(path), *extra,
                    )
                    self.assertEqual(code, 2, output)
                    self.assertEqual(output, "")
                    self.assertIn("error", error)
                    self._assert_nothing_started("bad-score")

    def test_limit_two_overlaps_two_parts_and_keeps_input_order(self):
        stamps = self._stamps()
        score = self._score([self._part("alpha", "codex"), self._part("beta", "codex")])
        with mock.patch.dict(os.environ, {"FAKE_STAMP_DIR": str(stamps), "FAKE_PEER_WAIT": "20"}):
            code, output, error = self._main(
                "run", "--lead", "grok", "--name", "pair", "--score", score, "--limit", "2",
            )
        self.assertEqual(code, 0, error)
        # Each fake saw the other started and unfinished while it was running.
        self.assertTrue((stamps / "alpha.overlap").exists())
        self.assertTrue((stamps / "beta.overlap").exists())
        summary = json.loads(output)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["limit"], 2)
        self.assertEqual([part["name"] for part in summary["parts"]], ["alpha", "beta"])
        self.assertTrue(all("fake codex saw" in part["reply"] for part in summary["parts"]))
        self.assertNotIn(SENTINEL, output)
        board_text = (self.repo / ".firstchair" / "conduct" / "pair" / "board.json").read_text(encoding="utf-8")
        self.assertNotIn(SENTINEL, board_text)
        self.assertNotIn("reply", board_text)
        board = json.loads(board_text)
        self.assertEqual(board["limit"], 2)
        self.assertEqual([part["name"] for part in board["parts"]], ["alpha", "beta"])
        self.assertEqual([part["status"] for part in board["parts"]], ["completed", "completed"])
        self.assertTrue(all("reply" not in part and "task" not in part for part in board["parts"]))

    def test_score_limit_runs_parts_together_and_the_flag_overrides_it(self):
        parts = [self._part("alpha", "codex"), self._part("beta", "codex")]
        path = self.temp / "limited.json"
        path.write_text(json.dumps({"parts": parts, "limit": 2}), encoding="utf-8")
        stamps = self._stamps()
        with mock.patch.dict(os.environ, {"FAKE_STAMP_DIR": str(stamps), "FAKE_PEER_WAIT": "0.3"}):
            code, output, error = self._main(
                "run", "--lead", "grok", "--name", "flagged", "--score", str(path), "--limit", "1",
            )
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["limit"], 1)
        # Sequential parts never see an unfinished peer.
        self.assertEqual(sorted(item.name for item in stamps.glob("*.end")), ["alpha.end", "beta.end"])
        self.assertEqual(list(stamps.glob("*.overlap")), [])

        shutil.rmtree(stamps)
        stamps.mkdir()
        parts = [self._part("gamma", "codex"), self._part("delta", "codex")]
        path.write_text(json.dumps({"parts": parts, "limit": 2}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"FAKE_STAMP_DIR": str(stamps), "FAKE_PEER_WAIT": "20"}):
            code, output, error = self._main(
                "run", "--lead", "grok", "--name", "scored", "--score", str(path),
            )
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["limit"], 2)
        self.assertTrue((stamps / "gamma.overlap").exists())
        self.assertTrue((stamps / "delta.overlap").exists())

    def test_limit_two_failure_starts_no_later_wave(self):
        score = self._score([
            self._part("parser", "codex"),
            self._part("own-notes", "self"),
            self._part("contract", "claude"),
        ])
        with mock.patch.dict(os.environ, {"FAKE_WORKER_EXIT": "3"}):
            code, output, error = self._main(
                "run", "--lead", "grok", "--name", "wave-stop", "--score", score, "--limit", "2",
            )
        self.assertEqual(code, 1, error)
        summary = json.loads(output)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual([part["name"] for part in summary["parts"]], ["parser", "own-notes", "contract"])
        self.assertEqual(
            [part["status"] for part in summary["parts"]],
            ["failed", "completed", "not_started"],
        )
        self.assertEqual(summary["parts"][0]["exit_code"], 3)
        self.assertIn("fake grok mode read", summary["parts"][1]["reply"])
        self.assertFalse((self.repo / ".firstchair" / "delegations" / "contract").exists())
        self.assertFalse((self.temp / "worktrees" / "repo" / "contract").exists())
        self.assertTrue((self.repo / ".firstchair" / "delegations" / "own-notes").is_dir())
        code, output, _ = self._main("status", "wave-stop")
        self.assertEqual(code, 1)
        board = json.loads(output)
        self.assertEqual(
            [part["status"] for part in board["parts"]],
            ["failed", "completed", "not_started"],
        )
        self.assertNotIn(SENTINEL, output)
        self.assertNotIn("reply", output)

    def test_earliest_failed_part_sets_the_score_exit_code(self):
        (self.bin / "grok").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
        score = self._score([
            self._part("slow", "grok", timeout=0.5),
            self._part("parser", "codex"),
        ])
        with mock.patch.dict(os.environ, {"FAKE_WORKER_EXIT": "3"}):
            code, output, error = self._main(
                "run", "--lead", "claude", "--name", "earliest", "--score", score, "--limit", "2",
            )
        # The codex part fails first in time. The timed-out part is first in input order.
        self.assertEqual(code, 124, error)
        summary = json.loads(output)
        self.assertEqual([part["status"] for part in summary["parts"]], ["timeout", "failed"])

    def _board(self, name):
        return json.loads(
            (self.repo / ".firstchair" / "conduct" / name / "board.json").read_text(encoding="utf-8")
        )

    def _echo_grok(self):
        # Reports whether the earlier reply reached its prompt, without repeating it.
        (self.bin / "grok").write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "prompt = Path(args[args.index('--prompt-file') + 1]).read_text()\n"
            "seen = 'fake codex saw' in prompt and '<earlier-reply>' in prompt\n"
            "print(json.dumps({'text': 'follow-up saw earlier reply: ' + str(seen), 'stopReason': 'end_turn'}))\n"
        )

    def test_follow_up_part_reads_the_earlier_reply_and_the_board_does_not(self):
        self._echo_grok()
        score = self._score([
            self._part("parser", "codex"),
            self._part("review", "grok", after="parser"),
            self._part("alone", "grok"),
        ])
        code, output, error = self._main("plan", "--lead", "claude", "--score", score)
        self.assertEqual(code, 0, error)
        self.assertEqual([part.get("after") for part in json.loads(output)["parts"]], [None, "parser", None])
        code, output, error = self._main("run", "--lead", "claude", "--name", "chain", "--score", score)
        self.assertEqual(code, 0, error)
        replies = [part["reply"] for part in json.loads(output)["parts"]]
        self.assertIn("follow-up saw earlier reply: True", replies[1])
        self.assertIn("follow-up saw earlier reply: False", replies[2])
        board_text = (self.repo / ".firstchair" / "conduct" / "chain" / "board.json").read_text(encoding="utf-8")
        self.assertNotIn("fake codex saw", board_text)
        self.assertNotIn("reply", board_text)
        self.assertNotIn(SENTINEL, board_text)
        self.assertEqual(json.loads(board_text)["parts"][1]["after"], "parser")
        for name in ("parser", "review"):
            log_dir = self.repo / ".firstchair" / "delegations" / name
            for item in log_dir.iterdir():
                self.assertNotIn("fake codex saw", item.read_text(encoding="utf-8"))

    def test_after_must_name_an_earlier_part_in_an_earlier_wave(self):
        cases = {
            "later": [self._part("a", "codex", after="b"), self._part("b", "codex")],
            "itself": [self._part("a", "codex", after="a")],
            "unknown": [self._part("a", "codex"), self._part("b", "codex", after="z")],
            "not-text": [self._part("a", "codex"), self._part("b", "codex", after=1)],
            "null": [self._part("a", "codex"), self._part("b", "codex", after=None)],
        }
        for label, parts in cases.items():
            with self.subTest(label=label):
                score = self._score(parts, f"after-{label}.json")
                code, output, error = self._main("run", "--lead", "grok", "--name", "bad-after", "--score", score)
                self.assertEqual(code, 2, output)
                self.assertIn("must name an earlier part", error)
                self._assert_nothing_started("bad-after")
        score = self._score([self._part("a", "codex"), self._part("b", "codex", after="a")])
        code, output, error = self._main(
            "run", "--lead", "grok", "--name", "same-wave", "--score", score, "--limit", "2",
        )
        self.assertEqual(code, 2, output)
        self.assertIn("runs at the same time", error)
        self._assert_nothing_started("same-wave")

    def test_follow_up_fails_when_the_earlier_part_returned_no_reply(self):
        (self.bin / "grok").write_text(
            f"#!{sys.executable}\nimport json\nprint(json.dumps({{'text': '', 'stopReason': 'end_turn'}}))\n"
        )
        score = self._score([self._part("quiet", "grok"), self._part("review", "codex", after="quiet")])
        code, output, error = self._main("run", "--lead", "claude", "--name", "quiet", "--score", score)
        self.assertEqual(code, 1, error)
        parts = json.loads(output)["parts"]
        self.assertEqual([part["status"] for part in parts], ["completed", "failed"])
        self.assertIn("returned no reply", parts[1]["error"])
        self.assertFalse((self.repo / ".firstchair" / "delegations" / "review").exists())

    def _slow_grok(self, stamps):
        (self.bin / "grok").write_text(
            f"#!{sys.executable}\n"
            "import os, time\n"
            "from pathlib import Path\n"
            f"Path({str(stamps / 'slow.pid')!r}).write_text(str(os.getpid()))\n"
            "time.sleep(120)\n"
        )

    def _start_conductor(self, *argv):
        source = str(Path(delegate.__file__).resolve().parents[1])
        env = {**os.environ, "PYTHONPATH": source}
        process = subprocess.Popen(
            [sys.executable, "-m", "firstchair", "conduct", "--repo", str(self.repo), *argv],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
        )
        self.addCleanup(process.communicate)
        self.addCleanup(process.kill)
        return process

    def _wait_for_pid(self, stamps):
        deadline = time.monotonic() + 30
        path = stamps / "slow.pid"
        while time.monotonic() < deadline:
            text = path.read_text() if path.exists() else ""
            if text:
                return int(text)
            time.sleep(0.05)
        self.fail("the slow worker did not start")

    def _alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    @unittest.skipUnless(os.name == "posix", "needs POSIX locks and process groups")
    def test_cancel_stops_the_running_part_of_a_live_score(self):
        stamps = self._stamps()
        self._slow_grok(stamps)
        score = self._score([self._part("slow", "grok"), self._part("later", "codex")])
        process = self._start_conductor("run", "--lead", "claude", "--name", "live", "--score", score)
        pid = self._wait_for_pid(stamps)
        code, output, _ = self._main("status", "live")
        self.assertEqual(code, 1)
        shown = json.loads(output)
        self.assertEqual(shown["conductor"], "live")
        self.assertEqual(shown["parts"][0]["status"], "running")
        self.assertIsInstance(shown["parts"][0]["elapsed_seconds"], int)
        self.assertLessEqual(shown["parts"][0]["elapsed_seconds"], shown["parts"][0]["timeout_seconds"])
        self.assertNotIn("elapsed_seconds", shown["parts"][1])
        self.assertNotIn("conductor", self._board("live"))
        code, _, error = self._main("resume", "--name", "live", "--score", score)
        self.assertEqual(code, 2)
        self.assertIn("live conductor", error)
        code, output, error = self._main("cancel", "live")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output), {"name": "live", "status": "cancel_requested"})
        output, error = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 2, error)
        summary = json.loads(output)
        self.assertEqual(summary["status"], "cancelled")
        self.assertEqual([part["status"] for part in summary["parts"]], ["cancelled", "not_started"])
        self.assertFalse(self._alive(pid))
        board = self._board("live")
        self.assertEqual(board["status"], "cancelled")
        self.assertEqual([part["status"] for part in board["parts"]], ["cancelled", "not_started"])
        self.assertNotIn("error", board["parts"][0])
        self.assertFalse((self.temp / "worktrees" / "repo" / "slow").exists())
        self.assertFalse((self.repo / ".firstchair" / "delegations" / "later").exists())
        self.assertFalse((self.repo / ".firstchair" / "conduct" / "live" / "cancel").exists())
        code, _, error = self._main("cancel", "live")
        self.assertEqual(code, 2)
        self.assertIn("is not running", error)
        code, _, error = self._main("cancel", "never-ran")
        self.assertEqual(code, 2)
        self.assertIn("no conductor score named", error)

    @unittest.skipUnless(os.name == "posix", "needs POSIX locks and process groups")
    def test_resume_after_the_conductor_died_does_not_replay_a_completed_part(self):
        stamps = self._stamps()
        self._slow_grok(stamps)
        score = self._score([
            self._part("first", "codex"), self._part("slow", "grok"), self._part("last", "codex"),
        ])
        environment = {"FAKE_STAMP_DIR": str(stamps)}
        with mock.patch.dict(os.environ, environment):
            process = self._start_conductor("run", "--lead", "claude", "--name", "died", "--score", score)
        pid = self._wait_for_pid(stamps)
        process.kill()
        process.communicate(timeout=30)
        os.kill(pid, signal.SIGKILL)
        self.assertTrue((stamps / "first.end").exists())
        board = self._board("died")
        self.assertEqual(board["status"], "running")
        self.assertEqual([part["status"] for part in board["parts"]], ["completed", "running", "queued"])
        self.assertTrue((self.temp / "worktrees" / "repo" / "slow").exists())
        code, output, _ = self._main("status", "died")
        self.assertEqual(code, 1)
        shown = json.loads(output)
        self.assertEqual(shown["conductor"], "gone")
        self.assertIn("elapsed_seconds", shown["parts"][1])
        self.assertIn("ended_at", shown["parts"][0])
        code, _, error = self._main("cancel", "died")
        self.assertEqual(code, 2)
        self.assertIn("no live conductor", error)

        changed = self._score([
            self._part("first", "codex"), self._part("slow", "codex"), self._part("last", "codex"),
        ], "changed.json")
        for other in (changed, self._score([self._part("first", "codex")], "short.json")):
            code, _, error = self._main("resume", "--name", "died", "--score", other)
            self.assertEqual(code, 2)
            self.assertIn("does not match the saved parts", error)
        self.assertEqual(self._board("died")["status"], "running")

        shutil.rmtree(stamps)
        stamps.mkdir()
        path = self.bin / "grok"
        path.write_text(f"#!{sys.executable}\n{FAKE_GROK}")
        with mock.patch.dict(os.environ, environment):
            code, output, error = self._main("resume", "--name", "died", "--score", score)
        self.assertEqual(code, 0, error)
        summary = json.loads(output)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual([part["status"] for part in summary["parts"]], ["completed"] * 3)
        self.assertNotIn("reply", summary["parts"][0])
        self.assertIn("fake grok mode read", summary["parts"][1]["reply"])
        # The completed part did not run again. The part after the dead one did.
        self.assertFalse((stamps / "first.start").exists())
        self.assertTrue((stamps / "last.end").exists())
        self.assertEqual(self._board("died")["status"], "completed")
        self.assertFalse((self.temp / "worktrees" / "repo" / "slow").exists())
        code, _, error = self._main("resume", "--name", "died", "--score", score)
        self.assertEqual(code, 2)
        self.assertIn("already completed", error)

    @unittest.skipUnless(os.name == "posix", "needs POSIX signals and process groups")
    def test_sigterm_stops_every_part_running_together(self):
        stamps = self._stamps()
        self._slow_grok(stamps)
        score = self._score([
            self._part("slow-a", "grok"), self._part("slow-b", "grok"), self._part("later", "codex"),
        ])
        process = self._start_conductor(
            "run", "--lead", "claude", "--name", "stopped", "--score", score, "--limit", "2",
        )
        self._wait_for_pid(stamps)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not all(
            (self.temp / "worktrees" / "repo" / name).exists() for name in ("slow-a", "slow-b")
        ):
            time.sleep(0.05)
        process.terminate()
        output, error = process.communicate(timeout=30)
        self.assertEqual(process.returncode, 2, error)
        board = self._board("stopped")
        self.assertEqual(board["status"], "failed")
        self.assertEqual([part["status"] for part in board["parts"]], ["failed", "failed", "not_started"])
        self.assertIn("SIGTERM", board["parts"][0]["error"])
        self.assertIn("SIGTERM", board["parts"][1]["error"])
        for name in ("slow-a", "slow-b", "later"):
            self.assertFalse((self.temp / "worktrees" / "repo" / name).exists())

    def test_another_session_cannot_cancel_or_resume_a_score_or_finish_its_parts(self):
        score = self._score([self._part("first", "codex"), self._part("edit", "claude", mode="write")])
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_EXIT": "1"}):
            code, output, error = self._main("run", "--lead", "grok", "--name", "mine", "--score", score)
        self.assertEqual(code, 1, error)
        self.assertEqual(json.loads(output)["session"], "fixture-session")
        before = self._board("mine")
        self.assertEqual(before["session"], "fixture-session")
        for name in ("first", "edit"):
            self.assertEqual(
                (self.repo / ".firstchair" / "delegations" / name / "session").read_text(),
                "fixture-session\n",
            )
        worktree = self.temp / "worktrees" / "repo" / "edit"
        for session in ("other-session", None):
            with self.subTest(session=session), mock.patch.dict(os.environ, {"FIRSTCHAIR_SESSION": session or "x"}):
                if session is None:
                    del os.environ["FIRSTCHAIR_SESSION"]
                for command in (("cancel", "mine"), ("resume", "--name", "mine", "--score", score)):
                    code, output, error = self._main(*command)
                    self.assertEqual(code, 2, output)
                    self.assertIn("score mine belongs to session fixture-session", error)
                code, _, error = self._delegate("finish", "edit", "--delete-branch", "--force")
                self.assertEqual(code, 2)
                self.assertIn("belongs to session fixture-session", error)
                # Reading another session's board is allowed.
                code, output, _ = self._main("status", "mine")
                self.assertEqual(json.loads(output)["session"], "fixture-session")
            self.assertEqual(self._board("mine"), before)
            self.assertTrue((worktree / "claude-edit.txt").exists())
        with mock.patch.dict(os.environ, {"FIRSTCHAIR_SESSION": "other-session"}):
            code, _, error = self._delegate("--session", "fixture-session", "finish", "edit", "--delete-branch", "--force")
            self.assertEqual(code, 0, error)
            code, output, error = self._main("--session", "fixture-session", "resume", "--name", "mine", "--score", score)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["session"], "fixture-session")
        self.assertEqual(self._board("mine")["session"], "fixture-session")

    def test_a_score_without_a_session_makes_one_and_refuses_a_bad_board_session(self):
        score = self._score([self._part("first", "codex"), self._part("second", "claude")])
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_EXIT": "1"}):
            del os.environ["FIRSTCHAIR_SESSION"]
            code, output, error = self._main("run", "--lead", "grok", "--name", "anon", "--score", score)
            self.assertEqual(code, 1, error)
            made = json.loads(output)["session"]
            self.assertRegex(made, r"^s-[0-9a-f]{12}$")
            code, _, error = self._main("resume", "--name", "anon", "--score", score)
            self.assertEqual(code, 2)
            self.assertIn(f"pass --session {made}", error)
        path = self.repo / ".firstchair" / "conduct" / "anon" / "board.json"
        board = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**board, "session": "bad session; rm"}), encoding="utf-8")
        code, _, error = self._main("resume", "--name", "anon", "--score", score)
        self.assertEqual(code, 2)
        self.assertIn("unreadable session ID", error)
        self.assertNotIn("rm", error)

    def test_resume_refuses_an_unreviewed_write_part_until_the_lead_finishes_it(self):
        score = self._score([self._part("first", "codex"), self._part("edit", "claude", mode="write")])
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_EXIT": "1"}):
            code, output, error = self._main("run", "--lead", "grok", "--name", "wrote", "--score", score)
        self.assertEqual(code, 1, error)
        worktree = self.temp / "worktrees" / "repo" / "edit"
        self.assertTrue((worktree / "claude-edit.txt").exists())
        before = self._board("wrote")
        code, _, error = self._main("resume", "--name", "wrote", "--score", score)
        self.assertEqual(code, 2)
        self.assertIn("left a worktree or branch", error)
        self.assertTrue((worktree / "claude-edit.txt").exists())
        self.assertEqual(self._board("wrote"), before)
        code, _, error = self._delegate("finish", "edit", "--delete-branch", "--force")
        self.assertEqual(code, 0, error)
        code, output, error = self._main("resume", "--name", "wrote", "--score", score)
        self.assertEqual(code, 0, error)
        self.assertEqual([part["status"] for part in json.loads(output)["parts"]], ["completed", "completed"])
        self.assertTrue((worktree / "claude-edit.txt").exists())

    def test_resume_reruns_a_read_part_whose_reply_a_follow_up_needs(self):
        self._echo_grok()
        stamps = self._stamps()
        score = self._score([
            self._part("other", "codex"),
            self._part("parser", "codex"),
            self._part("review", "grok", after="parser"),
            self._part("verdict", "claude", after="review"),
        ])
        environment = {"FAKE_STAMP_DIR": str(stamps)}
        with mock.patch.dict(os.environ, {**environment, "FAKE_CLAUDE_EXIT": "1"}):
            code, _, error = self._main("run", "--lead", "claude", "--name", "chain", "--score", score)
        self.assertEqual(code, 1, error)
        self.assertEqual(
            [part["status"] for part in self._board("chain")["parts"]],
            ["completed", "completed", "completed", "failed"],
        )
        shutil.rmtree(stamps)
        stamps.mkdir()
        with mock.patch.dict(os.environ, environment):
            code, output, error = self._main("resume", "--name", "chain", "--score", score)
        self.assertEqual(code, 0, error)
        parts = json.loads(output)["parts"]
        self.assertEqual([part["status"] for part in parts], ["completed"] * 4)
        # The whole read chain behind the failed part ran again. The unrelated part did not.
        self.assertFalse((stamps / "other.start").exists())
        self.assertTrue((stamps / "parser.start").exists())
        self.assertIn("follow-up saw earlier reply: True", parts[2]["reply"])
        self.assertIn("fake claude dontAsk saw", parts[3]["reply"])
        board_text = (self.repo / ".firstchair" / "conduct" / "chain" / "board.json").read_text(encoding="utf-8")
        self.assertNotIn("reply", board_text)
        self.assertNotIn("fake codex saw", board_text)

    def test_resume_refuses_a_follow_up_whose_earlier_write_reply_is_gone(self):
        score = self._score([
            self._part("parser", "codex", mode="write"), self._part("review", "claude", after="parser"),
        ])
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_EXIT": "1"}):
            code, _, error = self._main("run", "--lead", "grok", "--name", "lost", "--score", score)
        self.assertEqual(code, 1, error)
        before = self._board("lost")
        code, _, error = self._main("resume", "--name", "lost", "--score", score)
        self.assertEqual(code, 2)
        self.assertIn("was not saved", error)
        self.assertEqual(self._board("lost"), before)
        self.assertTrue((self.repo / ".firstchair" / "delegations" / "review").is_dir())
        self.assertTrue((self.temp / "worktrees" / "repo" / "parser" / "added.txt").exists())

    def test_resume_refuses_a_malformed_board(self):
        score = self._score([self._part("first", "codex"), self._part("second", "claude")])
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_EXIT": "1"}):
            self._main("run", "--lead", "grok", "--name", "bent", "--score", score)
        path = self.repo / ".firstchair" / "conduct" / "bent" / "board.json"
        good = json.loads(path.read_text(encoding="utf-8"))
        for label, change in (
            ("schema", {"schema_version": 2}), ("lead", {"lead": "gemini"}), ("parts", {"parts": "none"}),
            ("status", {"parts": [good["parts"][0], {**good["parts"][1], "status": "done"}]}),
        ):
            with self.subTest(label=label):
                path.write_text(json.dumps({**good, **change}), encoding="utf-8")
                code, output, error = self._main("resume", "--name", "bent", "--score", score)
                self.assertEqual(code, 2, output)
                self.assertIn("error", error)
                self.assertTrue((self.repo / ".firstchair" / "delegations" / "second").is_dir())

    def test_grok_write_part_is_refused_without_the_explicit_flag(self):
        for label, part, lead in (
            ("named", self._part("edit", "grok", mode="write"), "claude"),
            ("self", self._part("edit", "self", mode="write"), "grok"),
        ):
            with self.subTest(label=label):
                score = self._score([self._part("first", "codex"), part], f"grok-{label}.json")
                for command in (("plan",), ("run", "--name", "grok-write")):
                    code, output, error = self._main(*command, "--lead", lead, "--score", score)
                    self.assertEqual(code, 2, output)
                    self.assertEqual(output, "")
                    self.assertIn("--allow-grok-write", error)
                    self._assert_nothing_started("grok-write")
        code, output, error = self._main(
            "run", "--lead", "grok", "--name", "grok-write", "--score", score, "--allow-grok-write",
        )
        self.assertEqual(code, 0, error)
        self.assertIn("fake grok mode write", json.loads(output)["parts"][1]["reply"])
        self.assertEqual((self.repo / "README.md").read_text(), "invented project\n")

    def test_a_score_can_pick_each_parts_account_from_real_usage(self):
        from firstchair.accounts import account_environment
        from firstchair.quota import Quota, Window

        state = self.temp / "xdg"
        state.mkdir(mode=0o700)

        def reader(provider, profile, **_):
            return Quota((Window(10 if profile == "spare" else 60, 300, 2_000_000_000),), True, time.time())

        score = self._score([self._part("first", "codex"), self._part("second", "grok")])
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}), \
                mock.patch.object(delegate, "read_quota", side_effect=reader):
            account_environment("codex", "spare", create=True)
            code, output, error = self._main(
                "run", "--lead", "claude", "--name", "pooled", "--score", score, "--rotate-accounts",
            )
        self.assertEqual(code, 0, error)
        parts = json.loads(output)["parts"]
        self.assertEqual([part["account"] for part in parts], ["codex:spare", "grok"])
        self.assertEqual([part["account"] for part in self._board("pooled")["parts"]], ["codex:spare", "grok"])

    def test_malformed_claude_reply_fails_closed(self):
        (self.bin / "claude").write_text(f"#!{sys.executable}\nimport sys\nprint('{{}}')\nsys.exit(0)\n")
        score = self._score([self._part("contract", "claude")])
        code, output, error = self._main("run", "--lead", "grok", "--name", "bad-claude", "--score", score)
        self.assertEqual(code, 1, error)
        summary = json.loads(output)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["parts"][0]["status"], "failed")
        self.assertNotIn("reply", summary["parts"][0])
        self.assertFalse((self.temp / "worktrees" / "repo" / "contract").exists())

    def test_claude_write_stays_in_its_worktree(self):
        code, output, error = self._delegate(
            "run", "claude", "--lead", "grok", "--mode", "write", "--name", "cwrite", "Edit the notes",
        )
        self.assertEqual(code, 0, error)
        summary = json.loads(output)
        self.assertEqual(summary["provider"], "claude")
        self.assertIn("acceptEdits", summary["reply"])
        self.assertEqual(summary["usage"]["provenance"], "measured")
        self.assertFalse((self.repo / "claude-edit.txt").exists())
        edited = self.temp / "worktrees" / "repo" / "cwrite" / "claude-edit.txt"
        self.assertEqual(edited.read_text(encoding="utf-8"), "invented claude edit\n")
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "invented project\n")

    def test_self_dispatch_uses_the_lead_provider(self):
        code, output, error = self._delegate("run", "self", "--lead", "grok", "--name", "mine", "Look")
        self.assertEqual(code, 0, error)
        summary = json.loads(output)
        self.assertEqual(summary["requested_provider"], "self")
        self.assertEqual(summary["provider"], "grok")
        self.assertIn("fake grok mode read", summary["reply"])

        code, output, error = self._delegate(
            "run", "self", "--lead", "grok", "--model", "synthetic", "Look",
        )
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("explicit worker", error)

    def test_run_refuses_a_reused_desk_or_part_before_starting_work(self):
        score = self._score([self._part("own-notes", "self"), self._part("parser", "codex")])
        self.assertEqual(self._main("run", "--lead", "grok", "--name", "desk", "--score", score)[0], 0)
        code, output, error = self._main("run", "--lead", "grok", "--name", "desk", "--score", score)
        self.assertEqual(code, 2)
        self.assertIn("already exists", error)
        self.assertEqual(json.loads((self.repo / ".firstchair" / "conduct" / "desk" / "board.json").read_text())["status"], "completed")

        blocked = self.repo / ".firstchair" / "delegations" / "parser"
        self.assertTrue(blocked.is_dir())
        fresh = self._score([self._part("fresh", "self"), self._part("parser", "codex")], "blocked.json")
        code, output, error = self._main("run", "--lead", "grok", "--name", "second", "--score", fresh)
        self.assertEqual(code, 2)
        self.assertIn("parser", error)
        self.assertFalse((self.repo / ".firstchair" / "conduct" / "second").exists())
        self.assertFalse((self.temp / "worktrees" / "repo" / "own-notes").exists())

    def test_with_changes_reaches_a_write_part_without_touching_the_lead_tree(self):
        (self.repo / "README.md").write_text("lead edit\n")
        score = self._score([self._part("parser", "codex", mode="write")])
        code, output, error = self._main(
            "run", "--lead", "grok", "--name", "carry", "--score", score, "--with-changes",
        )
        self.assertEqual(code, 0, error)
        worktree = self.temp / "worktrees" / "repo" / "parser"
        self.assertEqual((worktree / "README.md").read_text(encoding="utf-8"), "lead edit\n")
        self.assertEqual((worktree / "added.txt").read_text(encoding="utf-8"), "invented change\n")
        self.assertFalse((self.repo / "added.txt").exists())
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "lead edit\n")


if __name__ == "__main__":
    unittest.main()

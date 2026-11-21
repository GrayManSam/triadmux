"""Exercise firstchair delegate with invented worker executables in a temporary repository."""

import contextlib
import io
import json
import os
import shutil
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
    print("codex-cli " + os.environ.get("FAKE_CODEX_VERSION", "0.170.0"))
    sys.exit(0)
prompt = sys.stdin.read()
cwd = Path(args[args.index("--cd") + 1])
reply = Path(args[args.index("--output-last-message") + 1])
time.sleep(float(os.environ.get("FAKE_WORKER_SLEEP", "0")))
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
sys.exit(int(os.environ.get("FAKE_WORKER_EXIT", "0")))
"""

FAKE_GROK = """\
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
prompt = Path(args[args.index("--prompt-file") + 1]).read_text()
cwd = Path(args[args.index("--cwd") + 1])
if "--disallowed-tools" not in args:
    (cwd / "README.md").write_text("edited by invented grok\\n")
print(json.dumps({"text": "## Summary\\nfake grok mode " + ("read" if "--disallowed-tools" in args else "write"), "stopReason": "end_turn"}))
"""


class DelegateTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="firstchair-delegate-test-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.repo = self.temp / "repo"
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("invented project\n")
        self._git("add", "README.md")
        self._git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                  "commit", "-q", "-m", "init")
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        for name, body in (("codex", FAKE_CODEX), ("grok", FAKE_GROK)):
            path = bin_dir / name
            path.write_text(f"#!{sys.executable}\n{body}")
            path.chmod(0o755)
        environment = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
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
            code = main(["delegate", "--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_needs_no_task_and_creates_no_worktree_or_state(self):
        code, output, error = self._main("run", "auto", "--lead", "grok", "--dry-run")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["provider"], "codex")
        self.assertFalse((self.repo / ".firstchair").exists())
        self.assertFalse((self.temp / "worktrees").exists())

    def test_timeout_must_be_finite_and_positive(self):
        for timeout in ("0", "nan", "inf"):
            with self.subTest(timeout=timeout):
                code, output, error = self._main("run", "codex", "--timeout", timeout, "--dry-run")
                self.assertEqual(code, 2)
                self.assertEqual(output, "")
                self.assertIn("finite positive", error)

    def test_auto_dispatch_uses_the_active_lead_and_records_the_worker(self):
        for lead, worker in (("claude", "grok"), ("codex", "grok"), ("grok", "codex")):
            with self.subTest(lead=lead):
                code, output, error = self._main(
                    "run", "auto", "--lead", lead, "--name", lead, "Inspect the README"
                )
                self.assertEqual(code, 0, error)
                summary = json.loads(output)
                self.assertEqual(summary["lead"], lead)
                self.assertEqual(summary["requested_provider"], "auto")
                self.assertEqual(summary["provider"], worker)
                expected_usage = (
                    {"provenance": "measured", "input_tokens": 42, "output_tokens": 8}
                    if worker == "codex"
                    else {"provenance": "unknown", "input_tokens": None, "output_tokens": None}
                )
                self.assertEqual(summary["usage"], expected_usage)

    def test_auto_falls_back_only_to_an_eligible_installed_worker(self):
        with mock.patch.object(delegate.shutil, "which", side_effect={"codex": "/fake/codex", "grok": None}.get):
            self.assertEqual(delegate.select_worker("auto", "claude", "read"), "codex")
            with self.assertRaisesRegex(delegate.DelegateError, "no eligible read worker"):
                delegate.select_worker("auto", "codex", "read")
        # Automatic write selection never reaches Grok, whichever seat leads.
        with mock.patch.object(delegate.shutil, "which", side_effect={"grok": "/fake/grok"}.get):
            for lead in ("claude", "codex"):
                with self.assertRaisesRegex(delegate.DelegateError, "no eligible write worker"):
                    delegate.select_worker("auto", lead, "write")
        for order in delegate.WORKER_ORDER.values():
            self.assertNotIn("grok", order["write"])

    def test_another_session_cannot_collect_or_finish_a_job(self):
        code, out, error = self._main("run", "codex", "Edit", "--mode", "write", "--name", "owned")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(out)["session"], "fixture-session")
        listed = json.loads(self._main("list")[1])
        self.assertEqual(listed["worktrees"][0]["session"], "fixture-session")
        worktree = self.temp / "worktrees" / "repo" / "owned"
        for session in ("other-session", None):
            with self.subTest(session=session):
                environment = {} if session is None else {"FIRSTCHAIR_SESSION": session}
                with mock.patch.dict(os.environ, environment):
                    if session is None:
                        del os.environ["FIRSTCHAIR_SESSION"]
                    for command in (("collect", "owned"), ("finish", "owned", "--delete-branch", "--force")):
                        code, out, error = self._main(*command)
                        self.assertEqual(code, 2, out)
                        self.assertIn("belongs to session fixture-session", error)
                self.assertTrue((worktree / "added.txt").exists())
                self.assertEqual(self._git("log", "--oneline", "delegate/owned").count("\n"), 1)
        # Naming the owner on the command line is the deliberate way across.
        with mock.patch.dict(os.environ, {"FIRSTCHAIR_SESSION": "other-session"}):
            code, _, error = self._main("--session", "fixture-session", "collect", "owned")
            self.assertEqual(code, 0, error)
            code, _, error = self._main("--session", "Not Valid", "finish", "owned")
            self.assertEqual(code, 2)
            self.assertIn("session must be", error)

    def test_a_run_without_a_session_makes_one_and_an_older_job_has_no_owner(self):
        with mock.patch.dict(os.environ):
            del os.environ["FIRSTCHAIR_SESSION"]
            code, out, error = self._main("run", "codex", "Edit", "--mode", "write", "--name", "fresh")
            self.assertEqual(code, 0, error)
            made = json.loads(out)["session"]
            self.assertRegex(made, r"^s-[0-9a-f]{12}$")
            code, _, error = self._main("finish", "fresh", "--force")
            self.assertEqual(code, 2)
            self.assertIn(f"pass --session {made}", error)
            # A job from before sessions existed has no session file.
            (self.repo / ".firstchair" / "delegations" / "fresh" / "session").unlink()
            code, _, error = self._main("finish", "fresh", "--force", "--delete-branch")
            self.assertEqual(code, 0, error)

    def test_rotation_runs_the_worker_under_the_account_with_the_most_room(self):
        from firstchair.accounts import account_environment
        from firstchair.quota import Quota, Window

        state = self.temp / "xdg"
        state.mkdir(mode=0o700)
        used = {None: 95, "spare": 20, "full": 100}

        def reader(provider, profile, **_):
            return Quota((Window(used[profile], 300, 2_000_000_000),), True, time.time())

        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(state)}), \
                mock.patch.object(delegate, "read_quota", side_effect=reader):
            for name in ("spare", "full"):
                account_environment("codex", name, create=True)
            code, out, error = self._main("run", "codex", "Review", "--rotate-accounts", "--dry-run")
            self.assertEqual(code, 0, error)
            planned = json.loads(out)
            self.assertEqual((planned["account_selection"], planned["account"]), ("rotated", "codex:spare"))
            self.assertEqual(
                {item["account"]: item["status"] for item in planned["account_candidates"]},
                {"codex": "over_budget", "codex:spare": "ready", "codex:full": "exhausted"},
            )
            code, out, error = self._main("run", "codex", "Review", "--name", "rotated", "--rotate-accounts")
            self.assertEqual(code, 0, error)
            summary = json.loads(out)
            self.assertEqual(summary["account"], "codex:spare")
            ledger = json.loads((state / "firstchair" / "account-ledger.json").read_text())
            # The finished work is counted against the account that ran it.
            self.assertEqual(len(ledger["accounts"]["codex:spare"]["events"]), 1)
            self.assertEqual(ledger["accounts"]["codex"]["events"], [])
            self.assertNotIn("Review", json.dumps(ledger))

            code, out, error = self._main(
                "run", "codex", "Review", "--rotate-accounts", "--profile", "spare", "--dry-run",
            )
            self.assertEqual(code, 2)
            self.assertIn("leave out --profile", error)
            used.update({None: 100, "spare": 100})
            # Every account full: nothing starts.
            (state / "firstchair" / "account-ledger.json").unlink()
            code, out, error = self._main("run", "codex", "Review", "--name", "none-left", "--rotate-accounts")
            self.assertEqual(code, 2, out)
            self.assertIn("every codex account is out of usage", error)
            self.assertFalse((self.repo / ".firstchair" / "delegations" / "none-left").exists())

    def test_unknown_codex_version_does_not_report_usage(self):
        with mock.patch.dict(os.environ, {"FAKE_CODEX_VERSION": "0.0.0"}):
            code, output, error = self._main("run", "codex", "Inspect the README")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["usage"]["provenance"], "unknown")

    def test_auto_rejects_worker_specific_overrides(self):
        code, output, error = self._main("run", "auto", "--model", "synthetic", "Review")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("require an explicit worker", error)

    def test_codex_read_task_uses_read_only_sandbox_and_removes_worktree(self):
        code, out, _ = self._main("run", "codex", "Inspect the README", "--name", "scout")
        summary = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "completed")
        self.assertIn("fake codex saw", summary["reply"])
        self.assertIsNone(summary["worktree"])
        self.assertIsNone(summary["branch"])
        self.assertFalse((self.temp / "worktrees" / "repo" / "scout").exists())
        directory = Path(summary["logs"])
        self.assertEqual(sorted(path.name for path in directory.iterdir()), ["session", "summary.json"])
        self.assertEqual((directory / "session").read_text(), "fixture-session\n")
        saved = json.loads((directory / "summary.json").read_text())
        self.assertNotIn("reply", saved)
        self.assertNotIn("task", saved)

    def test_write_task_keeps_branch_then_collect_and_finish(self):
        code, out, _ = self._main("run", "codex", "Add a file", "--mode", "write", "--name", "w1")
        summary = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(summary["branch"], "delegate/w1")
        self.assertIn("?? added.txt", summary["changes"]["status"])
        self.assertFalse((self.repo / "added.txt").exists())

        code, out, err = self._main("finish", "w1")
        self.assertEqual(code, 2)
        self.assertIn("uncommitted changes", err)

        code, out, _ = self._main("collect", "w1")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["committed"])
        self.assertIn("added.txt", self._git("show", "--stat", "delegate/w1"))
        self.assertNotIn("added.txt", self._git("ls-files"))

        code, out, _ = self._main("finish", "w1", "--delete-branch", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(self._git("branch", "--list", "delegate/*").strip(), "")

    def test_grok_read_and_write_commands(self):
        code, out, _ = self._main("run", "grok", "Review", "--name", "g-read")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["reply"], "## Summary\nfake grok mode read")

        for extra in ((), ("--dry-run",)):
            code, out, error = self._main("run", "grok", "Edit", "--mode", "write", "--name", "g-write", *extra)
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertIn("--allow-grok-write", error)
        self.assertEqual(self._git("branch", "--list", "delegate/*").strip(), "")
        self.assertFalse((self.repo / ".firstchair" / "delegations" / "g-write").exists())

        code, out, _ = self._main(
            "run", "grok", "Edit", "--mode", "write", "--name", "g-write", "--allow-grok-write",
        )
        summary = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIn("M README.md", summary["changes"]["status"])
        self.assertEqual((self.repo / "README.md").read_text(), "invented project\n")
        listed = json.loads(self._main("list")[1])
        self.assertEqual([job["name"] for job in listed["worktrees"]], ["g-write"])
        self.assertTrue(listed["worktrees"][0]["dirty"])

    def test_failure_and_timeout_exit_codes(self):
        with mock.patch.dict(os.environ, {"FAKE_WORKER_EXIT": "3"}):
            code, out, _ = self._main("run", "codex", "fail", "--name", "bad")
        self.assertEqual((code, json.loads(out)["status"]), (1, "failed"))

        with mock.patch.dict(os.environ, {"FAKE_WORKER_SLEEP": "5"}):
            code, out, _ = self._main("run", "codex", "slow", "--name", "slow", "--timeout", "0.5")
        summary = json.loads(out)
        self.assertEqual((code, summary["status"]), (124, "timeout"))
        self.assertLess(summary["elapsed_seconds"], 4)

    def test_rejects_bad_names_and_reused_names(self):
        code, _, err = self._main("run", "codex", "x", "--name", "../escape")
        self.assertEqual(code, 2)
        self.assertIn("name must be", err)
        self.assertEqual(self._main("run", "codex", "x", "--name", "dup")[0], 0)
        code, _, err = self._main("run", "codex", "x", "--name", "dup")
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)

    def test_with_changes_carries_uncommitted_work(self):
        (self.repo / "README.md").write_text("lead edit\n")
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "new.md").write_text("untracked\n")
        code, out, _ = self._main(
            "run", "codex", "Edit", "--mode", "write", "--name", "carry", "--with-changes"
        )
        summary = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(sorted(summary["carried_changes"]), ["README.md", "notes/"])
        worktree = Path(summary["worktree"])
        self.assertEqual((worktree / "README.md").read_text(), "lead edit\n")
        self.assertEqual((worktree / "notes" / "new.md").read_text(), "untracked\n")
        self.assertIn("lead edit", (self.repo / "README.md").read_text())
        # Carried work is committed on the branch, so only the worker's file is pending.
        self.assertEqual(summary["changes"]["status"], "?? added.txt")
        self.assertEqual(self._git("log", "--format=%s", "-1", "delegate/carry").strip(),
                         "Carry lead's uncommitted changes into delegate carry")

    def test_carried_changes_do_not_trigger_read_warning(self):
        (self.repo / "README.md").write_text("lead edit\n")
        code, out, _ = self._main("run", "codex", "Look", "--name", "quiet", "--with-changes")
        self.assertEqual(code, 0)
        self.assertNotIn("warning", json.loads(out))

    @unittest.skipUnless(os.name == "posix", "requires symlinks")
    def test_untracked_symlink_is_copied_as_a_link(self):
        secret = self.temp / "outside.txt"
        secret.write_text("outside the checkout\n")
        (self.repo / "link.txt").symlink_to(secret)
        code, out, _ = self._main(
            "run", "codex", "Look", "--mode", "write", "--name", "link", "--with-changes"
        )
        self.assertEqual(code, 0)
        copied = Path(json.loads(out)["worktree"]) / "link.txt"
        self.assertTrue(copied.is_symlink())

    def test_refuses_foreign_worktree_under_root(self):
        root = self.temp / "worktrees" / "repo"
        root.mkdir(parents=True)
        self._git("worktree", "add", "-q", "-b", "feature", str(root / "review"), "HEAD")
        code, _, err = self._main("finish", "review", "--delete-branch", "--force")
        self.assertEqual(code, 2)
        self.assertIn("not created by delegate.py", err)
        self.assertIn("feature", self._git("branch", "--list", "feature"))

    def test_worker_that_ignores_large_prompt_is_reported_not_raised(self):
        quitter = self.temp / "bin" / "codex"
        quitter.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(5)\n")
        code, out, _ = self._main("run", "codex", "x" * 1_000_000, "--name", "quit")
        self.assertEqual((code, json.loads(out)["status"]), (1, "failed"))

    def test_relative_worktree_root_is_resolved(self):
        with mock.patch.dict(os.environ, {"FIRSTCHAIR_WORKTREE_ROOT": "rel-root"}):
            previous = Path.cwd()
            os.chdir(self.temp)
            try:
                self.assertEqual(self._main("run", "codex", "w", "--mode", "write",
                                            "--name", "rel")[0], 0)
                listed = json.loads(self._main("list")[1])
            finally:
                os.chdir(previous)
        self.assertEqual([job["name"] for job in listed["worktrees"]], ["rel"])

    def test_command_flags(self):
        job = delegate.Job(
            provider="codex", mode="write", name="n", repo=Path("/r"), worktree=Path("/w"),
            log_dir=Path("/l"), timeout=1, model="m", effort="high",
        )
        argv, use_stdin = delegate.build_command(job, Path("/p"), Path("/o"))
        self.assertTrue(use_stdin)
        self.assertIn("workspace-write", argv)
        self.assertEqual(argv[-1], "-")
        self.assertIn('model_reasoning_effort="high"', argv)
        grok = delegate.Job(
            provider="grok", mode="read", name="n", repo=Path("/r"), worktree=Path("/w"),
            log_dir=Path("/l"), timeout=1,
        )
        argv, use_stdin = delegate.build_command(grok, Path("/p"), Path("/o"))
        self.assertFalse(use_stdin)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace")
        self.assertIn("--disallowed-tools", argv)
        self.assertIn("--no-subagents", argv)


if __name__ == "__main__":
    unittest.main()

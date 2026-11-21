import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from firstchair.provider import ProviderError
from firstchair.state_listing import list_runs
from firstchair.workflow import continue_workflow, record_path, recover_workflow, run_workflow, save


class WorkflowTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "requires POSIX file locking")
    def test_recover_after_controller_crash_refuses_live_run(self):
        script = """
import sys
from pathlib import Path

from firstchair.workflow import run_workflow

run_workflow("synthetic task", Path(sys.argv[1]), str, lambda _: input(), str)
"""
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, "-m", "firstchair", "--state-dir", directory]
            with subprocess.Popen(
                [sys.executable, "-c", script, directory], stdin=subprocess.PIPE
            ) as controller:
                try:
                    deadline = time.monotonic() + 5
                    record = None
                    while time.monotonic() < deadline and controller.poll() is None:
                        for path in Path(directory).glob("*.json"):
                            snapshot = json.loads(path.read_text(encoding="utf-8"))
                            if snapshot["stage"] == "review":
                                record = snapshot
                        if record is not None:
                            break
                        time.sleep(0.01)
                    self.assertIsNotNone(record)
                    run_id = record["run_id"]
                    active = subprocess.run(
                        [*command, "recover", run_id],
                        capture_output=True, text=True, timeout=5,
                    )
                    self.assertEqual(active.returncode, 1)
                    self.assertIn("locked", json.loads(active.stderr)["error"])
                    self.assertEqual(
                        json.loads(record_path(Path(directory), run_id).read_text(encoding="utf-8")),
                        record,
                    )
                finally:
                    controller.kill()
                    controller.wait(timeout=5)

            recovered = subprocess.run(
                [*command, "recover", run_id],
                capture_output=True, text=True, timeout=5, check=True,
            )
            expected = dict(record, status="interrupted", error="controller_crash")
            self.assertEqual(json.loads(recovered.stdout), expected)
            status = subprocess.run(
                [*command, "status", run_id],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(status.returncode, 1)
            self.assertEqual(json.loads(status.stdout), expected)
            with self.assertRaisesRegex(ValueError, "only running records"):
                recover_workflow(Path(directory), run_id)

    @unittest.skipUnless(os.name == "posix", "requires POSIX file locking")
    def test_recovery_preserves_legacy_and_unsupported_records(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            run_id = str(uuid4())
            path = record_path(state_dir, run_id)
            legacy = dict(schema_version=1, run_id=run_id, status="running", stage="plan")
            save(path, legacy)
            with self.assertRaisesRegex(ValueError, "no recovery lock"):
                recover_workflow(state_dir, run_id)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), legacy)

            unsupported = dict(legacy, schema_version=2)
            save(path, unsupported)
            path.with_suffix(".lock").touch()
            with self.assertRaisesRegex(ValueError, "unsupported run record"):
                recover_workflow(state_dir, run_id)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), unsupported)

            path.write_text("{", encoding="utf-8")
            with self.assertRaises(json.JSONDecodeError):
                recover_workflow(state_dir, run_id)
            self.assertEqual(path.read_text(encoding="utf-8"), "{")

    def test_dry_run_reports_limits_without_executing_or_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory, "state")
            command = [sys.executable, "-m", "firstchair", "--state-dir", str(state_dir)]
            output = subprocess.check_output(
                [*command, "run", "--provider", "synthetic", "--dry-run", "--timeout", "2",
                 "--total-timeout", "3", "--max-output-bytes", "1", "--max-input-bytes", "2",
                 "private synthetic prompt"],
                text=True, timeout=10,
            )
            self.assertEqual(json.loads(output), {
                "status": "planned",
                "provider": "synthetic",
                "selection": "explicit",
                "routing": {
                    "demo": "requires_explicit_selection",
                    "synthetic": "selected",
                    "codex": "adapter_unavailable",
                    "claude": "adapter_unavailable",
                    "grok": "adapter_unavailable",
                },
                "stages": ["plan", "review", "synthesize"],
                "limits": {
                    "stage_timeout": 2.0,
                    "stage_output_bytes": 1,
                    "stage_input_bytes": 2,
                    "total_timeout": 3.0,
                },
                "workspace": "temporary",
            })
            self.assertFalse(state_dir.exists())

    def test_cli_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, "-m", "firstchair", "--state-dir", directory]
            output = subprocess.check_output(
                [*command, "run", "--demo", "Review the project README"], text=True
            )
            run = json.loads(output)
            self.assertEqual(run["status"], "completed")
            self.assertIn("repeatable validation", run["result"])
            saved = subprocess.check_output([*command, "status", run["run_id"]], text=True)
            self.assertEqual(json.loads(saved), run)

    def test_failed_review_stops_workflow_and_records_failure(self):
        def fail(_):
            raise RuntimeError("private provider diagnostic")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            record = run_workflow("task", state_dir, str, fail, self.fail)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["stage"], "review")
            saved = record_path(state_dir, record["run_id"]).read_text()
            self.assertNotIn("private provider diagnostic", saved)

    def test_invalid_run_id(self):
        with self.assertRaises(ValueError):
            record_path(Path("."), "../outside")

    def test_usage_failure_resumes_without_repeating_the_finished_stage(self):
        calls = []

        def plan(task):
            calls.append(("plan", task))
            return "saved plan"

        def fail(_text):
            calls.append("fail")
            raise ProviderError("rate_limited")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            failed = run_workflow("keep going", state_dir, plan, fail, self.fail)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["stage_outputs"]["plan"], "saved plan")
            self.assertNotIn("keep going", json.dumps(list_runs(state_dir)))

            def review(text):
                calls.append(("review", text))
                return "saved review"

            def synthesize(text):
                calls.append(("synthesize", text))
                return "finished"

            resumed = continue_workflow(
                state_dir, failed["run_id"], self.fail, review, synthesize,
            )
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["result"], "finished")
            self.assertEqual(resumed["stage_outputs"]["review"], "saved review")
            self.assertEqual(calls, [("plan", "keep going"), "fail", ("review", "saved plan"), ("synthesize", "saved review")])

    def test_interrupt_keeps_finished_stages_for_an_abandoned_resume(self):
        calls = []

        def plan(_task):
            calls.append("plan")
            return "saved plan"

        def stop(_text):
            calls.append("stop")
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            stopped = run_workflow("task", state_dir, plan, stop, self.fail)
            self.assertEqual(stopped["status"], "interrupted")
            self.assertEqual(stopped["stage"], "review")
            self.assertNotIn("error", stopped)
            path = record_path(state_dir, stopped["run_id"])
            abandoned = json.loads(path.read_text(encoding="utf-8"))
            abandoned["status"] = "running"
            path.write_text(json.dumps(abandoned), encoding="utf-8")

            def review(text):
                calls.append(("review", text))
                return "saved review"

            resumed = continue_workflow(
                state_dir, stopped["run_id"], self.fail, review, lambda text: f"done:{text}",
            )
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["result"], "done:saved review")
            self.assertEqual(calls, ["plan", "stop", ("review", "saved plan")])

    def test_authorization_failure_cannot_be_resumed(self):
        def fail(_text):
            raise ProviderError("authorization_failed")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            failed = run_workflow("task", state_dir, str, fail, self.fail)
            self.assertEqual(failed["error"], "authorization_failed")
            with self.assertRaisesRegex(ValueError, "usage failure"):
                continue_workflow(state_dir, failed["run_id"], self.fail, self.fail, self.fail)
            self.assertEqual(
                json.loads(record_path(state_dir, failed["run_id"]).read_text(encoding="utf-8"))["status"],
                "failed",
            )

    def test_invalid_saved_output_leaves_a_usage_failure_unchanged(self):
        def fail(_text):
            raise ProviderError("rate_limited")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            failed = run_workflow("task", state_dir, lambda _task: "saved plan", fail, self.fail)
            path = record_path(state_dir, failed["run_id"])
            record = json.loads(path.read_text(encoding="utf-8"))
            record["stage_outputs"]["plan"] = 1
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid stage output"):
                continue_workflow(state_dir, failed["run_id"], self.fail, self.fail, self.fail)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["error"], "rate_limited")

    def test_details_failure_still_records_the_provider_error(self):
        def fail(_text):
            raise ProviderError("authorization_failed")

        def boom() -> dict[str, object]:
            raise KeyError("usage")

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            record = run_workflow("task", state_dir, fail, self.fail, self.fail, details=boom)
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["error"], "authorization_failed")
            saved = json.loads(record_path(state_dir, record["run_id"]).read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertEqual(saved["error"], "authorization_failed")

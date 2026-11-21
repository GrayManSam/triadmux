import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from firstchair.cli import main


class DoctorParallelTests(unittest.TestCase):
    def setUp(self):
        self.state = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _doctor(self, *args: str) -> dict[str, Any]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--state-dir", str(self.state), *args])
        self.assertEqual(code, 0, stderr.getvalue() or stdout.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertIsInstance(payload, dict)
        return payload

    def test_probes_overlap_and_keep_provider_fields(self):
        barrier = threading.Barrier(3, timeout=3)
        current = 0
        peak = 0
        lock = threading.Lock()
        contracts = {
            "codex": "verified",
            "claude": "compatible",
            "grok": "unsupported_provider_version",
        }

        def fake_readiness(name: str) -> dict[str, object]:
            nonlocal current, peak
            with lock:
                current += 1
                peak = max(peak, current)
            try:
                barrier.wait()
            finally:
                with lock:
                    current -= 1
            return {"version": f"{name}-9.9.9", "contract": contracts[name]}

        with patch("firstchair.inventory.shutil.which", return_value="/tmp/fixture-provider"), \
                patch("firstchair.cli.readiness", side_effect=fake_readiness) as readiness:
            payload = self._doctor("doctor")

        self.assertEqual(peak, 3)
        self.assertCountEqual(
            [call.args[0] for call in readiness.call_args_list],
            ["codex", "claude", "grok"],
        )
        providers = payload["providers"]
        self.assertIsInstance(providers, dict)
        self.assertEqual(list(providers), ["codex", "claude", "grok", "synthetic"])
        self.assertEqual(providers["synthetic"]["readiness"], "ready")
        self.assertIs(providers["synthetic"]["enabled"], True)
        self.assertNotIn("contract", providers["synthetic"])
        expected = {
            "codex": ("codex-9.9.9", "verified", "ready", True),
            "claude": ("claude-9.9.9", "compatible", "ready", True),
            "grok": ("grok-9.9.9", "unsupported_provider_version", "unverified", False),
        }
        for name, (version, contract, ready, enabled) in expected.items():
            provider = providers[name]
            self.assertEqual(provider["version"], version)
            self.assertEqual(provider["contract"], contract)
            self.assertEqual(provider["readiness"], ready)
            self.assertIs(provider["enabled"], enabled)

    def test_offline_doctor_does_not_call_readiness(self):
        with patch("firstchair.cli.readiness", side_effect=AssertionError("readiness")) as readiness, \
                patch("firstchair.cli.run_bounded", side_effect=AssertionError("run_bounded")) as bounded:
            payload = self._doctor("doctor", "--offline")
        readiness.assert_not_called()
        bounded.assert_not_called()
        providers = payload["providers"]
        self.assertIsInstance(providers, dict)
        for name in ("codex", "claude", "grok"):
            self.assertNotIn("version", providers[name])
            self.assertNotIn("contract", providers[name])
            self.assertIs(providers[name]["enabled"], False)

    def test_doctor_calls_run_bounded_with_three_probes(self):
        def one_wave(calls, *, limit):
            if limit != 3 or len(calls) != 3 or not all(callable(item) for item in calls):
                raise AssertionError(
                    f"expected limit=3 and three callables, got limit={limit!r} count={len(calls)}"
                )
            return [item() for item in calls]

        with patch("firstchair.cli.run_bounded", side_effect=one_wave) as bounded, \
                patch("firstchair.cli.readiness", return_value={"version": "1.2.3", "contract": "verified"}):
            payload = self._doctor("doctor")

        bounded.assert_called_once()
        calls, kwargs = bounded.call_args
        self.assertEqual(kwargs["limit"], 3)
        self.assertEqual(len(calls[0]), 3)
        providers = payload["providers"]
        self.assertIsInstance(providers, dict)
        for name in ("codex", "claude", "grok"):
            self.assertEqual(providers[name]["version"], "1.2.3")
            self.assertEqual(providers[name]["contract"], "verified")
            self.assertEqual(providers[name]["readiness"], "ready")
            self.assertIs(providers[name]["enabled"], True)

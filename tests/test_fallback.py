"""Only usage and availability failures hand a stage to another provider."""

import unittest

from firstchair.fallback import is_transient


class TransientCodeTests(unittest.TestCase):
    def test_usage_and_availability_codes_are_transient(self):
        for code in ("rate_limited", "provider_unavailable", "provider_timeout"):
            with self.subTest(code=code):
                self.assertTrue(is_transient(code))

    def test_other_codes_stop_the_run(self):
        for code in ("authorization_failed", "malformed_response", "unsupported_version", "", "None"):
            with self.subTest(code=code):
                self.assertFalse(is_transient(code))


if __name__ == "__main__":
    unittest.main()

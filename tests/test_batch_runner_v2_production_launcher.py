"""Offline-only checks for the explicit production launcher."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from ActiveLearning.batch_runner_v2 import production_launcher


class ProductionLauncherTests(unittest.TestCase):
    def setUp(self):
        self.args = [
            "--manifest", "manifest.json",
            "--output-root", "outputs",
            "--authorization", "approval.json",
            "--authorization-sha256", "a" * 64,
        ]

    def test_missing_enable_does_not_enter_scheduler(self):
        with patch.object(production_launcher, "run_authorized_batch") as run, \
                self.assertRaises(SystemExit) as error:
            production_launcher.main(self.args)
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_missing_or_wrong_environment_pin_does_not_enter_scheduler(self):
        for value in (None, "b" * 64):
            env = {} if value is None else {
                "BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256": value
            }
            with self.subTest(value=value), patch.dict(os.environ, env, clear=True), \
                    patch.object(production_launcher, "run_authorized_batch") as run, \
                    self.assertRaises(SystemExit) as error:
                production_launcher.main(self.args + ["--enable-production"])
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()

    def test_pinned_explicit_request_uses_existing_scheduler(self):
        env = {"BATCH_RUNNER_V2_PRODUCTION_AUTHORIZATION_SHA256": "a" * 64}
        with patch.dict(os.environ, env, clear=True), \
                patch.object(production_launcher, "run_authorized_batch",
                             return_value={"status": "mocked"}) as run, \
                patch("builtins.print"):
            self.assertEqual(
                production_launcher.main(self.args + ["--enable-production"]), 0
            )
        run.assert_called_once()
        self.assertEqual(run.call_args.args[3], "a" * 64)
        self.assertIs(run.call_args.kwargs["enable_production"], True)


if __name__ == "__main__":
    unittest.main()

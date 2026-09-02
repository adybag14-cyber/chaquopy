import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from compat.hermes import build_native


class NativeBuildContractTests(unittest.TestCase):
    def test_one_explicit_python_and_abi_identifier(self):
        command = build_native.command_for(Path("source"), Path("wheels"), "3.14", "arm64-v8a")
        self.assertEqual(command[:3], [sys.executable, "-m", "cibuildwheel"])
        self.assertEqual(command[command.index("--only") + 1], "cp314-android_arm64_v8a")
        self.assertIn("--config-file", command)
        self.assertEqual(command[command.index("--output-dir") + 1], "wheels")

    def test_host_credentials_and_ambient_skip_settings_do_not_leak(self):
        with patch.dict(os.environ, {
            "GITHUB_TOKEN": "do-not-forward", "SERVICE_API_KEY": "do-not-forward",
            "CIBW_TEST_SKIP": "*", "CIBW_TEST_COMMAND_ANDROID": "echo fake",
            "ANDROID_HOME": "/sdk",
        }, clear=True):
            environment = build_native.build_environment("jiter")
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("SERVICE_API_KEY", environment)
        self.assertEqual(environment["ANDROID_HOME"], "/sdk")
        self.assertEqual(environment["CIBW_TEST_SKIP"], "")
        self.assertEqual(
            environment["CIBW_TEST_COMMAND_ANDROID"],
            "python -m compat.hermes.runtime_smoke --package jiter --require-android",
        )
        self.assertIn("ANDROID_API_LEVEL=24", environment["CIBW_ENVIRONMENT_ANDROID"])
        self.assertIn("MATURIN_PEP517_ARGS=--locked", environment["CIBW_ENVIRONMENT_ANDROID"])
        self.assertIn("max-page-size=16384", environment["CIBW_ENVIRONMENT_ANDROID"])


if __name__ == "__main__":
    unittest.main()

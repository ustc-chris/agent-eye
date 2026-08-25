from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallIntegrationTests(unittest.TestCase):
    def test_install_creates_source_symlink_without_copying_files(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["AGENT_EYE_HOME"] = str(repository)
            environment["AGENT_EYE_BIN_DIR"] = directory
            completed = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            installed = Path(directory) / "eye"
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(installed.is_symlink())
            self.assertEqual(installed.resolve(), (repository / "eye").resolve())

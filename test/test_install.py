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
            environment["AGENT_EYE_MAN_DIR"] = str(Path(directory) / "man1")
            completed = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            installed = Path(directory) / "eye"
            installed_manpage = Path(directory) / "man1" / "eye.1"
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(installed.is_symlink())
            self.assertEqual(installed.resolve(), (repository / "eye").resolve())
            self.assertTrue(installed_manpage.is_symlink())
            self.assertEqual(
                installed_manpage.resolve(), (repository / "docs" / "eye.1").resolve()
            )

    def test_install_is_idempotent_for_command_and_manpage(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["AGENT_EYE_HOME"] = str(repository)
            environment["AGENT_EYE_BIN_DIR"] = directory
            environment["AGENT_EYE_MAN_DIR"] = str(Path(directory) / "man1")
            first = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            second = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("eye 已安装", second.stdout)
            self.assertIn("man eye 已安装", second.stdout)

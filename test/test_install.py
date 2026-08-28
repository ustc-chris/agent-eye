from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallIntegrationTests(unittest.TestCase):
    def _environment(
        self, repository: Path, directory: str
    ) -> tuple[dict[str, str], Path, Path]:
        bin_directory = Path(directory) / "bin"
        man_directory = Path(directory) / "man1"
        environment = os.environ.copy()
        environment["AGENT_EYE_HOME"] = str(repository)
        environment["AGENT_EYE_BIN_DIR"] = str(bin_directory)
        environment["AGENT_EYE_MAN_DIR"] = str(man_directory)
        return environment, bin_directory, man_directory

    def test_install_creates_source_symlink_without_copying_files(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            environment, bin_directory, man_directory = self._environment(
                repository, directory
            )
            completed = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            installed = bin_directory / "eye"
            installed_manpage = man_directory / "eye.1"
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
            environment, _bin_directory, _man_directory = self._environment(
                repository, directory
            )
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

    def test_install_updates_links_from_a_previous_agent_eye_tree(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            environment, bin_directory, man_directory = self._environment(
                repository, directory
            )
            old_repository = Path(directory) / "old-agent-eye"
            (old_repository / "agent_eye").mkdir(parents=True)
            (old_repository / "docs").mkdir()
            (old_repository / "eye").write_text("old eye\n")
            (old_repository / "install.sh").write_text("old installer\n")
            (old_repository / "agent_eye" / "cli.py").write_text("old cli\n")
            (old_repository / "docs" / "eye.1").write_text("old manpage\n")
            bin_directory.mkdir()
            man_directory.mkdir()
            (bin_directory / "eye").symlink_to(old_repository / "eye")
            (man_directory / "eye.1").symlink_to(old_repository / "docs" / "eye.1")

            completed = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((bin_directory / "eye").resolve(), (repository / "eye").resolve())
            self.assertEqual(
                (man_directory / "eye.1").resolve(),
                (repository / "docs" / "eye.1").resolve(),
            )
            self.assertIn("已更新 eye", completed.stdout)
            self.assertIn("已更新 man eye", completed.stdout)

    def test_install_replaces_broken_command_and_manpage_links(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            environment, bin_directory, man_directory = self._environment(
                repository, directory
            )
            deleted_repository = Path(directory) / "deleted-agent-eye"
            bin_directory.mkdir()
            man_directory.mkdir()
            (bin_directory / "eye").symlink_to(deleted_repository / "eye")
            (man_directory / "eye.1").symlink_to(
                deleted_repository / "docs" / "eye.1"
            )

            completed = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                (bin_directory / "eye").resolve(), (repository / "eye").resolve()
            )
            self.assertEqual(
                (man_directory / "eye.1").resolve(),
                (repository / "docs" / "eye.1").resolve(),
            )
            self.assertIn("已更新 eye", completed.stdout)
            self.assertIn("已更新 man eye", completed.stdout)

    def test_install_refuses_an_unrelated_symlink(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            environment, bin_directory, man_directory = self._environment(
                repository, directory
            )
            unrelated = Path(directory) / "unrelated-eye"
            unrelated.write_text("do not replace\n")
            bin_directory.mkdir()
            man_directory.mkdir()
            installed = bin_directory / "eye"
            installed.symlink_to(unrelated)

            completed = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(installed.resolve(), unrelated.resolve())
            self.assertIn("拒绝覆盖非 Agent Eye 链接", completed.stderr)

    def test_install_refuses_a_regular_file(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            environment, bin_directory, man_directory = self._environment(
                repository, directory
            )
            bin_directory.mkdir()
            man_directory.mkdir()
            installed = bin_directory / "eye"
            installed.write_text("keep me\n")

            completed = subprocess.run(
                ["bash", str(repository / "install.sh")],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertEqual(installed.read_text(), "keep me\n")
            self.assertIn("拒绝覆盖已有路径", completed.stderr)

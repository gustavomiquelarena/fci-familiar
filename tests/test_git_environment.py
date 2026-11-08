"""Git resolution in a scheduler environment without the development PATH."""
from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from fci_familiar import db, publication
from fci_familiar.config import Config


class GitEnvironmentTests(unittest.TestCase):
    def setUp(self):
        # Native absolute paths make these checks portable between Windows/Linux.
        self.git_path = str((Path.cwd() / "synthetic Program Files" / "Git" / "cmd" / "git.exe").resolve())
        self.path_git = str((Path.cwd() / "synthetic PATH Git" / "git.exe").resolve())
        self.missing_git = str((Path.cwd() / "missing-git" / "git.exe").resolve())
        environment = patch.dict("os.environ", {"FCI_GIT_EXE": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def test_configured_git_works_when_scheduler_path_has_no_git(self):
        with patch.object(publication.Path, "is_file", lambda candidate: str(candidate) == self.git_path), \
             patch("fci_familiar.publication.shutil.which", return_value=None):
            self.assertEqual(publication.resolve_git(self.git_path), self.git_path)

    def test_missing_configured_executable_falls_back_to_path(self):
        with patch.object(publication.Path, "is_file", lambda candidate: str(candidate) == self.path_git), \
             patch("fci_familiar.publication.shutil.which", return_value=self.path_git):
            self.assertEqual(publication.resolve_git(self.missing_git), self.path_git)

    def test_spaces_in_executable_and_working_directory_are_single_argv_elements(self):
        result = subprocess.CompletedProcess([self.git_path], 0, "", "")
        repo = Path.cwd() / "synthetic repository with spaces"
        with patch("fci_familiar.publication.resolve_git", return_value=self.git_path) as resolve, \
             patch("fci_familiar.publication.REPO_ROOT", repo), \
             patch("fci_familiar.publication.subprocess.run", return_value=result) as run:
            publication._git("show", "--format=message with spaces", executable=self.git_path)
        resolve.assert_called_once_with(self.git_path)
        command = run.call_args.args[0]
        self.assertEqual(command, [self.git_path, "-C", str(repo), "show", "--format=message with spaces"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(run.call_args.kwargs["env"]["GCM_INTERACTIVE"], "never")

    def test_bundled_git_can_be_rediscovered_after_a_runtime_update(self):
        runtime_git = str((Path.cwd() / "synthetic WindowsApps Runtime" / "dependencies/native/git/cmd/git.exe").resolve())
        found = subprocess.CompletedProcess(["powershell.exe"], 0, runtime_git + "\n", "")
        with patch("fci_familiar.publication.sys.platform", "win32"), \
             patch.object(publication.Path, "is_file", lambda candidate: str(candidate) == runtime_git), \
             patch("fci_familiar.publication.shutil.which", return_value=None), \
             patch("fci_familiar.publication.subprocess.run", return_value=found) as run:
            self.assertEqual(publication.resolve_git(self.missing_git), runtime_git)
        self.assertEqual(run.call_args.args[0][0], "powershell.exe")
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_absent_git_returns_actionable_error_even_without_powershell(self):
        with patch("fci_familiar.publication.sys.platform", "win32"), \
             patch.object(publication.Path, "is_file", return_value=False), \
             patch("fci_familiar.publication.shutil.which", return_value=None), \
             patch("fci_familiar.publication.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "Git.*git_executable"):
                publication.resolve_git(self.missing_git)

    def test_ensure_repository_passes_configured_executable_to_every_git_command(self):
        config = Config(data_dir=str(Path.cwd() / "synthetic-private"),
                        repository_url="https://github.com/example/fondo-familiar",
                        git_executable=self.git_path)
        def git_result(*args, **kwargs):
            return subprocess.CompletedProcess([self.git_path, *args],
                1 if args[:3] == ("remote", "get-url", "origin") else 0, "", "")
        with patch.object(publication.Path, "exists", return_value=False), \
             patch("fci_familiar.publication._git", side_effect=git_result) as git:
            publication.ensure_repository(config)
        self.assertEqual(git.call_count, 3)
        self.assertTrue(all(call.kwargs.get("executable") == self.git_path for call in git.call_args_list))
        self.assertEqual(git.call_args_list[0].args, ("init", "-b", "main"))
        self.assertEqual(git.call_args_list[-1].args, ("remote", "add", "origin", config.repository_url))

    def test_publication_passes_configured_executable_to_every_git_command(self):
        config = Config(data_dir=str(Path.cwd() / "synthetic-private"),
                        repository_url="https://github.com/example/fondo-familiar",
                        git_executable=self.git_path)
        bundle = {"schema_version": 1, "as_of": "2026-01-01", "version": "synthetic",
                  "participants": [{"id": "tommy", "name": "Tommy", "report": {"balance_usd": "100"}}],
                  "issues": []}
        def git_result(*args, **kwargs):
            output = "a" * 40 if args == ("rev-parse", "HEAD") else ""
            return subprocess.CompletedProcess([self.git_path, *args], 0, output, "")
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        with patch("fci_familiar.publication.stage_reports", return_value={"version": "synthetic"}), \
             patch("fci_familiar.publication.assert_public_tree"), \
             patch("fci_familiar.publication.ensure_repository") as ensure, \
             patch("fci_familiar.publication._git", side_effect=git_result) as git:
            result = publication.publish(conn, config, {"tommy": "simple"}, bundle=bundle, verify=False)
        self.assertEqual(result["state"], "pushed")
        ensure.assert_called_once_with(config)
        self.assertGreaterEqual(git.call_count, 6)
        self.assertTrue(all(call.kwargs.get("executable") == self.git_path for call in git.call_args_list))
        self.assertEqual(git.call_args_list[-1].args, ("push", "-u", "origin", "main"))


if __name__ == "__main__":
    unittest.main()

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

WRAPPER = Path(__file__).resolve().parents[1] / "gitea-git.py"
spec = importlib.util.spec_from_file_location("gitea_git", WRAPPER)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


class GitBudgetTests(unittest.TestCase):
    def test_global_options_do_not_confuse_diff_detection_or_alter_pack_commands(self):
        self.assertTrue(
            wrapper.diff_command(
                ["--literal-pathspecs", "-C", "upload-pack", "-c", "key=fetch", "diff", "HEAD"]
            )
        )
        self.assertFalse(
            wrapper.diff_command(["-C", "diff", "clone", "--branch", "show", "remote"])
        )
        self.assertFalse(wrapper.diff_command(["-c", "key=log", "gc"]))

    def test_large_web_diff_is_bounded_but_full_blob_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)

            def git(*args):
                return subprocess.run(
                    ["/usr/bin/git", "-C", directory, *args], check=True, capture_output=True
                ).stdout

            git("init", "-q")
            content = b"line\n" * 1800000
            (cwd / "large.txt").write_bytes(content)
            git("add", "large.txt")
            git(
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "Fixture",
            )
            (cwd / "large.txt").write_bytes(content + b"changed\n")
            diff = subprocess.run(
                [sys.executable, str(WRAPPER), "-C", directory, "diff", "--numstat"],
                check=True,
                capture_output=True,
            ).stdout
            self.assertEqual(diff.strip(), b"-\t-\tlarge.txt")
            raw = subprocess.run(
                [sys.executable, str(WRAPPER), "-C", directory, "show", "HEAD:large.txt"],
                check=True,
                capture_output=True,
            ).stdout
            self.assertEqual(raw, content)


if __name__ == "__main__":
    unittest.main()

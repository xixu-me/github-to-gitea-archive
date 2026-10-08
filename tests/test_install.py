import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallerTests(unittest.TestCase):
    def test_staged_install_preserves_custom_config_and_renders_sandbox_and_ports(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            config = stage / "etc/gitea/github-archive.env"
            config.parent.mkdir(parents=True)
            data = (
                "GITHUB_OWNER=another-user\nGITEA_OWNER=backup-org\nARCHIVE_ADMIN_USER=maintainer\n"
                "ARCHIVE_ROOT=/srv/archive\nGITEA_DB_PATH=/srv/data/gitea.db\n"
                "GITEA_REPO_ROOT=/srv/repositories\nARCHIVE_PORT=3999\n"
            )
            config.write_text(data)
            subprocess.run(
                [sys.executable, str(ROOT / "scripts/install.py"), "--root", directory],
                check=True,
                capture_output=True,
            )
            self.assertEqual(config.read_text(), data)
            self.assertEqual(config.stat().st_mode & 0o777, 0o640)
            self.assertEqual((stage / "srv/archive").stat().st_mode & 0o777, 0o700)
            self.assertIn(
                "127.0.0.1:3999", (stage / "etc/nginx/snippets/github-archive.conf").read_text()
            )
            unit = (stage / "etc/systemd/system/github-archive-worker.service").read_text()
            self.assertIn('ReadWritePaths="/srv/archive" "/srv/repositories" "/srv/data"', unit)
            self.assertTrue((stage / "usr/local/lib/github-archive/archive_config.py").is_file())
            self.assertNotIn("texas", unit)

    def test_cli_env_file_manifest_is_generic_and_does_not_expose_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "archive.env"
            config.write_text(
                "GITHUB_OWNER=another-user\nARCHIVE_PUBLIC_URL=https://git.example.test\nGITEA_TOKEN=test-only-secret\n"
            )
            result = subprocess.run(
                [sys.executable, str(ROOT / "archive.py"), "--env-file", str(config), "manifest"],
                check=True,
                capture_output=True,
                text=True,
            )
            manifest = json.loads(result.stdout)
            self.assertIn("another-user", manifest["description"])
            self.assertNotIn("test-only-secret", result.stdout)
            self.assertEqual(
                manifest["hook_attributes"]["url"],
                "https://git.example.test/github-archive/webhook",
            )

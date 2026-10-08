import contextlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import io
from types import SimpleNamespace
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import archive as a
from archive_config import load_config, read_env_file


class ConfigurationTests(unittest.TestCase):
    def test_independent_source_namespace_and_admin(self):
        config = load_config(
            {
                "GITHUB_OWNER": "Example-Org",
                "GITEA_OWNER": "backups",
                "ARCHIVE_ADMIN_USER": "maintainer",
                "ARCHIVE_PORT": "3092",
                "GITEA_REPO_ROOT": "/srv/repos",
                "GITEA_DB_PATH": "/srv/data/gitea.db",
            }
        )
        config.validate()
        self.assertEqual(config.owner, "Example-Org")
        self.assertEqual(config.gitea_owner, "backups")
        self.assertEqual(config.admin_user, "maintainer")
        self.assertEqual(config.port, 3092)
        self.assertEqual(config.gitea_db, Path("/srv/data/gitea.db"))

    def test_missing_owner_fails_instead_of_archiving_a_default_account(self):
        with self.assertRaisesRegex(ValueError, "GITHUB_OWNER"):
            load_config({}).validate()

    def test_invalid_configuration_rejected_before_network_or_disk_writes(self):
        for key, value in [
            ("GITHUB_OWNER", "../other"),
            ("GITEA_OWNER", "x/y"),
            ("ARCHIVE_ADMIN_USER", "x/y"),
            ("ARCHIVE_PORT", "70000"),
            ("ARCHIVE_ROOT", "relative"),
            ("GITEA_DB_PATH", "/srv/../etc/db"),
            ("GITEA_URL", "https://user:password@example.test"),
            ("ARCHIVE_PUBLIC_URL", "http://example.test"),
            ("ARCHIVE_PUBLIC_URL", "https://example.test/subpath"),
            ("ARCHIVE_MIN_FREE_MIB", "0"),
            ("GITHUB_ACCOUNT_TYPE", "other"),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                load_config({"GITHUB_OWNER": "example", key: value}).validate()

    def test_env_reader_never_executes_shell_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.env"
            path.write_text(
                "GITHUB_OWNER=example\nGITHUB_APP_NAME='Example Archive'\nGITHUB_TOKEN='$(echo forbidden)'\n"
            )
            values = read_env_file(path)
            self.assertEqual(values["GITHUB_APP_NAME"], "Example Archive")
            self.assertEqual(values["GITHUB_TOKEN"], "$(echo forbidden)")
            path.write_text("GITHUB_APP_NAME=unquoted words\n")
            with self.assertRaises(ValueError):
                read_env_file(path)

    def test_manifest_uses_configured_owner_origin_and_only_read_permissions(self):
        config = load_config(
            {
                "GITHUB_OWNER": "another-owner",
                "ARCHIVE_PUBLIC_URL": "https://git.example.test",
                "GITHUB_APP_NAME": "Another Archive",
            }
        )
        with patch.object(a, "CONFIG", config), patch.object(a, "OWNER", config.owner):
            manifest = a.app_manifest()
        self.assertEqual(manifest["name"], "Another Archive")
        self.assertEqual(
            manifest["redirect_url"], "https://git.example.test/github-archive/callback"
        )
        self.assertTrue(all(value == "read" for value in manifest["default_permissions"].values()))
        self.assertIn("another-owner", manifest["description"])

    def test_namespace_cannot_be_reused_for_another_account(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(a, "ROOT", Path(directory)),
            patch.object(a, "OWNER", "first-owner"),
            patch.object(a, "GITEA_OWNER", "backups"),
        ):
            with contextlib.closing(a.db()):
                pass
            with (
                patch.object(a, "OWNER", "second-owner"),
                self.assertRaisesRegex(RuntimeError, "another"),
            ):
                a.db()

    def test_setup_capability_is_single_use_and_expires(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(a, "ROOT", Path(directory)):
            with contextlib.closing(a.db()) as c:
                a.setting(c, "state", "secret")
                a.setting(c, "expiry", a.now() + 10)
                self.assertFalse(a.consume_setup_token(c, "state", "expiry", "wrong"))
                self.assertTrue(a.consume_setup_token(c, "state", "expiry", "secret"))
                self.assertFalse(a.consume_setup_token(c, "state", "expiry", "secret"))
                a.setting(c, "state", "expired")
                a.setting(c, "expiry", a.now() - 1)
                self.assertFalse(a.consume_setup_token(c, "state", "expiry", "expired"))


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(a, "ROOT", Path(self.tmp.name))
        self.root_patch.start()
        self.c = a.db()
        self.addCleanup(self.c.close)
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.api = a.API(self.c)

    def test_public_user_without_credentials(self):
        with (
            patch.object(a, "OWNER", "public-user"),
            patch.object(self.api, "github_token", return_value=""),
            patch.object(self.api, "request", return_value={"type": "User"}),
            patch.object(self.api, "pages", return_value=[]) as pages,
        ):
            self.api.repositories()
        pages.assert_called_once_with(
            "/users/public-user/repos?type=owner&sort=updated", cached=False
        )

    def test_personal_token_enumerates_private_owned_repositories(self):
        with (
            patch.object(a, "OWNER", "private-user"),
            patch.object(self.api, "github_token", return_value="test-only"),
            patch.object(
                self.api, "request", side_effect=[{"type": "User"}, {"login": "PRIVATE-USER"}]
            ),
            patch.object(self.api, "pages", return_value=[]) as pages,
        ):
            self.api.repositories()
        self.assertIn("visibility=all", pages.call_args.args[0])

    def test_another_users_token_does_not_change_archive_scope(self):
        with (
            patch.object(a, "OWNER", "target-user"),
            patch.object(self.api, "github_token", return_value="test-only"),
            patch.object(
                self.api, "request", side_effect=[{"type": "User"}, {"login": "different-user"}]
            ),
            patch.object(self.api, "pages", return_value=[]) as pages,
        ):
            self.api.repositories()
        self.assertTrue(pages.call_args.args[0].startswith("/users/target-user/"))

    def test_organization_inventory_includes_authorized_private_repos(self):
        with (
            patch.object(a, "OWNER", "example-org"),
            patch.object(self.api, "request", return_value={"type": "Organization"}),
            patch.object(self.api, "pages", return_value=[]) as pages,
        ):
            self.api.repositories()
        pages.assert_called_once_with("/orgs/example-org/repos?type=all", cached=False)

    def test_app_inventory_does_not_fall_back_to_a_public_subset(self):
        (a.ROOT / "app.json").write_text("{}")
        with patch.object(self.api, "pages", return_value=[]) as pages:
            self.api.repositories()
        pages.assert_called_once_with("/installation/repositories", cached=False)

    def test_app_installation_pagination_and_read_only_token_scope(self):
        (a.ROOT / "app.json").write_text(json.dumps({"id": 123}))
        replies = [
            [{"account": {"login": "other"}, "id": n} for n in range(100)],
            [{"account": {"login": "target"}, "id": 999, "repository_selection": "all"}],
            {"token": "test-only-installation"},
        ]

        def response(request, **kwargs):
            return io.BytesIO(json.dumps(replies.pop(0)).encode())

        with (
            patch.object(a, "OWNER", "target"),
            patch.object(a.subprocess, "run", return_value=SimpleNamespace(stdout=b"signature")),
            patch.object(a, "http_open", side_effect=response) as network,
        ):
            self.assertEqual(self.api.github_token(), "test-only-installation")
        self.assertIn("page=2", network.call_args_list[1].args[0].full_url)
        requested = json.loads(network.call_args_list[2].args[0].data)
        self.assertTrue(all(value == "read" for value in requested["permissions"].values()))

    def test_credentials_cannot_be_sent_to_another_origin(self):
        for url in ["https://evil.example/api", "http://api.github.com/users/example"]:
            with self.subTest(url=url), patch.object(a, "http_open") as network:
                with self.assertRaisesRegex(RuntimeError, "origin"):
                    self.api.request(url)
                network.assert_not_called()

    def test_source_write_methods_are_rejected(self):
        with (
            patch.object(a, "http_open") as network,
            self.assertRaisesRegex(RuntimeError, "forbidden"),
        ):
            self.api.request("https://api.github.com/user/repos", method="POST", data={})
        network.assert_not_called()

    def test_redirect_does_not_forward_a_token(self):
        self.assertIsNone(
            a.RejectCredentialRedirect().redirect_request(
                None, None, 302, "", {}, "https://evil.example"
            )
        )


class BranchRefreshTests(unittest.TestCase):
    def test_native_hook_uses_local_namespace_and_real_admin_and_verifies_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bare = root / "demo.git"
            subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
            database = root / "native.db"
            sha = "a" * 40
            with contextlib.closing(sqlite3.connect(database)) as c:
                c.executescript(
                    "CREATE TABLE user(id INTEGER,name TEXT,lower_name TEXT);"
                    "CREATE TABLE repository(id INTEGER,name TEXT,lower_name TEXT,owner_id INTEGER);"
                    "CREATE TABLE branch(repo_id INTEGER,name TEXT,commit_id TEXT,is_deleted INTEGER);"
                    "INSERT INTO user VALUES(5,'backups','backups'),(9,'maintainer','maintainer');"
                    "INSERT INTO repository VALUES(12,'demo','demo',5);"
                )
            hook = bare / "hooks/post-receive.d/gitea"
            hook.parent.mkdir()
            hook.write_text(
                "#!" + sys.executable + "\nimport sqlite3,os,sys\n"
                "assert os.environ['GITEA_REPO_USER_NAME']=='backups'\n"
                "assert os.environ['GITEA_PUSHER_ID']=='9'\n"
                "assert os.environ['GITEA_PUSHER_NAME']=='maintainer'\n"
                f"c=sqlite3.connect({str(database)!r})\n"
                "old,new,ref=sys.stdin.read().strip().split()\n"
                "c.execute('INSERT INTO branch VALUES(?,?,?,0)',(12,ref.removeprefix('refs/heads/'),new))\n"
                "c.commit()\n"
            )
            hook.chmod(0o755)
            with (
                patch.object(a, "ROOT", root),
                patch.object(a, "GITEA_DB", database),
                patch.object(a, "GITEA_OWNER", "backups"),
                patch.object(a, "ADMIN_USER", "maintainer"),
                patch.object(a, "git", return_value=sha + " refs/heads/main\n"),
            ):
                self.assertEqual(a.refresh_gitea_branches(bare), 1)
                self.assertEqual(a.refresh_gitea_branches(bare), 0)
                with contextlib.closing(sqlite3.connect(database)) as c:
                    c.execute("DELETE FROM branch")
                    c.commit()
                hook.write_text("#!/bin/sh\nexit 0\n")
                with self.assertRaisesRegex(RuntimeError, "records differ"):
                    a.refresh_gitea_branches(bare)


class WebBoundaryTests(unittest.TestCase):
    def test_anonymous_archive_is_denied_and_malformed_webhook_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(a, "ROOT", Path(directory)),
            patch.object(a, "ADMIN_USER", "maintainer"),
            patch.dict(os.environ, {"WEBHOOK_SECRET": "test-only"}),
        ):
            with contextlib.closing(a.db()), contextlib.closing(a.inbox_db(initialize=True)):
                pass
            server = a.ThreadingHTTPServer(("127.0.0.1", 0), a.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = "http://127.0.0.1:" + str(server.server_port)
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(url + "/status.json")
                self.assertEqual(caught.exception.code, 403)
                caught.exception.close()
                request = urllib.request.Request(
                    url + "/webhook", data=b"", headers={"Content-Length": "-1"}
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request)
                self.assertEqual(caught.exception.code, 400)
                caught.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

import importlib.util
import gzip
import contextlib
import sqlite3
import tarfile
import json
import os
from pathlib import Path
import tempfile
import threading
import hashlib
import hmac
import urllib.request
import urllib.error
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "github_archive.archive_tests",
    Path(__file__).resolve().parents[1] / "src/github_archive/archive.py",
)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.multiple(
            a,
            ROOT=Path(self.tmp.name),
            OWNER="example-user",
            GITEA_OWNER="local-archive",
            ADMIN_USER="archive-admin",
        )
        self.patch.start()
        self.c = a.db()
        with contextlib.closing(a.inbox_db(initialize=True)):
            pass

    def tearDown(self):
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_atomic_snapshot_generation_rotation_and_restore(self):
        gitea = a.ROOT / "test-gitea.db"
        with contextlib.closing(sqlite3.connect(gitea)) as c:
            c.execute("CREATE TABLE item(value TEXT)")
            c.execute("INSERT INTO item VALUES ('retained')")
            c.commit()
        with patch.object(a, "GITEA_DB", gitea):
            a.snapshot(self.c)
            first = gzip.decompress(a.snapshot_path("archive").read_bytes())
            a.setting(self.c, "round_trip_test", "preserved")
            a.snapshot(self.c)
        self.assertEqual(
            gzip.decompress(a.snapshot_path("archive", "previous").read_bytes()), first
        )
        restored = a.ROOT / "restore-test.db"
        restored.write_bytes(gzip.decompress(a.snapshot_path("archive").read_bytes()))
        with contextlib.closing(sqlite3.connect(restored)) as c:
            self.assertEqual(c.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                c.execute("SELECT value FROM setting WHERE key='round_trip_test'").fetchone()[0],
                "preserved",
            )
        self.assertEqual(a.snapshot_path("archive").stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(list((a.ROOT / "snapshots").glob("generation-*"))), 2)
        generation = a.snapshot_path("archive").parent
        with tarfile.open(generation / "configuration.tgz", "r:gz") as bundle:
            names = bundle.getnames()
            self.assertTrue(any(name.endswith("github_archive/audits/common.py") for name in names))
            self.assertTrue(any(name.endswith("github_archive/audits/code.py") for name in names))

    def test_failed_snapshot_does_not_publish_a_mixed_generation(self):
        gitea = a.ROOT / "test-gitea.db"
        with contextlib.closing(sqlite3.connect(gitea)) as c:
            c.execute("CREATE TABLE item(value TEXT)")
        with patch.object(a, "GITEA_DB", gitea):
            a.snapshot(self.c)
            committed = (a.ROOT / "snapshots/generations.json").read_bytes()
            before = a.snapshot_path("gitea").read_bytes()
            real_copy = a.compressed_snapshot_copy

            def fail_archive(source, destination):
                if source.name == "archive.db":
                    raise RuntimeError("simulated interrupted archive copy")
                real_copy(source, destination)

            with patch.object(a, "compressed_snapshot_copy", side_effect=fail_archive):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    a.snapshot(self.c)
            self.assertEqual((a.ROOT / "snapshots/generations.json").read_bytes(), committed)
            self.assertEqual(a.snapshot_path("gitea").read_bytes(), before)
            self.assertEqual(list((a.ROOT / "snapshots").glob(".snapshot-*")), [])

    def test_unchanged_objects_keep_data_and_refresh_presence(self):
        with patch.object(a, "now", return_value=100):
            a.save_objects(self.c, 1, "release_asset", [{"id": 7, "name": "old"}], "release:3")
        with patch.object(a, "now", return_value=200):
            a.save_objects(self.c, 1, "release_asset", [{"id": 7, "name": "old"}], "release:3")
        self.assertEqual(self.c.execute("SELECT updated FROM object").fetchone()[0], 100)
        self.assertEqual(self.c.execute("SELECT checked FROM presence").fetchone()[0], 200)
        with patch.object(a, "now", return_value=300):
            a.save_objects(self.c, 1, "release_asset", [{"id": 7, "name": "edited"}], "release:3")
        self.assertEqual(self.c.execute("SELECT updated FROM object").fetchone()[0], 300)
        self.assertEqual(
            a.decode(self.c.execute("SELECT body FROM object").fetchone()[0])["name"], "edited"
        )
        self.assertIn(
            "presence_scope",
            " ".join(
                str(tuple(x))
                for x in self.c.execute(
                    'EXPLAIN QUERY PLAN UPDATE presence SET present=0 WHERE repo_id=1 AND kind="release_asset" AND scope="release:3"'
                )
            ),
        )

    def test_release_inventory_reuses_only_verified_unchanged_assets(self):
        calls = []
        assets = [
            {
                "id": 7,
                "name": "binary",
                "size": 10,
                "browser_download_url": "https://github.com/example-user/demo/releases/download/v1/file",
            }
        ]
        release = {"id": 3, "body": "", "assets": assets}
        a.save_objects(self.c, 1, "release", [release])
        a.save_objects(self.c, 1, "release_asset", assets, "release:3")
        a.setting(self.c, "deep:1", a.now())

        class Fake:
            def pages(self, path):
                calls.append(path)
                if path.endswith("/releases"):
                    return [dict(release, assets=list(assets))]
                if path.endswith("/releases/3/assets"):
                    return list(assets)
                return []

        a.sync_metadata(self.c, Fake(), {"id": 1, "name": "demo", "source": a.encode({})})
        self.assertFalse(any(x.endswith("/releases/3/assets") for x in calls))
        assets.append(
            {
                "id": 8,
                "name": "new",
                "size": 20,
                "browser_download_url": assets[0]["browser_download_url"] + "2",
            }
        )
        calls.clear()
        a.sync_metadata(self.c, Fake(), {"id": 1, "name": "demo", "source": a.encode({})})
        self.assertTrue(any(x.endswith("/releases/3/assets") for x in calls))
        a.setting(self.c, "deep:1", 0)
        calls.clear()
        a.sync_metadata(self.c, Fake(), {"id": 1, "name": "demo", "source": a.encode({})})
        self.assertTrue(any(x.endswith("/releases/3/assets") for x in calls))
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM object WHERE kind='release_asset'").fetchone()[0],
            2,
        )

    def test_hourly_pull_list_refresh_preserves_full_details_and_merged_provenance(self):
        detail_calls = []
        parent = {
            "id": 9,
            "number": 3,
            "title": "Example",
            "body": "Body",
            "state": "closed",
            "updated_at": "2026-10-08T00:00:00Z",
            "labels": [],
            "merged_at": "2026-10-07T00:00:00Z",
            "merge_commit_sha": "abc",
            "html_url": "https://github.com/example-user/demo/pull/3",
        }
        issue = dict(parent, id=7, pull_request={})
        full = dict(parent, changed_files=7, merged=True, merged_by={"login": "alice"})
        available = [True]
        native_calls = []
        native = {
            "number": 3,
            "title": "Example",
            "state": "closed",
            "body": "old",
            "content_version": 2,
            "pull_request": {"merged": False},
        }

        class Fake:
            def pages(self, path, **kwargs):
                if "/issues?" in path:
                    return [issue] if available[0] else []
                if "/pulls?" in path:
                    return [parent] if available[0] else []
                return []

            def parallel_pages(self, paths):
                return {k: [] for k in paths}

            def request(self, url, **kwargs):
                detail_calls.append(url)
                return full

            def gt_pages(self, path):
                return [native] if "/issues?" in path else []

            def gt(self, path, method="GET", data=None):
                native_calls.append((path, method, data))
                return native

        row = {"id": 1, "name": "demo", "source": a.encode({})}
        self.c.execute("INSERT INTO repo(id,name,source) VALUES(1,?,?)", ("demo", row["source"]))
        self.c.commit()
        a.sync_metadata(self.c, Fake(), row)
        a.sync_metadata(self.c, Fake(), row)
        self.assertEqual(len(detail_calls), 1)
        saved = a.decode(
            self.c.execute("SELECT body FROM object WHERE kind='pull_detail'").fetchone()[0]
        )
        self.assertEqual(saved["changed_files"], 7)
        self.assertEqual(saved["merged_by"]["login"], "alice")
        # Repair old partial archives even when the parent timestamp has not changed.
        self.c.execute("DELETE FROM object WHERE kind='pull_content_manifest'")
        self.c.commit()
        a.sync_metadata(self.c, Fake(), row)
        self.assertEqual(len(detail_calls), 2)
        self.assertEqual(
            self.c.execute(
                "SELECT count(*) FROM object WHERE kind='pull_content_manifest'"
            ).fetchone()[0],
            1,
        )
        a.project_gitea(self.c, Fake(), row)
        edits = [x[2] for x in native_calls if x[1] == "PATCH"]
        self.assertIn("merged", edits[0]["body"])
        self.assertIn("abc", edits[0]["body"])
        self.assertEqual(native["pull_request"]["merged"], False)
        available[0] = False
        a.sync_metadata(self.c, Fake(), row)
        self.assertEqual(
            self.c.execute("SELECT present FROM presence WHERE kind='pull_detail'").fetchone()[0], 0
        )
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM object WHERE kind='pull_detail'").fetchone()[0], 1
        )

    def test_event_coalescing_preserves_both_work_types(self):
        a.enqueue(self.c, 1, "code", "push")
        a.enqueue(self.c, 1, "metadata", "issue")
        rows = self.c.execute("select * from job").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "all")

    def test_webhook_priority_survives_periodic_reconciliation(self):
        a.enqueue(self.c, 1, "all", "hourly-reconcile")
        a.enqueue(self.c, 2, "code", "webhook:delivery")
        a.enqueue(self.c, 2, "all", "hourly-reconcile")
        row = self.c.execute("select * from job order by priority desc,due limit 1").fetchone()
        self.assertEqual(row["repo_id"], 2)
        self.assertEqual(row["priority"], 10)

    def test_review_event_forces_deep_scan_without_parent_update(self):
        a.setting(self.c, "deep:2", a.now())
        a.enqueue(self.c, 2, "all", "webhook:review-edit")
        self.assertIsNone(a.setting(self.c, "deep:2"))

    def test_typed_webhooks_invalidate_parent_and_child_metadata_scans(self):
        for event in ["repository", "create", "delete"]:
            a.setting(self.c, "deep:2", 12345)
            a.enqueue(self.c, 2, "all", f"webhook:{event}:delivery")
            self.assertEqual(a.setting(self.c, "deep:2"), "12345", event)
        for event in [
            "issues",
            "pull_request",
            "release",
            "issue_comment",
            "pull_request_review",
            "pull_request_review_comment",
            "pull_request_review_thread",
        ]:
            a.setting(self.c, "deep:2", 12345)
            a.enqueue(self.c, 2, "all", f"webhook:{event}:delivery")
            self.assertIsNone(a.setting(self.c, "deep:2"), event)
        self.assertEqual(
            self.c.execute("SELECT priority FROM job WHERE repo_id=2").fetchone()[0], 10
        )

    def test_wiki_event_refreshes_wiki_without_restarting_review_scan(self):
        a.setting(self.c, "wiki_checked:2", a.now())
        a.setting(self.c, "deep:2", 12345)
        a.enqueue(self.c, 2, "all", "webhook:gollum:delivery")
        self.assertIsNone(a.setting(self.c, "wiki_checked:2"))
        self.assertEqual(a.setting(self.c, "deep:2"), "12345")

    def test_snapshot_waits_for_worker_before_copying_database_pair(self):
        entered = threading.Event()
        with open(a.ROOT / "worker.lock", "a") as worker_lock:
            a.fcntl.flock(worker_lock, a.fcntl.LOCK_EX)
            with patch.object(a, "_snapshot", side_effect=lambda c: entered.set()):
                thread = threading.Thread(target=a.snapshot, args=(self.c,))
                thread.start()
                try:
                    self.assertFalse(entered.wait(0.05))
                finally:
                    a.fcntl.flock(worker_lock, a.fcntl.LOCK_UN)
                self.assertTrue(entered.wait(2))
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())

    def test_worker_retries_after_concurrent_writer_invalidates_read_snapshot(self):
        self.c.execute(
            "INSERT INTO repo(id,name,local_name,source,available) VALUES(1,?,?,?,1)",
            ("demo", "demo", a.encode({})),
        )
        self.c.commit()
        a.setting(self.c, "discovery_ok", a.now())
        a.enqueue(self.c, 1, "metadata", "test")

        def conflicting_sync(c, api, row):
            c.execute("BEGIN")
            c.execute("SELECT * FROM setting").fetchall()
            with contextlib.closing(sqlite3.connect(a.ROOT / "state.db")) as writer:
                writer.execute("INSERT OR REPLACE INTO setting VALUES('concurrent','1')")
                writer.commit()
            c.execute("INSERT OR REPLACE INTO setting VALUES('stale-writer','1')")

        with patch.object(a, "sync_metadata", side_effect=conflicting_sync):
            self.assertEqual(a.worker(self.c, 1), 1)
        row = self.c.execute("SELECT * FROM job WHERE repo_id=1").fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["claimed"], 0)
        self.assertIn("locked", self.c.execute("SELECT error FROM repo WHERE id=1").fetchone()[0])
        self.assertIsNone(a.setting(self.c, "stale-writer"))

    def test_private_guard_runs_before_metadata_and_blocks_failed_protection(self):
        bare = a.ROOT / "repositories" / "demo.git"
        bare.mkdir(parents=True)
        self.c.execute(
            "INSERT INTO repo(id,name,local_name,private,source,available) VALUES(1,?,?,1,?,1)",
            ("demo", "demo", a.encode({"private": True})),
        )
        self.c.commit()
        a.setting(self.c, "discovery_ok", a.now())
        events = []

        class Fake:
            def __init__(self, c):
                pass

            def gt(self, path, method="GET", data=None):
                events.append(("privacy", data))
                return {"private": True}

        with (
            patch.object(a, "GITROOT", bare.parent),
            patch.object(a, "API", Fake),
            patch.object(
                a, "sync_metadata", side_effect=lambda *args: events.append(("metadata", None))
            ),
            patch.object(a, "project_gitea"),
        ):
            a.enqueue(self.c, 1, "metadata", "test")
            self.assertEqual(a.worker(self.c, 1), 0)
        self.assertEqual(events[:2], [("privacy", {"private": True}), ("metadata", None)])

        class Failing(Fake):
            def gt(self, *args, **kwargs):
                raise RuntimeError("privacy protection failed")

        with (
            patch.object(a, "GITROOT", bare.parent),
            patch.object(a, "API", Failing),
            patch.object(a, "sync_metadata") as sync,
        ):
            a.enqueue(self.c, 1, "metadata", "test")
            self.assertEqual(a.worker(self.c, 1), 1)
            sync.assert_not_called()

    def test_browser_auth_uses_protected_profile_and_rejects_other_users(self):
        import io

        class Response(io.BytesIO):
            status = 200

        class Opener:
            def __init__(self, body):
                self.body = body

            def open(self, req, **kw):
                self.request = req
                return Response(self.body)

        for user, expected in [(a.ADMIN_USER, True), ("other-user", False)]:
            opener = Opener(f'<input id="username" name="name" value="{user}">'.encode())
            with patch("urllib.request.build_opener", return_value=opener):
                self.assertEqual(a.authenticated_owner({"Cookie": "session=test"}), expected)
            self.assertTrue(opener.request.full_url.endswith("/user/settings"))
        self.assertFalse(a.authenticated_owner({"X-Archive-User": a.OWNER}))
        self.assertIsNone(
            a.NoAuthRedirect().redirect_request(None, None, 302, "", {}, "https://outside.example/")
        )

    def test_attachment_inventory_keeps_links_sizes_and_checksums(self):
        a.save_objects(
            self.c,
            1,
            "issue",
            [{"id": 2, "body": "![image](https://github.com/user-attachments/assets/demo)"}],
        )
        a.save_objects(
            self.c,
            1,
            "release_asset",
            [
                {
                    "id": 3,
                    "browser_download_url": "https://github.com/example-user/demo/releases/download/v1/test",
                    "size": 42,
                    "digest": "sha256:abc",
                    "name": "test",
                }
            ],
        )
        a.inventory_links(self.c, 1)
        rows = [
            a.decode(x[0])
            for x in self.c.execute("select body from object where kind='attachment_manifest'")
        ]
        self.assertEqual(len(rows), 2)
        asset = next(x for x in rows if x.get("size"))
        self.assertEqual(asset["digest"], "sha256:abc")
        self.assertEqual(asset["policy"], "link-only")

    def test_lfs_inventory_handles_binary_blob_before_pointer(self):
        pointer = (
            "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\nsize 12345\n"
        ).encode()
        binary = b"\xff" * 110
        tree = (
            "100644 blob first 110\tfile.bin\0" + f"100644 blob second {len(pointer)}\tlarge.bin\0"
        )
        batch = (
            b"first blob 110\n"
            + binary
            + b"\nsecond blob "
            + str(len(pointer)).encode()
            + b"\n"
            + pointer
            + b"\n"
        )
        with patch.object(a, "git", side_effect=[tree, batch]):
            a.inventory_lfs(self.c, {"id": 1, "name": "demo"}, Path("/bare"), True)
        row = self.c.execute("select body from object where kind='lfs_manifest'").fetchone()
        data = a.decode(row[0])
        self.assertEqual(data["pointers"][0]["size"], 12345)
        self.assertEqual(data["pointers"][0]["paths"], ["large.bin"])

    def test_lfs_pointer_beyond_former_scan_limit_is_archived_in_bounded_batches(self):
        pointer = (
            "version https://git-lfs.github.com/spec/v1\next-0-demo sha256:"
            + "b" * 64
            + "\noid sha256:"
            + "a" * 64
            + "\nsize 987\n"
        ).encode()
        tree = "".join(
            f"100644 blob blob-{i} {len(pointer) if i == 6000 else 110}\tfile-{i}\0"
            for i in range(6001)
        )
        batches = []

        def fake_git(args, **kwargs):
            if "ls-tree" in args:
                return tree
            ids = kwargs["input_data"].decode().splitlines()
            batches.append(len(ids))
            return b"".join(
                (
                    f"{sha} blob {len(pointer) if sha == 'blob-6000' else 110}\n".encode()
                    + (pointer if sha == "blob-6000" else b"\xff" * 110)
                    + b"\n"
                )
                for sha in ids
            )

        with patch.object(a, "git", side_effect=fake_git):
            a.inventory_lfs(self.c, {"id": 1, "name": "demo"}, Path("/bare"), True, "c" * 40)
        manifest = a.decode(
            self.c.execute("select body from object where kind='lfs_manifest'").fetchone()[0]
        )
        self.assertTrue(manifest["inventory_complete"])
        self.assertEqual(manifest["candidate_blobs"], 6001)
        self.assertEqual(manifest["head_sha"], "c" * 40)
        self.assertEqual([x["paths"] for x in manifest["pointers"]], [["file-6000"]])
        self.assertEqual(
            manifest["pointers"][0]["pointer_fields"]["ext-0-demo"], "sha256:" + "b" * 64
        )
        self.assertLessEqual(max(batches), 1000)
        self.assertEqual(sum(batches), 6001)

    def test_discovery_new_renamed_and_inaccessible_repositories(self):
        class Fake:
            def __init__(self, rows):
                self.rows = rows

            def repositories(self):
                return self.rows

        repo = {"id": 42, "name": "original", "private": True, "owner": {"login": a.OWNER}}
        a.discover(self.c, Fake([repo]))
        repo["name"] = "renamed"
        a.discover(self.c, Fake([repo]))
        row = self.c.execute("select * from repo where id=42").fetchone()
        self.assertEqual(row["name"], "renamed")
        self.assertEqual(row["local_name"], "original")
        # An empty successful enumeration must also mark inaccessible sources.
        a.discover(self.c, Fake([]))
        row = self.c.execute("select * from repo where id=42").fetchone()
        self.assertEqual(row["available"], 0)

    def test_discovery_does_not_overwrite_newer_private_event(self):
        test = self

        class Fake:
            def repositories(self):
                a.setting(test.c, "private_event:42", a.time.time_ns())
                test.c.commit()
                return [{"id": 42, "name": "demo", "private": False, "owner": {"login": a.OWNER}}]

        with patch.object(a.time, "time_ns", side_effect=[100, 200]):
            a.discover(self.c, Fake())
        row = self.c.execute("select * from repo where id=42").fetchone()
        self.assertEqual(row["private"], 1)
        self.assertTrue(a.decode(row["source"])["private"])

    def test_pagination_beyond_old_nineteen_page_limit(self):
        api = a.API(self.c)
        count = [0]

        def request(*args, **kwargs):
            count[0] += 1
            return [{"id": count[0]}] * (100 if count[0] < 22 else 3)

        api.request = request
        self.assertEqual(len(api.pages("/user/repos")), 2103)

    def test_object_edits_replace_current_record_without_duplicates(self):
        a.save_objects(self.c, 1, "issue", [{"id": 7, "body": "before"}])
        a.save_objects(self.c, 1, "issue", [{"id": 7, "body": "after"}])
        rows = self.c.execute("select * from object").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(a.decode(rows[0]["body"])["body"], "after")

    def test_source_deletion_marks_missing_without_destroying_archive(self):
        a.save_objects(self.c, 1, "comment", [{"id": 7, "body": "retained"}], "issue:3")
        a.save_objects(self.c, 1, "comment", [], "issue:3")
        self.assertEqual(self.c.execute("select count(*) from object").fetchone()[0], 1)
        self.assertEqual(self.c.execute("select present from presence").fetchone()[0], 0)
        a.save_objects(self.c, 1, "comment", [{"id": 8, "body": "other"}], "issue:4")
        self.assertEqual(
            self.c.execute("select present from presence where id=?", ("7",)).fetchone()[0], 0
        )

    def test_metadata_preserves_pr_reviews_files_timeline_and_all_release_assets(self):
        issue = {"id": 7, "number": 3, "updated_at": "2026-10-08T00:00:00Z", "pull_request": {}}
        pull = {"id": 9, "number": 3, "merged": True}

        class Fake:
            def pages(self, path, **kwargs):
                if "/issues?" in path:
                    return [issue]
                if "/pulls?" in path:
                    return [pull]
                if path.endswith("/releases"):
                    return [{"id": 11, "assets": []}]
                if path.endswith("/releases/11/assets"):
                    return [{"id": 21}, {"id": 22}]
                if path.endswith("/issues/3/comments"):
                    return [
                        {
                            "id": 13,
                            "issue_url": "https://api.github.com/repos/example-user/demo/issues/3",
                        }
                    ]
                if path.endswith("/issues/3/events"):
                    return [{"id": 14}, {"id": 15}]
                if path.endswith("/issues/3/timeline"):
                    return [{"event": "committed", "sha": "a"}, {"event": "committed", "sha": "b"}]
                if path.endswith("/pulls/3/reviews"):
                    return [{"id": 16}]
                if path.endswith("/pulls/3/comments"):
                    return [{"id": 17}]
                if path.endswith("/pulls/3/commits"):
                    return [{"sha": "a"}, {"sha": "b"}]
                if path.endswith("/pulls/3/files"):
                    return [
                        {"sha": "same", "filename": "a.txt"},
                        {"sha": "same", "filename": "b.txt"},
                    ]
                return []

            def parallel_pages(self, paths):
                return {k: self.pages(v) for k, v in paths.items()}

            def request(self, *args, **kwargs):
                return pull

        self.c.execute(
            "insert into repo(id,name,local_name,private,source,seen) values(1,?,?,0,?,0)",
            ("demo", "demo", a.encode({})),
        )
        self.c.commit()
        a.sync_metadata(self.c, Fake(), {"id": 1, "name": "demo", "source": a.encode({})})
        counts = dict(self.c.execute("select kind,count(*) from object group by kind"))
        self.assertEqual(counts["release_asset"], 2)
        self.assertEqual(counts["pull_file"], 2)
        self.assertEqual(counts["issue_timeline"], 2)
        self.assertEqual(counts["review"], 1)
        self.assertEqual(counts["review_comment"], 1)
        self.assertGreater(
            self.c.execute("select metadata_ok from repo where id=1").fetchone()[0], 0
        )

    def test_disabled_pr_endpoint_retains_old_records_and_reports_gap(self):
        self.c.execute(
            "insert into repo(id,name,source) values(1,?,?)",
            ("demo", a.encode({"has_pull_requests": False})),
        )
        self.c.commit()
        a.save_objects(self.c, 1, "pull", [{"id": 42, "number": 1}])

        class Fake:
            def pages(self, path, **kw):
                if "/pulls?" in path:
                    raise RuntimeError("API GET /pulls: HTTP 404")
                return []

        row = self.c.execute("select * from repo").fetchone()
        a.sync_metadata(self.c, Fake(), row)
        self.assertEqual(
            self.c.execute("select present from presence where kind='pull'").fetchone()[0], 1
        )
        self.assertIn("disabled", a.setting(self.c, "feature_gaps:1"))
        self.assertGreater(self.c.execute("select metadata_ok from repo").fetchone()[0], 0)

    def test_capacity_blocks_api_before_any_write_or_network(self):
        with patch.object(a, "free", return_value=a.LOW - 1), patch.object(a, "http_open") as net:
            with self.assertRaisesRegex(RuntimeError, "capacity"):
                a.API(self.c).request("https://api.github.com/user/repos")
            net.assert_not_called()

    def test_uncertain_create_is_not_blindly_repeated(self):
        with patch.object(a, "http_open", side_effect=urllib.error.URLError("timeout")) as net:
            with self.assertRaisesRegex(RuntimeError, "POST outcome uncertain"):
                a.API(self.c).request(
                    "http://127.0.0.1:3000/api/v1/user/repos",
                    method="POST",
                    data={"name": "example"},
                    gitea=True,
                )
            self.assertEqual(net.call_count, 1)

    def test_git_token_not_present_in_argv(self):
        class Result:
            returncode = 0

            def communicate(self, **kw):
                return b"", b""

            def poll(self):
                return 0

        with patch("subprocess.Popen", return_value=Result()) as run:
            a.git(["fetch", "https://github.com/example/repo.git"], "sensitive-token")
        args, kwargs = run.call_args
        self.assertNotIn("sensitive-token", repr(args))
        self.assertIn("GIT_CONFIG_VALUE_0", kwargs["env"])

    def test_git_cancels_process_group_when_capacity_drops(self):
        class Process:
            pid = 42
            calls = 0

            def poll(self):
                return None

            def communicate(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise a.subprocess.TimeoutExpired("git", 1)
                return b"", b""

        with (
            patch("subprocess.Popen", return_value=Process()),
            patch.object(a, "free", return_value=a.LOW - 1),
            patch("os.killpg") as kill,
        ):
            with self.assertRaisesRegex(RuntimeError, "capacity protection"):
                a.git(["fetch", "https://github.com/example/repo.git"])
            kill.assert_called_once_with(42, a.signal.SIGTERM)

    def test_gitea_projection_is_idempotent_and_updates_labels(self):
        calls = []

        class Fake:
            def gt_pages(self, path):
                return (
                    [{"number": 3, "title": "Example", "body": "old"}] if "/issues?" in path else []
                )

            def gt(self, path, method="GET", data=None):
                calls.append((path, method, data))
                return {"id": 99, "number": 3, "content_version": 9}

        item = {
            "id": 7,
            "number": 3,
            "title": "Example",
            "body": "new",
            "state": "open",
            "html_url": "https://github.com/example-user/demo/issues/3",
            "labels": [],
        }
        a.save_objects(self.c, 1, "issue", [item])
        row = {"id": 1, "name": "demo"}
        a.project_gitea(self.c, Fake(), row)
        self.assertEqual(len([x for x in calls if x[1] == "PATCH"]), 1)
        self.assertEqual([x for x in calls if x[1] == "PATCH"][0][2]["content_version"], 9)
        calls.clear()
        a.project_gitea(self.c, Fake(), row)
        self.assertEqual(calls, [])

    def test_merged_pr_update_does_not_repeat_state_transition(self):
        calls = []
        native = {
            "number": 3,
            "title": "Merged example",
            "state": "closed",
            "pull_request": {"merged": True},
            "content_version": 9,
            "body": "old",
        }

        class Fake:
            def gt_pages(self, path):
                return [native] if "/issues?" in path else []

            def gt(self, path, method="GET", data=None):
                calls.append((path, method, data))
                return native

        item = {
            "id": 7,
            "number": 3,
            "title": native["title"],
            "body": "new",
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "a" * 40,
            "html_url": "https://github.com/example-user/demo/pull/3",
            "labels": [],
        }
        a.save_objects(self.c, 1, "pull", [item])
        a.project_gitea(self.c, Fake(), {"id": 1, "name": "demo"})
        updates = [x[2] for x in calls if x[1] == "PATCH"]
        self.assertEqual(len(updates), 1)
        self.assertNotIn("state", updates[0])

    def test_inbox_accepts_while_main_database_writer_is_locked(self):
        self.c.execute("BEGIN IMMEDIATE")
        with patch.object(a, "db", side_effect=AssertionError("response must not open main DB")):
            self.assertTrue(a.receive_delivery({}, b"{}", "locked-main", "ping"))
            self.assertFalse(a.receive_delivery({}, b"{}", "locked-main", "ping"))
        self.c.rollback()
        self.assertEqual(a.ingest(self.c), 1)
        self.assertEqual(a.ingest(self.c), 0)
        with contextlib.closing(a.inbox_db()) as q:
            row = q.execute("SELECT * FROM delivery").fetchone()
            self.assertEqual(row["done"], 1)
            self.assertIsNone(row["payload"])

    def test_inbox_replay_after_crash_between_main_commit_and_ack(self):
        payload = {
            "repository": {"id": 91, "name": "demo", "private": False, "owner": {"login": a.OWNER}}
        }
        a.receive_delivery(payload, json.dumps(payload).encode(), "crash-replay", "repository")
        a.apply_delivery(self.c, payload, "crash-replay", "repository")
        with patch.object(a, "enqueue", side_effect=AssertionError("already applied")):
            self.assertEqual(a.ingest(self.c), 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM event").fetchone()[0], 1)
        self.assertEqual(
            self.c.execute("SELECT reason FROM job").fetchone()[0],
            "webhook:repository:crash-replay",
        )

    def test_failed_privacy_protection_keeps_inbox_gate_and_payload(self):
        payload = {
            "repository": {"id": 92, "name": "demo", "private": True, "owner": {"login": a.OWNER}}
        }
        a.receive_delivery(payload, json.dumps(payload).encode(), "private-retry", "repository")
        with patch.object(a, "protect_private_repo", side_effect=RuntimeError("offline")):
            self.assertEqual(a.ingest(self.c), 0)
        with contextlib.closing(a.inbox_db()) as q:
            row = q.execute("SELECT * FROM delivery").fetchone()
            self.assertEqual(row["done"], 0)
            self.assertEqual(row["privacy_pending"], 1)
            self.assertEqual(a.decode(row["payload"]), payload)
        with patch.object(a, "protect_private_repo"):
            self.assertEqual(a.ingest(self.c), 1)
        with contextlib.closing(a.inbox_db()) as q:
            self.assertEqual(q.execute("SELECT privacy_pending FROM delivery").fetchone()[0], 0)

    def test_privacy_gate_http_and_concurrent_deduplication(self):
        import concurrent.futures

        payload = {
            "repository": {"id": 93, "name": "demo", "private": True, "owner": {"login": a.OWNER}}
        }
        raw = json.dumps(payload).encode()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            accepted = list(
                pool.map(
                    lambda _: a.receive_delivery(payload, raw, "concurrent-private", "repository"),
                    range(4),
                )
            )
        self.assertEqual(accepted.count(True), 1)
        server = a.ThreadingHTTPServer(("127.0.0.1", 0), a.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/privacy-guard"
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            with patch.object(a, "protect_private_repo"):
                self.assertEqual(a.ingest(self.c), 1)
            with urllib.request.urlopen(url) as response:
                self.assertEqual(response.status, 204)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_branch_hook_updates_changes_creates_deletes_and_revives(self):
        old = "a" * 40
        new = "b" * 40
        zero = "0" * 40
        changes = a.branch_hook_updates(
            {"main": new, "unchanged": old, "new": new, "revived": old},
            {
                "main": (old, False),
                "unchanged": (old, False),
                "deleted": (old, False),
                "already-gone": (old, True),
                "revived": (old, True),
            },
        )
        self.assertEqual(
            changes,
            [
                f"{old} {zero} refs/heads/deleted",
                f"{old} {new} refs/heads/main",
                f"{zero} {new} refs/heads/new",
                f"{zero} {old} refs/heads/revived",
            ],
        )

    def test_pending_private_signal_is_repo_specific_and_survives_failed_ingest(self):
        payload = {
            "repository": {"id": 94, "name": "demo", "private": True, "owner": {"login": a.OWNER}}
        }
        a.receive_delivery(
            payload, json.dumps(payload).encode(), "pending-private-hook", "repository"
        )
        self.assertTrue(a.pending_private_signal(94))
        self.assertFalse(a.pending_private_signal(95))
        with patch.object(a, "protect_private_repo", side_effect=RuntimeError("offline")):
            a.ingest(self.c)
        self.assertTrue(a.pending_private_signal(94))
        with patch.object(a, "protect_private_repo"):
            a.ingest(self.c)
        self.assertFalse(a.pending_private_signal(94))

    def test_webhook_signature_and_duplicate_delivery(self):
        server = a.ThreadingHTTPServer(("127.0.0.1", 0), a.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with patch.dict(os.environ, {"WEBHOOK_SECRET": "test-only-secret"}):
            thread.start()
            raw = b'{"zen":"test"}'
            valid = "sha256=" + hmac.new(b"test-only-secret", raw, hashlib.sha256).hexdigest()
            codes = []
            self.c.execute("BEGIN IMMEDIATE")
            for signature in ["sha256=invalid", valid, valid]:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}/webhook",
                    raw,
                    {
                        "X-Hub-Signature-256": signature,
                        "X-GitHub-Delivery": "test-delivery",
                        "X-GitHub-Event": "ping",
                        "Content-Type": "application/json",
                    },
                )
                try:
                    with urllib.request.urlopen(req, timeout=2) as r:
                        codes.append(r.status)
                except urllib.error.HTTPError as e:
                    codes.append(e.code)
                    e.close()
            self.assertEqual(codes, [401, 202, 200])
            self.c.rollback()
            self.assertEqual(self.c.execute("select count(*) from event").fetchone()[0], 0)
            self.assertEqual(a.ingest(self.c), 1)
            self.assertEqual(self.c.execute("select count(*) from event").fetchone()[0], 1)
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

import importlib.util
import pathlib
import unittest
import io
from unittest.mock import patch
import tempfile
import sqlite3
import json
import sys
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location(
    "audit_children", pathlib.Path(__file__).resolve().parents[1] / "audit_children.py"
)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class ChildAuditTests(unittest.TestCase):
    def test_changed_comment_body_fails_even_with_same_id_and_count(self):
        result = audit.compare(
            [{"id": 1, "body": "edited"}], {"1": {"id": 1, "body": "old"}}, "issue_comment", 9
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["changed"], ["1"])

    def test_composite_file_identity_retains_original_payload_and_filename(self):
        source = {"filename": "目录/文件.txt", "patch": "original patch", "status": "added"}
        saved = dict(source, id="9:目录/文件.txt", _archive_pull_number=9)
        self.assertTrue(audit.compare([source], {saved["id"]: saved}, "pull_file", 9)["ok"])
        self.assertFalse(audit.compare([source], {}, "pull_file", 9)["ok"])

    def test_empty_source_detects_active_stale_archive_record(self):
        self.assertTrue(audit.compare([], {}, "review", 9)["ok"])
        self.assertFalse(audit.compare([], {"1": {"id": 1}}, "review", 9)["ok"])

    def test_timeline_events_without_numeric_id_are_not_lost(self):
        event = {"event": "committed", "sha": "abc", "message": "first commit"}
        self.assertTrue(audit.compare([event], {"abc": event}, "issue_timeline", 9)["ok"])
        anonymous = {"event": "review_dismissed", "message": "context"}
        key = audit.identity(anonymous, "issue_timeline", 9)
        self.assertTrue(audit.compare([anonymous], {key: anonymous}, "issue_timeline", 9)["ok"])

    def test_resume_reuses_only_successful_unchanged_records_from_this_campaign(self):
        stored = {"1": {"id": 1, "body": "verified"}}
        previous = {
            "ok": True,
            "url": "https://example.test/comments",
            "source_checked": 101,
            "archive_digest": audit.archive_digest(stored),
        }
        self.assertTrue(audit.reusable(previous, stored, previous["url"], 100))
        self.assertFalse(
            audit.reusable(previous, {"1": {"id": 1, "body": "changed"}}, previous["url"], 100)
        )
        self.assertFalse(audit.reusable(previous, stored, previous["url"] + "/renamed", 100))
        self.assertFalse(audit.reusable(previous, stored, previous["url"], 102))
        self.assertFalse(audit.reusable(dict(previous, ok=False), stored, previous["url"], 100))
        failed = dict(previous, ok=False, expected=2, missing=["2"])
        self.assertTrue(audit.reusable(failed, stored, previous["url"], 100, allow_failed=True))
        self.assertFalse(failed["ok"])
        self.assertFalse(
            audit.reusable(
                dict(failed, error="unproven response"),
                stored,
                previous["url"],
                100,
                allow_failed=True,
            )
        )

    def test_live_audit_preserves_quota_without_writing_production_backoff(self):
        class Response(io.BytesIO):
            headers = {"X-RateLimit-Remaining": "1499", "X-RateLimit-Reset": "2000"}

        control = {}
        with patch.object(audit, "http_open", return_value=Response(b"[]")) as call:
            self.assertEqual(
                audit.live_request(
                    "test-only",
                    "https://api.github.com/repos/example/issues",
                    control,
                    lambda: 1000,
                ),
                [],
            )
            self.assertEqual(control, {"quota_wait_until": 2000})
            with self.assertRaises(audit.AuditPaused):
                audit.live_request(
                    "test-only",
                    "https://api.github.com/repos/example/issues",
                    control,
                    lambda: 1001,
                )
            self.assertEqual(call.call_count, 1)

    def test_remote_disconnect_is_retried_and_repeated_failure_remains_unproven(self):
        class Response(io.BytesIO):
            headers = {}

        with (
            patch.object(audit.time, "sleep"),
            patch.object(
                audit,
                "http_open",
                side_effect=[audit.http.client.RemoteDisconnected("closed"), Response(b"[]")],
            ) as net,
        ):
            self.assertEqual(
                audit.live_request(
                    "test-only", "https://api.github.com/repos/example/issues", {}, lambda: 1000
                ),
                [],
            )
            self.assertEqual(net.call_count, 2)
        with (
            patch.object(audit.time, "sleep"),
            patch.object(
                audit, "http_open", side_effect=audit.http.client.RemoteDisconnected("closed")
            ) as net,
        ):
            with self.assertRaisesRegex(RuntimeError, "audit API transport error"):
                audit.live_request(
                    "test-only", "https://api.github.com/repos/example/issues", {}, lambda: 1000
                )
            self.assertEqual(net.call_count, 4)

    def test_budget_pause_persists_partial_repo_and_resume_finishes_remaining_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            c = sqlite3.connect(root / "state.db")
            c.executescript("""CREATE TABLE repo(id INTEGER,name TEXT,available INTEGER);
                CREATE TABLE object(repo_id INTEGER,kind TEXT,id TEXT,body TEXT);
                CREATE TABLE presence(repo_id INTEGER,kind TEXT,id TEXT,scope TEXT,present INTEGER);
                INSERT INTO repo VALUES(1,'demo',1);
                INSERT INTO presence VALUES(1,'issue','1','issue',1);""")
            c.execute(
                "INSERT INTO object VALUES(?,?,?,?)",
                (1, "issue", "1", json.dumps({"id": 1, "number": 1})),
            )
            c.commit()

            class API:
                def __init__(self, conn):
                    pass

                def github_token(self):
                    return "test-only"

                def pages(self, path, cached=False):
                    return self.request("https://api.github.com" + path, cached=cached)

            fake = SimpleNamespace(
                ROOT=root,
                OWNER="example",
                API=API,
                db=lambda: c,
                setting=lambda conn, key: None,
                now=lambda: 1000,
                decode=json.loads,
                LOW=500,
                free=lambda: 1000000,
            )
            with (
                patch.object(audit, "load_archive", return_value=fake),
                patch.object(audit, "live_request", return_value=[]) as reads,
            ):
                clock = iter([0, 0, 2])
                with (
                    patch.object(
                        sys, "argv", ["audit", "--live", "--resume", "--max-seconds", "1"]
                    ),
                    patch.object(audit.time, "monotonic", side_effect=lambda: next(clock)),
                ):
                    with self.assertRaises(audit.AuditPaused):
                        audit.main()
                partial = json.loads((root / "audit-children-live.json").read_text())
                self.assertNotIn("finished", partial)
                self.assertTrue(partial["repositories"][0]["in_progress"])
                self.assertEqual(len(partial["repositories"][0]["collections"]), 1)
                with (
                    patch.object(sys, "argv", ["audit", "--live", "--resume"]),
                    patch.object(audit.time, "monotonic", return_value=0),
                ):
                    audit.main()
                finished = json.loads((root / "audit-children-live.json").read_text())
                self.assertTrue(finished["repositories"][0]["ok"])
                self.assertFalse(finished["repositories"][0]["in_progress"])
                self.assertEqual(len(finished["repositories"][0]["collections"]), 4)
                self.assertEqual(reads.call_count, 4)
                # An observed failure must persist without preventing traversal
                # on subsequent batches, and must never turn into a pass.
                c.execute(
                    "INSERT INTO object VALUES(?,?,?,?)",
                    (1, "issue_comment", "41", json.dumps({"id": 41, "body": "stale"})),
                )
                c.execute(
                    "INSERT INTO presence VALUES(?,?,?,?,?)",
                    (1, "issue_comment", "41", "issue:1", 1),
                )
                c.commit()
                for _ in range(2):
                    with (
                        patch.object(sys, "argv", ["audit", "--live", "--resume"]),
                        patch.object(audit.time, "monotonic", return_value=0),
                    ):
                        audit.main()
                    failed = json.loads((root / "audit-children-live.json").read_text())
                    self.assertFalse(failed["repositories"][0]["ok"])
                    self.assertEqual(failed["repositories"][0]["collections"][0]["extra"], ["41"])
                self.assertEqual(reads.call_count, 5)
            c.close()


if __name__ == "__main__":
    unittest.main()

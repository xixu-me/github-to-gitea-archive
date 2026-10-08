"""Lifecycle acceptance runs against public or installed production runtime."""

import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "github_archive.lifecycle_runtime",
    os.environ.get(
        "ARCHIVE_TEST_MODULE",
        str(Path(__file__).resolve().parents[1] / "src/github_archive/archive.py"),
    ),
)
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.multiple(a, ROOT=Path(self.tmp.name), OWNER="example-user")
        self.patch.start()
        self.c = a.db()
        self.repo = {
            "id": 42,
            "name": "demo",
            "owner": {"login": a.OWNER},
            "private": True,
            "description": "Original description",
        }
        self.c.execute(
            "INSERT INTO repo(id,name,local_name,private,source) VALUES(42,'demo','demo',1,?)",
            (a.encode(self.repo),),
        )
        self.c.commit()

    def tearDown(self):
        self.c.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_deleted_webhook_preserves_data_and_is_idempotent(self):
        a.save_objects(self.c, 42, "issue", [{"id": 10, "number": 1, "body": "retained"}])
        payload = {"action": "deleted", "repository": self.repo}
        a.apply_delivery(self.c, payload, "delivery-1", "repository")
        a.apply_delivery(self.c, payload, "delivery-1", "repository")
        self.assertEqual(a.source_state(self.c, 42)["state"], "deleted")
        self.assertEqual(self.c.execute("SELECT available FROM repo").fetchone()[0], 0)
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM object WHERE kind='issue'").fetchone()[0], 1
        )
        self.assertEqual(
            self.c.execute(
                "SELECT count(*) FROM object WHERE kind='repository_lifecycle'"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(self.c.execute("SELECT kind FROM job").fetchone()[0], "source-state")

    def test_transfer_outside_owner_and_return_by_same_id(self):
        transferred = dict(self.repo, owner={"login": "another-owner"})
        a.apply_delivery(
            self.c, {"action": "transferred", "repository": transferred}, "transfer", "repository"
        )
        self.assertEqual(a.source_state(self.c, 42)["destination"], "another-owner")
        self.assertEqual(a.source_state(self.c, 42)["state"], "transferred")
        a.reconcile_source_inventory(self.c, [], a.time.time_ns())
        self.assertEqual(a.source_state(self.c, 42)["state"], "transferred")
        a.reconcile_source_inventory(self.c, [self.repo], a.time.time_ns())
        self.assertEqual(a.source_state(self.c, 42)["state"], "active")
        self.assertEqual(
            a.source_description(self.c, 42, "Original description"), "Original description"
        )

    def test_installation_removal_marks_unavailable_without_claiming_deletion(self):
        a.apply_delivery(
            self.c,
            {"action": "removed", "repositories_removed": [{"id": 42}]},
            "access-removed",
            "installation_repositories",
        )
        self.assertEqual(a.source_state(self.c, 42)["state"], "unavailable")
        self.assertEqual(
            a.source_state(self.c, 42)["evidence"], "signed-webhook:installation-access-removed"
        )
        self.assertEqual(self.c.execute("SELECT available FROM repo").fetchone()[0], 0)

    def test_access_removal_after_deletion_or_transfer_preserves_stronger_evidence(self):
        for state in ("deleted", "transferred"):
            with self.subTest(state=state):
                a.set_source_state(self.c, 42, state, "signed-webhook:" + state)
                a.apply_delivery(
                    self.c,
                    {"action": "removed", "repositories_removed": [{"id": 42}]},
                    "access-removed-after-" + state,
                    "installation_repositories",
                )
                self.assertEqual(a.source_state(self.c, 42)["state"], state)
                self.assertEqual(a.source_state(self.c, 42)["evidence"], "signed-webhook:" + state)

    def test_absence_is_not_proof_of_deletion(self):
        a.reconcile_source_inventory(self.c, [], a.time.time_ns())
        self.assertEqual(a.source_state(self.c, 42)["state"], "unavailable")
        count = self.c.execute("SELECT count(*) FROM object").fetchone()[0]
        a.reconcile_source_inventory(self.c, [], a.time.time_ns())
        self.assertEqual(self.c.execute("SELECT count(*) FROM object").fetchone()[0], count)

    def test_older_inventory_cannot_resurrect_newer_deletion(self):
        started = a.time.time_ns()
        a.apply_delivery(
            self.c, {"action": "deleted", "repository": self.repo}, "deleted", "repository"
        )
        a.reconcile_source_inventory(self.c, [self.repo], started)
        self.assertEqual(a.source_state(self.c, 42)["state"], "deleted")
        self.assertEqual(self.c.execute("SELECT available FROM repo").fetchone()[0], 0)

    def test_missing_parent_cascades_through_comments_reviews_and_assets(self):
        inventories = [
            ("issue", [{"id": 10, "number": 1}], "issue"),
            ("pull", [{"id": 20, "number": 2}], "pull"),
            ("release", [{"id": 30}], "release"),
            ("issue_comment", [{"id": 11}], "issue:1"),
            ("comment_reaction", [{"id": 12}], "comment:11"),
            ("issue_comment", [{"id": 21}], "issue:2"),
            ("pull_detail", [{"id": 20}], "pull-detail:2"),
            ("review", [{"id": 22}], "pull:2"),
            ("review_comment", [{"id": 23}], "pull:2"),
            ("review_comment_reaction", [{"id": 24}], "review-comment:23"),
            ("release_asset", [{"id": 31}], "release:30"),
        ]
        for kind, values, scope in inventories:
            a.save_objects(self.c, 42, kind, values, scope)
        for kind in ("issue", "pull", "release"):
            a.save_objects(self.c, 42, kind, [])
        a.cascade_missing_children(self.c, 42)
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM presence WHERE present=1").fetchone()[0], 0
        )
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM object").fetchone()[0], len(inventories)
        )
        # Re-observed collections restore current presence without losing old data.
        for kind, values, scope in inventories:
            a.save_objects(self.c, 42, kind, values, scope)
        a.cascade_missing_children(self.c, 42)
        self.assertEqual(
            self.c.execute("SELECT count(*) FROM presence WHERE present=1").fetchone()[0],
            len(inventories),
        )

    def test_deleted_comment_cascades_reactions_with_active_parent(self):
        a.save_objects(self.c, 42, "issue", [{"id": 10, "number": 1}])
        a.save_objects(self.c, 42, "issue_comment", [{"id": 11}], "issue:1")
        a.save_objects(self.c, 42, "comment_reaction", [{"id": 12}], "comment:11")
        a.save_objects(self.c, 42, "issue_comment", [], "issue:1")
        a.cascade_missing_children(self.c, 42)
        self.assertEqual(
            self.c.execute("SELECT present FROM presence WHERE kind='comment_reaction'").fetchone()[
                0
            ],
            0,
        )
        self.assertEqual(
            self.c.execute("SELECT present FROM presence WHERE kind='issue'").fetchone()[0], 1
        )

    def test_projection_marks_and_restores_description_without_deleting(self):
        target = {"description": "Original description"}
        calls = []

        class Fake:
            def gt(self, path, method="GET", data=None):
                calls.append(method)
                if method == "PATCH":
                    target.update(data)
                return target

        a.set_source_state(self.c, 42, "deleted", "signed-webhook:deleted")
        row = self.c.execute("SELECT * FROM repo").fetchone()
        a.project_source_state(self.c, Fake(), row)
        self.assertIn("Source deleted; archive retained", target["description"])
        a.set_source_state(self.c, 42, "active", "inventory")
        a.project_source_state(self.c, Fake(), row)
        self.assertEqual(target["description"], "Original description")
        self.assertNotIn("DELETE", calls)

    def test_discovery_error_does_not_mark_every_repository_missing(self):
        class Fake:
            def repositories(self):
                raise RuntimeError("transport failure")

            def pages(self, *args, **kwargs):
                raise RuntimeError("transport failure")

        with self.assertRaisesRegex(RuntimeError, "transport failure"):
            a.discover(self.c, Fake())
        self.assertEqual(self.c.execute("SELECT available FROM repo").fetchone()[0], 1)
        self.assertEqual(self.c.execute("SELECT count(*) FROM object").fetchone()[0], 0)

    def test_release_projection_cannot_resurrect_deleted_source_tags(self):
        bare = a.ROOT / "demo.git"
        subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
        sha = (
            subprocess.run(
                ["git", "--git-dir", str(bare), "hash-object", "-w", "--stdin"],
                input=b"retained",
                check=True,
                capture_output=True,
            )
            .stdout.decode()
            .strip()
        )
        a.git(["--git-dir", str(bare), "update-ref", "refs/tags/active", sha])
        a.git(["--git-dir", str(bare), "update-ref", "refs/archive/history/test", sha])
        with a.preserve_source_tags(bare):
            a.git(["--git-dir", str(bare), "update-ref", "refs/tags/deleted-upstream", sha])
        refs = a.git(["--git-dir", str(bare), "for-each-ref", "--format=%(refname)"])
        self.assertNotIn("refs/tags/deleted-upstream", refs)
        self.assertIn("refs/tags/active", refs)
        self.assertIn("refs/archive/history/test", refs)
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with a.preserve_source_tags(bare):
                a.git(["--git-dir", str(bare), "update-ref", "refs/tags/deleted-upstream", sha])
                raise RuntimeError("interrupted")
        refs = a.git(["--git-dir", str(bare), "for-each-ref", "--format=%(refname)"])
        self.assertNotIn("refs/tags/deleted-upstream", refs)

    def test_active_inventory_preserves_real_sync_error(self):
        a.set_source_state(self.c, 42, "active", "inventory")
        self.c.execute("UPDATE repo SET error='real sync failure'")
        self.c.commit()
        a.reconcile_source_inventory(self.c, [self.repo], a.time.time_ns())
        self.assertEqual(
            self.c.execute("SELECT error FROM repo").fetchone()[0], "real sync failure"
        )


if __name__ == "__main__":
    unittest.main()

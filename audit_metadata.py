#!/usr/bin/env python3
"""Independent live parent inventories and local PR/asset completeness audit.

GET requests bypass the worker's response cache. Reports stay on the archive host.
This does not claim independent live validation of every child comment/review.
"""

from audit_common import load_archive
import json
import sqlite3
import urllib.parse
from audit_children import AuditPaused, live_request

a = load_archive()
import argparse

argparse.ArgumentParser(description=__doc__).parse_args()
c = sqlite3.connect("file:" + str(a.ROOT / "state.db") + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row


class ReadAuditAPI(a.API):
    def request(self, url, method="GET", **kwargs):
        if method != "GET":
            raise RuntimeError("audit performs read-only GET requests")
        if a.free() < a.LOW:
            raise AuditPaused("capacity reserve reached")
        if int(a.setting(c, "github_backoff_until") or 0) > a.now():
            raise AuditPaused("production API backoff active")
        return live_request(self.github_token(), url, report, a.now)


api = ReadAuditAPI(c)
report = {
    "started": a.now(),
    "scope": "Live owner inventory, issues, PRs, releases, labels, milestones; release asset IDs; archived full PR details and file manifests",
    "repositories": [],
}
destination = a.ROOT / "audit-metadata.json"


def save():
    temp = destination.with_suffix(".next.json")
    temp.write_text(json.dumps(report, ensure_ascii=False))
    temp.chmod(0o600)
    temp.replace(destination)


try:
    live = api.repositories()
except AuditPaused as error:
    report["paused"] = str(error)
    save()
    c.close()
    raise SystemExit(2)
live = [r for r in live if r["owner"]["login"].lower() == a.OWNER.lower()]
rows = {r["id"]: r for r in c.execute("SELECT * FROM repo WHERE available=1").fetchall()}
report["owner_inventory"] = {
    "live": len(live),
    "archived": len(rows),
    "missing": sorted({r["id"] for r in live} - rows.keys()),
    "extra": sorted(rows.keys() - {r["id"] for r in live}),
}
save()

for source in sorted(live, key=lambda r: r["name"].lower()):
    rid = source["id"]
    item = {"id": rid, "name": source["name"], "checked": a.now(), "inventories": {}}
    try:
        if rid not in rows:
            raise RuntimeError("live repository not yet discovered")
        base = "/repos/" + a.OWNER + "/" + urllib.parse.quote(source["name"])
        parents = {}
        for kind, endpoint in [
            ("issue", "/issues?state=all"),
            ("pull", "/pulls?state=all"),
            ("release", "/releases"),
            ("label", "/labels"),
            ("milestone", "/milestones?state=all"),
        ]:
            try:
                values = api.pages(base + endpoint, cached=False)
            except RuntimeError as e:
                if (
                    kind == "pull"
                    and source.get("has_pull_requests") is False
                    and "HTTP 404" in str(e)
                ):
                    item["pull_feature_gap"] = "disabled upstream, verified HTTP 404"
                    values = []
                else:
                    raise
            if kind == "issue":
                values = [x for x in values if "pull_request" not in x]
            parents[kind] = values
            expected = {str(x["id"]) for x in values}
            actual = {
                r[0]
                for r in c.execute(
                    "SELECT id FROM presence WHERE repo_id=? AND kind=? AND scope=? AND present=1",
                    (rid, kind, kind),
                ).fetchall()
            }
            stale = []
            for value in values:
                raw = c.execute(
                    "SELECT body FROM object WHERE repo_id=? AND kind=? AND id=?",
                    (rid, kind, str(value["id"])),
                ).fetchone()
                if raw:
                    archived = a.decode(raw[0])
                    # Compare every source field, including bodies and nested
                    # authors/labels, rather than only IDs and parent timestamps.
                    if any(
                        archived.get(key) != field
                        for key, field in value.items()
                        if not (kind == "release" and key == "assets")
                    ):
                        stale.append(str(value["id"]))
            item["inventories"][kind] = {
                "source": len(expected),
                "archive": len(actual),
                "missing": sorted(expected - actual),
                "extra": sorted(actual - expected),
                "stale": stale,
            }
        asset_mismatches = []
        for release in parents["release"]:
            # Use the dedicated endpoint if the fresh embedded list differs from
            # the archive; an embedded truncation alone cannot prove data loss.
            assets = release.get("assets", [])
            expected = {str(x["id"]) for x in assets}
            actual = {
                r[0]
                for r in c.execute(
                    "SELECT id FROM presence WHERE repo_id=? AND kind='release_asset' AND scope=? AND present=1",
                    (rid, f"release:{release['id']}"),
                ).fetchall()
            }
            if expected != actual:
                assets = api.pages(base + f"/releases/{release['id']}/assets", cached=False)
                expected = {str(x["id"]) for x in assets}
            changed = []
            for asset in assets:
                raw = c.execute(
                    "SELECT body FROM object WHERE repo_id=? AND kind='release_asset' AND id=?",
                    (rid, str(asset["id"])),
                ).fetchone()
                if raw:
                    archived = a.decode(raw[0])
                    if any(archived.get(key) != value for key, value in asset.items()):
                        changed.append(str(asset["id"]))
            if expected != actual or changed:
                asset_mismatches.append(
                    {
                        "release": release["id"],
                        "missing": sorted(expected - actual),
                        "extra": sorted(actual - expected),
                        "changed": changed,
                    }
                )
        item["asset_mismatches"] = asset_mismatches
        missing_details, missing_manifests, invalid_manifests, patch_limits = [], [], [], []
        for pull in parents["pull"]:
            number = pull["number"]
            detail = c.execute(
                "SELECT body FROM object WHERE repo_id=? AND kind='pull_detail' AND id=?",
                (rid, str(pull["id"])),
            ).fetchone()
            manifest = c.execute(
                "SELECT body FROM object WHERE repo_id=? AND kind='pull_content_manifest' AND id=?",
                (rid, str(number)),
            ).fetchone()
            if not detail:
                missing_details.append(number)
            if not manifest:
                missing_manifests.append(number)
                continue
            m = a.decode(manifest[0])
            count = c.execute(
                "SELECT count(*) FROM presence WHERE repo_id=? AND kind='pull_file' AND scope=? AND present=1",
                (rid, f"pull:{number}"),
            ).fetchone()[0]
            d = a.decode(detail[0]) if detail else {}
            if count != m.get("archived_files") or (
                detail and d.get("changed_files") != m.get("expected_files")
            ):
                invalid_manifests.append(number)
            if m.get("file_list_complete") is not True or m.get("files_without_patch"):
                patch_limits.append(
                    {
                        "number": number,
                        "expected_files": m.get("expected_files"),
                        "archived_files": count,
                        "missing_patches": len(m.get("files_without_patch", [])),
                    }
                )
        item.update(
            missing_details=missing_details,
            missing_manifests=missing_manifests,
            invalid_manifests=invalid_manifests,
            patch_limits=patch_limits,
        )
        item["ok"] = not (
            asset_mismatches
            or missing_details
            or missing_manifests
            or invalid_manifests
            or any(v["missing"] or v["extra"] or v["stale"] for v in item["inventories"].values())
        )
    except AuditPaused as e:
        report["paused"] = str(e)
        item["error"] = str(e)
        item["ok"] = False
        report["repositories"].append(item)
        save()
        c.close()
        raise SystemExit(2)
    except Exception as e:
        item["error"] = str(e)[:250]
        item["ok"] = False
    report["repositories"].append(item)
    save()
    print("AUDIT", item["name"], "OK" if item["ok"] else "NEEDS_SYNC", flush=True)

report["finished"] = a.now()
report["ok"] = (
    all(x["ok"] for x in report["repositories"])
    and not report["owner_inventory"]["missing"]
    and not report["owner_inventory"]["extra"]
)
save()
print(
    "COMPLETE",
    len(report["repositories"]),
    sum(x["ok"] for x in report["repositories"]),
    flush=True,
)
c.close()

raise SystemExit(0 if report["ok"] else 1)

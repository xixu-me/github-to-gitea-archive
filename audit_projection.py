#!/usr/bin/env python3
"""Read-only audit of native Gitea rows against durable projection fingerprints.

Source records without native mappings are reported separately; unrepresentable
PRs require the explicit projection-gap records rather than fabricated issues.
"""

import hashlib
import json
import sqlite3
import zlib
import datetime
import time

from audit_common import load_archive
import argparse

a = load_archive()
argparse.ArgumentParser(description=__doc__).parse_args()
ROOT = a.ROOT
c = sqlite3.connect("file:" + str(ROOT / "state.db") + "?mode=ro", uri=True, timeout=60)
c.row_factory = sqlite3.Row
g = sqlite3.connect("file:" + str(a.GITEA_DB) + "?mode=ro", uri=True, timeout=60)
g.row_factory = sqlite3.Row


def decode(blob):
    return json.loads(zlib.decompress(blob))


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def source_object(rid, kind, sid):
    raw = c.execute(
        "SELECT body FROM object WHERE repo_id=? AND kind=? AND id=?", (rid, kind, sid)
    ).fetchone()
    if not raw:
        return None
    value = decode(raw[0])
    if kind == "pull":
        detail = c.execute(
            "SELECT body FROM object WHERE repo_id=? AND kind='pull_detail' AND id=?", (rid, sid)
        ).fetchone()
        if detail:
            value = dict(decode(detail[0]), **value)
        if value.get("merged_at"):
            value["merged"] = True
    return value


def source_body(value):
    author = (value.get("user") or value.get("author") or {}).get("login", "")
    state = (
        "\nGitHub PR status: merged, merge commit " + str(value.get("merge_commit_sha") or "")
        if value.get("merged")
        else ""
    )
    return (
        (value.get("body") or "")
        + f"\n\n---\nGitHub source: {value['html_url']}\nAuthor: {author} · Created: {value.get('created_at', '')}{state}\n<!-- github-archive:{value['id']} -->"
    )


def mapped_id(rid, kind, sid):
    row = c.execute(
        "SELECT target_id FROM mapping WHERE repo_id=? AND kind=? AND source_id=?",
        (rid, kind, str(sid)),
    ).fetchone()
    return int(row[0]) if row else 0


def expected_active_payload(rid, kind, sid):
    source_kind = "issue_comment" if kind == "comment" else kind
    present = c.execute(
        "SELECT present FROM presence WHERE repo_id=? AND kind=? AND id=? ORDER BY (scope=?) DESC,checked DESC LIMIT 1",
        (rid, source_kind, sid, source_kind),
    ).fetchone()
    if not present or not present[0]:
        return None
    x = source_object(rid, source_kind, sid)
    if not x:
        return None
    if kind == "label":
        return {"name": x["name"], "color": x["color"], "description": x.get("description") or ""}
    if kind in ("issue", "pull"):
        return {
            "title": x["title"],
            "body": source_body(x),
            "state": x["state"],
            "milestone": mapped_id(rid, "milestone", (x.get("milestone") or {}).get("id")),
            "_labels": [
                mapped_id(rid, "label", label["id"])
                for label in x.get("labels", [])
                if mapped_id(rid, "label", label["id"])
            ],
        }
    if kind == "comment":
        return {"body": source_body(x)}
    if kind == "milestone":
        payload = {
            "title": x["title"],
            "description": x.get("description") or "",
            "state": x["state"],
        }
        if x.get("due_on"):
            payload["due_on"] = x["due_on"]
        return payload
    if kind == "release":
        links = "\n".join(
            f"- [{v['name']}]({v['browser_download_url']}) · {v['size']} bytes · {v.get('digest') or 'upstream checksum unavailable'}"
            for v in x.get("assets", [])
        )
        return {
            "tag_name": x["tag_name"],
            "target_commitish": x["target_commitish"],
            "name": x.get("name") or x["tag_name"],
            "body": source_body(x) + "\n\nAssets (links only):\n" + links,
            "draft": x["draft"],
            "prerelease": x["prerelease"],
        }


ref_report = ROOT / "audit-code-full.json"
if not ref_report.exists():
    ref_report = ROOT / "audit-code.json"
refs = {x["id"]: x for x in json.loads(ref_report.read_text())["repositories"]}
report = {
    "checked": int(time.time()),
    "scope": "All available owner repositories: native repository state, actual projected rows against durable fingerprints and latest active raw source fields, explicit gaps for unmapped records",
    "repositories": [],
}
for r in c.execute("SELECT * FROM repo WHERE available=1 ORDER BY name").fetchall():
    rid = r["id"]
    native = g.execute(
        "SELECT r.* FROM repository r JOIN user u ON u.id=r.owner_id WHERE u.lower_name=? AND r.lower_name=?",
        (a.GITEA_OWNER.lower(), (r["local_name"] or r["name"]).lower()),
    ).fetchone()
    item = {
        "id": rid,
        "name": r["name"],
        "checked_mappings": 0,
        "mismatches": [],
        "source_mismatches": [],
        "missing_targets": [],
        "unmapped": {},
        "unexplained_unmapped": {},
    }
    if native is None:
        item["missing_repository"] = True
        report["repositories"].append(item)
        continue
    source = decode(r["source"])
    branches = [key for key in refs.get(rid, {}).get("actual", {}) if key.startswith("refs/heads/")]
    item["repository_mismatches"] = [
        field
        for field, ok in {
            "name": native["name"] == r["name"],
            "private": bool(native["is_private"]) == bool(source["private"]),
            "default_branch": not branches
            or native["default_branch"] == source.get("default_branch"),
            "is_empty": bool(native["is_empty"]) == (not branches),
            "is_mirror": not native["is_mirror"],
        }.items()
        if not ok
    ]
    for m in c.execute("SELECT * FROM mapping WHERE repo_id=?", (rid,)).fetchall():
        kind, tid = m["kind"], m["target_id"]
        payload = None
        if kind == "label":
            x = g.execute(
                "SELECT * FROM label WHERE repo_id=? AND id=?", (native["id"], tid)
            ).fetchone()
            if x:
                color = x["color"].lstrip("#")
                source_row = c.execute(
                    "SELECT body FROM object WHERE repo_id=? AND kind='label' AND id=?",
                    (rid, m["source_id"]),
                ).fetchone()
                source_color = decode(source_row[0])["color"] if source_row else color
                if color.lower() == source_color.lower():
                    color = source_color
                payload = {"name": x["name"], "color": color, "description": x["description"]}
        elif kind in ("issue", "pull"):
            x = g.execute(
                'SELECT * FROM issue WHERE repo_id=? AND "index"=?', (native["id"], tid)
            ).fetchone()
            if x:
                source_row = c.execute(
                    "SELECT body FROM object WHERE repo_id=? AND kind=? AND id=?",
                    (rid, kind, m["source_id"]),
                ).fetchone()
                source_item = decode(source_row[0]) if source_row else {}
                source_labels = []
                for label in source_item.get("labels", []):
                    lm = c.execute(
                        "SELECT target_id FROM mapping WHERE repo_id=? AND kind='label' AND source_id=?",
                        (rid, str(label["id"])),
                    ).fetchone()
                    if lm:
                        source_labels.append(int(lm[0]))
                actual_labels = {
                    v[0]
                    for v in g.execute(
                        "SELECT label_id FROM issue_label WHERE issue_id=?", (x["id"],)
                    ).fetchall()
                }
                # Label order is not a native property; retain the source order
                # when sets match so equivalent label sets do not look different.
                labels = (
                    source_labels if actual_labels == set(source_labels) else sorted(actual_labels)
                )
                payload = {
                    "title": x["name"],
                    "body": x["content"],
                    "state": "closed" if x["is_closed"] else "open",
                    "milestone": x["milestone_id"],
                    "_labels": labels,
                }
                if bool(x["is_pull"]) != (kind == "pull"):
                    item["mismatches"].append(
                        {
                            "kind": kind,
                            "source_id": m["source_id"],
                            "target": tid,
                            "reason": "native issue/PR type differs",
                        }
                    )
        elif kind == "comment":
            x = g.execute(
                "SELECT x.* FROM comment x JOIN issue i ON i.id=x.issue_id WHERE i.repo_id=? AND x.id=?",
                (native["id"], tid),
            ).fetchone()
            if x:
                payload = {"body": x["content"]}
        elif kind == "release":
            x = g.execute(
                "SELECT * FROM release WHERE repo_id=? AND id=?", (native["id"], tid)
            ).fetchone()
            if x:
                payload = {
                    "tag_name": x["tag_name"],
                    "target_commitish": x["target"],
                    "name": x["title"],
                    "body": x["note"],
                    "draft": bool(x["is_draft"]),
                    "prerelease": bool(x["is_prerelease"]),
                }
        elif kind == "milestone":
            x = g.execute(
                "SELECT * FROM milestone WHERE repo_id=? AND id=?", (native["id"], tid)
            ).fetchone()
            if x:
                payload = {
                    "title": x["name"],
                    "description": x["content"],
                    "state": "closed" if x["is_closed"] else "open",
                }
                source_row = c.execute(
                    "SELECT body FROM object WHERE repo_id=? AND kind='milestone' AND id=?",
                    (rid, m["source_id"]),
                ).fetchone()
                due = decode(source_row[0]).get("due_on") if source_row else None
                if due:
                    expected_epoch = int(
                        datetime.datetime.fromisoformat(due.replace("Z", "+00:00")).timestamp()
                    )
                    payload["due_on"] = (
                        due if expected_epoch == x["deadline_unix"] else str(x["deadline_unix"])
                    )
        else:
            continue
        item["checked_mappings"] += 1
        if payload is None:
            item["missing_targets"].append(
                {"kind": kind, "source_id": m["source_id"], "target": tid}
            )
        elif digest(payload) != m["hash"]:
            item["mismatches"].append(
                {
                    "kind": kind,
                    "source_id": m["source_id"],
                    "target": tid,
                    "reason": "native content differs from recorded projection fingerprint",
                }
            )
        expected = expected_active_payload(rid, kind, m["source_id"])
        if payload is not None and expected is not None and digest(payload) != digest(expected):
            item["source_mismatches"].append(
                {"kind": kind, "source_id": m["source_id"], "target": tid}
            )
    for kind in ["issue", "pull", "label", "milestone", "release", "issue_comment"]:
        mapping_kind = "comment" if kind == "issue_comment" else kind
        rows = c.execute(
            """SELECT DISTINCT o.id FROM object o JOIN presence p
            ON p.repo_id=o.repo_id AND p.kind=o.kind AND p.id=o.id
            WHERE o.repo_id=? AND o.kind=? AND p.present=1
            AND NOT EXISTS(SELECT 1 FROM mapping m WHERE m.repo_id=o.repo_id AND m.kind=? AND m.source_id=o.id)""",
            (rid, kind, mapping_kind),
        ).fetchall()
        if rows:
            item["unmapped"][kind] = [v[0] for v in rows]
    raw_gaps = c.execute(
        "SELECT value FROM setting WHERE key=?", (f"projection_gaps:{rid}",)
    ).fetchone()
    gap_numbers = {
        x["number"]
        for x in json.loads(raw_gaps[0] if raw_gaps else "[]")
        if "number" in x and x.get("reason")
    }
    for kind, ids in item["unmapped"].items():
        for sid in ids:
            value = source_object(rid, kind, sid)
            number = (
                value.get("number")
                if kind == "pull"
                else int(value.get("issue_url", "/0").rsplit("/", 1)[-1])
                if kind == "issue_comment"
                else None
            )
            if kind not in ("pull", "issue_comment") or number not in gap_numbers:
                item["unexplained_unmapped"].setdefault(kind, []).append(sid)
    report["repositories"].append(item)

report["ok"] = all(
    not any(
        x.get(k)
        for k in (
            "missing_repository",
            "mismatches",
            "source_mismatches",
            "missing_targets",
            "unexplained_unmapped",
            "repository_mismatches",
        )
    )
    for x in report["repositories"]
)
p = ROOT / "audit-projection.json"
temp = p.with_suffix(".next.json")
temp.write_text(json.dumps(report, ensure_ascii=False))
temp.chmod(0o600)
temp.replace(p)
print("REPOSITORIES", len(report["repositories"]))
print("MAPPINGS", sum(x["checked_mappings"] for x in report["repositories"]))
print("MISMATCHES", sum(len(x["mismatches"]) for x in report["repositories"]))
print("MISSING_TARGETS", sum(len(x["missing_targets"]) for x in report["repositories"]))
print("LATEST_SOURCE_DIFFERENCES", sum(len(x["source_mismatches"]) for x in report["repositories"]))
print(
    "UNEXPLAINED_UNMAPPED",
    [
        (x["name"], {k: len(v) for k, v in x["unexplained_unmapped"].items()})
        for x in report["repositories"]
        if x["unexplained_unmapped"]
    ],
)
print(
    "REPOSITORY_FLAGS",
    [
        (x["name"], x.get("repository_mismatches"))
        for x in report["repositories"]
        if x.get("repository_mismatches")
    ],
)
c.close()
g.close()

raise SystemExit(0 if report["ok"] else 1)

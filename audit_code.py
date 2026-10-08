#!/usr/bin/env python3
"""Read-only upstream Git reference and local connectivity audit; no Git writes."""

from audit_common import load_archive
import sqlite3, json, argparse

a = load_archive()
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--full-objects", action="store_true")
options = parser.parse_args()

c = sqlite3.connect("file:" + str(a.ROOT / "state.db") + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row
api = a.API(c)
report = {
    "started": a.now(),
    "scope": "All available owner repositories; branches, annotated tag objects, PR head refs, default HEAD; "
    + (
        "full Git object validity"
        if options.full_objects
        else "local reachable-object connectivity"
    ),
    "repositories": [],
}
path = a.ROOT / ("audit-code-full.json" if options.full_objects else "audit-code.json")


def save():
    temp = path.with_suffix(".next.json")
    temp.write_text(json.dumps(report, ensure_ascii=False))
    temp.chmod(0o600)
    temp.replace(path)


for row in c.execute("SELECT * FROM repo WHERE available=1 ORDER BY name").fetchall():
    item = {"id": row["id"], "name": row["name"], "checked": a.now()}
    try:
        source = a.decode(row["source"])
        remote = a.git(
            ["ls-remote", source["clone_url"], "refs/heads/*", "refs/tags/*", "refs/pull/*/head"],
            api.github_token(),
            timeout=120,
        )
        expected = {
            line.split("\t", 1)[1]: line.split("\t", 1)[0]
            for line in remote.splitlines()
            if "\t" in line and not line.split("\t", 1)[1].endswith("^{}")
        }
        bare = a.GITROOT / ((row["local_name"] or row["name"]).lower() + ".git")
        current = a.git(
            [
                "--git-dir",
                str(bare),
                "for-each-ref",
                "--format=%(objectname) %(refname)",
                "refs/heads",
                "refs/tags",
                "refs/archive/pull",
            ],
            timeout=120,
        )
        actual = {
            line.split(" ", 1)[1].replace("refs/archive/pull/", "refs/pull/", 1): line.split(
                " ", 1
            )[0]
            for line in current.splitlines()
            if " " in line
        }
        item.update(refs_match=actual == expected, expected=expected, actual=actual)
        item["default_head"] = a.git(
            ["--git-dir", str(bare), "symbolic-ref", "HEAD"], timeout=30
        ).strip()
        branch = source.get("default_branch")
        item["default_head_ok"] = not expected or item["default_head"] == "refs/heads/" + str(
            branch
        )
        a.git(
            [
                "--git-dir",
                str(bare),
                "fsck",
                "--full" if options.full_objects else "--connectivity-only",
                "--no-dangling",
            ],
            timeout=600,
        )
        item["connectivity_ok"] = True
        if options.full_objects:
            item["full_objects_ok"] = True
    except Exception as e:
        item["error"] = str(e)[:250]
    report["repositories"].append(item)
    save()
    print(
        "AUDIT",
        item["name"],
        "OK"
        if item.get("refs_match") and item.get("connectivity_ok") and item.get("default_head_ok")
        else "NEEDS_SYNC",
        flush=True,
    )
report["finished"] = a.now()
save()
print(
    "COMPLETE",
    len(report["repositories"]),
    sum(
        bool(x.get("refs_match") and x.get("connectivity_ok") and x.get("default_head_ok"))
        for x in report["repositories"]
    ),
    flush=True,
)

c.close()
raise SystemExit(
    0
    if all(
        x.get("refs_match") and x.get("connectivity_ok") and x.get("default_head_ok")
        for x in report["repositories"]
    )
    else 1
)

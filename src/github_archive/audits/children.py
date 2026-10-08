#!/usr/bin/env python3
"""Validate all observed issue/PR child collections against archived records.

Default mode uses captured paginated API replies and states that limitation.
--live instead makes independent uncached GETs. Neither mode alters raw records.
"""

import argparse
from .common import load_archive
import json
import hashlib
import http.client
import sqlite3
import urllib.parse
import urllib.request
import urllib.error
import time
from ..archive import http_open


class AuditPaused(Exception):
    pass


def live_request(token, url, control, now):
    """Read only; reserve API quota for production and keep audit backoff local."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
        raise RuntimeError("audit accepts only GitHub HTTPS API reads")
    if control.get("quota_wait_until", 0) > now():
        raise AuditPaused("audit API quota reserve reached")
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-account-archive-audit/0.1",
        },
    )
    for attempt in range(4):
        try:
            with http_open(request, timeout=120) as response:
                raw = response.read(16 * 1024**2 + 1)
                if len(raw) > 16 * 1024**2:
                    raise RuntimeError("audit response exceeds bounded 16 MiB limit")
                remaining = response.headers.get("X-RateLimit-Remaining")
                if remaining is not None and int(remaining) <= 1500:
                    control["quota_wait_until"] = now() + min(
                        3600, max(60, int(response.headers.get("X-RateLimit-Reset", "0")) - now())
                    )
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code == 429 or (
                error.code == 403
                and (
                    error.headers.get("X-RateLimit-Remaining") == "0"
                    or error.headers.get("Retry-After")
                )
            ):
                delay = max(
                    int(error.headers.get("Retry-After", "60")),
                    int(error.headers.get("X-RateLimit-Reset", "0")) - now(),
                )
                control["quota_wait_until"] = now() + min(3600, max(60, delay))
                raise AuditPaused("audit API rate-limit backoff active") from None
            if error.code >= 500 and attempt < 3:
                time.sleep(2**attempt)
                continue
            raise RuntimeError(f"API GET {parsed.path}: HTTP {error.code}") from None
        except (TimeoutError, urllib.error.URLError, http.client.RemoteDisconnected):
            if attempt == 3:
                raise RuntimeError("audit API transport error") from None
            time.sleep(2**attempt)


def archive_digest(stored):
    return hashlib.sha256(
        json.dumps(stored, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def reusable(previous, stored, url, started, allow_failed=False):
    observed = previous and (
        previous.get("ok") or (allow_failed and "expected" in previous and "error" not in previous)
    )
    return bool(
        observed
        and previous.get("url") == url
        and previous.get("source_checked", 0) >= started
        and previous.get("archive_digest") == archive_digest(stored)
    )


def identity(value, kind, number):
    if kind == "pull_commit":
        return str(number) + ":" + value["sha"]
    if kind == "pull_file":
        return str(number) + ":" + value["filename"]
    return str(
        value.get("id")
        or value.get("sha")
        or value.get("number")
        or hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    )


def compare(values, stored, kind, number):
    expected = {identity(v, kind, number): v for v in values}
    missing = sorted(expected.keys() - stored.keys())
    extra = sorted(stored.keys() - expected.keys())
    changed = [
        key
        for key in expected.keys() & stored.keys()
        if any(stored[key].get(field) != value for field, value in expected[key].items())
    ]
    return {
        "expected": len(expected),
        "archived": len(stored),
        "missing": missing,
        "extra": extra,
        "changed": sorted(changed),
        "ok": not (missing or extra or changed),
    }


def main():
    a = load_archive()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=0)
    args = parser.parse_args()
    if args.resume and not args.live:
        parser.error("--resume requires --live")

    c = sqlite3.connect("file:" + str(a.ROOT / "state.db") + "?mode=ro", uri=True)
    try:
        return run_audit(a, c, args)
    finally:
        c.close()


def run_audit(a, c, args):
    c.row_factory = sqlite3.Row
    mode = "live" if args.live else "captured"
    destination = a.ROOT / f"audit-children-{mode}.json"
    report = {
        "started": a.now(),
        "mode": mode,
        "scope": "All available repositories; issue/PR comments, events, timelines, reactions, reviews, review comments, commits and files; reaction collections for comments",
        "repositories": [],
    }
    if args.resume and destination.exists():
        report = json.loads(destination.read_text())
        if report.get("mode") != "live":
            raise RuntimeError("resume report mode mismatch")
        report.pop("finished", None)
    previous_repos = {r["id"]: r for r in report["repositories"]}
    rows = c.execute("SELECT * FROM repo WHERE available=1 ORDER BY name").fetchall()
    current_repos = {r["id"]: previous_repos[r["id"]] for r in rows if r["id"] in previous_repos}
    deadline = time.monotonic() + args.max_seconds if args.max_seconds else float("inf")
    if (
        args.live
        and max(int(a.setting(c, "github_backoff_until") or 0), report.get("quota_wait_until", 0))
        > a.now()
    ):
        print("PAUSED GitHub rate-limit backoff active", flush=True)
        return 2

    class ReadAuditAPI(a.API):
        def request(self, url, method="GET", **kwargs):
            if method != "GET":
                raise RuntimeError("audit performs read-only GET requests")
            if a.free() < a.LOW:
                raise AuditPaused("capacity reserve reached")
            return live_request(self.github_token(), url, report, a.now)

    api = ReadAuditAPI(c)

    def save():
        report["repositories"] = sorted(current_repos.values(), key=lambda r: r["name"])
        temp = destination.with_suffix(".next.json")
        temp.write_text(json.dumps(report, ensure_ascii=False))
        temp.chmod(0o600)
        temp.replace(destination)

    for row in rows:
        rid = row["id"]
        base = "/repos/" + a.OWNER + "/" + urllib.parse.quote(row["name"])
        item = {"id": rid, "name": row["name"], "checked": a.now(), "collections": []}
        current_repos[rid] = item
        previous_collections = {
            (x["kind"], x["scope"]): x for x in previous_repos.get(rid, {}).get("collections", [])
        }
        # Keep checkpoints for collections not reached yet when this invocation
        # ends mid-repository. Completed traversal prunes obsolete scopes.
        checkpoints = dict(previous_collections)
        item["in_progress"] = True

        def collection(kind, scope, path, number):
            if time.monotonic() >= deadline:
                item["collections"] = list(checkpoints.values())
                item["ok"] = False
                save()
                raise AuditPaused("time budget reached")
            url = "https://api.github.com" + base + path
            entry = {"kind": kind, "scope": scope, "url": url}
            values = None
            archived = {
                r["id"]: a.decode(r["body"])
                for r in c.execute(
                    """
                SELECT o.id,o.body FROM object o JOIN presence p
                ON p.repo_id=o.repo_id AND p.kind=o.kind AND p.id=o.id
                WHERE p.repo_id=? AND p.kind=? AND p.scope=? AND p.present=1""",
                    (rid, kind, scope),
                ).fetchall()
            }
            previous = previous_collections.get((kind, scope))
            if args.resume and reusable(
                previous, archived, url, report["started"], allow_failed=True
            ):
                item["collections"].append(previous)
                if kind in ("issue_comment", "review_comment"):
                    ids = previous.get("source_ids")
                    if ids is None:
                        ids = sorted(
                            (archived.keys() | set(previous.get("missing", [])))
                            - set(previous.get("extra", []))
                        )
                    return [{"id": oid} for oid in ids]
                return list(archived.values())
            try:
                if args.live:
                    values = api.pages(base + path, cached=False)
                    entry["source_checked"] = a.now()
                else:
                    values = []
                    for page in range(1, 10001):
                        page_url = url + f"?per_page=100&page={page}"
                        cached = c.execute(
                            "SELECT body,checked FROM resource WHERE url=?", (page_url,)
                        ).fetchone()
                        if cached is None:
                            raise RuntimeError(
                                f"captured API page {page} missing; source coverage unproven"
                            )
                        batch = a.decode(cached["body"])
                        if not isinstance(batch, list):
                            raise RuntimeError("captured API response is not an array")
                        values.extend(batch)
                        entry["source_checked"] = min(
                            entry.get("source_checked", cached["checked"]), cached["checked"]
                        )
                        if len(batch) < 100:
                            break
                    else:
                        raise RuntimeError("captured pagination did not terminate")
                entry.update(compare(values, archived, kind, number))
                if kind in ("issue_comment", "review_comment"):
                    entry["source_ids"] = [str(v["id"]) for v in values]
                entry["archive_digest"] = archive_digest(archived)
            except Exception as e:
                if isinstance(e, AuditPaused):
                    item["collections"] = list(checkpoints.values())
                    item["ok"] = False
                    save()
                    raise
                entry.update(ok=False, error=str(e)[:250])
                values = None
            item["collections"].append(entry)
            checkpoints[(kind, scope)] = entry
            if len(item["collections"]) % 20 == 0:
                reached = item["collections"]
                item["collections"] = list(checkpoints.values())
                save()
                item["collections"] = reached
            return values

        parents = c.execute(
            """SELECT o.kind,o.body FROM object o JOIN presence p
            ON p.repo_id=o.repo_id AND p.kind=o.kind AND p.id=o.id
            WHERE o.repo_id=? AND o.kind IN ('issue','pull') AND p.scope=o.kind AND p.present=1""",
            (rid,),
        ).fetchall()
        for parent in parents:
            value = a.decode(parent["body"])
            number = value["number"]
            scope = f"issue:{number}"
            comments = None
            for kind, endpoint in [
                ("issue_comment", "comments"),
                ("issue_event", "events"),
                ("issue_timeline", "timeline"),
                ("issue_reaction", "reactions"),
            ]:
                values = collection(kind, scope, f"/issues/{number}/{endpoint}", number)
                if kind == "issue_comment":
                    comments = values
            for comment in comments or []:
                collection(
                    "comment_reaction",
                    f"comment:{comment['id']}",
                    f"/issues/comments/{comment['id']}/reactions",
                    number,
                )
            if parent["kind"] == "pull":
                for kind, endpoint in [
                    ("review", "reviews"),
                    ("review_comment", "comments"),
                    ("pull_commit", "commits"),
                    ("pull_file", "files"),
                ]:
                    values = collection(
                        kind, f"pull:{number}", f"/pulls/{number}/{endpoint}", number
                    )
                    if kind == "review_comment":
                        for comment in values or []:
                            collection(
                                "review_comment_reaction",
                                f"review-comment:{comment['id']}",
                                f"/pulls/comments/{comment['id']}/reactions",
                                number,
                            )
        item["parents"] = len(parents)
        item["ok"] = all(x["ok"] for x in item["collections"])
        item["in_progress"] = False
        save()
        print(
            "AUDIT",
            item["name"],
            len(item["collections"]),
            "OK" if item["ok"] else "NEEDS_SYNC",
            flush=True,
        )
    report["finished"] = a.now()
    save()
    print(
        "COMPLETE",
        len(report["repositories"]),
        sum(r["ok"] for r in report["repositories"]),
        flush=True,
    )

    return 0 if all(x["ok"] for x in report["repositories"]) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditPaused as e:
        print("PAUSED", str(e), flush=True)
        raise SystemExit(2) from None

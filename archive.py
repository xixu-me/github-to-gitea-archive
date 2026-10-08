#!/usr/bin/env python3
"""Single-host GitHub archive. Python stdlib only; GitHub is authoritative."""

import argparse
import base64
import contextlib
import concurrent.futures
import datetime
import fcntl
import gzip
import hashlib
import hmac
import http.client
import html
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import shutil
import tempfile
import time
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser

from archive_config import load_config, read_env_file


def configure(config):
    global CONFIG, ROOT, OWNER, GITEA_OWNER, ADMIN_USER, GITEA, GITROOT, LOW, NORMAL, GITEA_DB
    CONFIG = config
    ROOT = config.root
    OWNER = config.owner
    GITEA_OWNER = config.gitea_owner
    ADMIN_USER = config.admin_user
    GITEA = config.gitea_url
    GITROOT = config.repo_root / config.gitea_owner.lower()
    GITEA_DB = config.gitea_db
    LOW = config.minimum_free
    NORMAL = config.normal_free


configure(load_config())
GITHUB_API = "https://api.github.com"


class RejectCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward bearer tokens or cookies to a redirect destination.
        return None


def http_open(request, timeout):
    return urllib.request.build_opener(RejectCredentialRedirect()).open(request, timeout=timeout)


def now():
    return int(time.time())


def free():
    st = os.statvfs(ROOT)
    return st.f_bavail * st.f_frsize


def db():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    ROOT.chmod(0o700)
    c = sqlite3.connect(ROOT / "state.db", timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript("""
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS repo(id INTEGER PRIMARY KEY,name TEXT,local_name TEXT,
        private INTEGER,source BLOB,seen INTEGER,code_ok INTEGER DEFAULT 0,
        metadata_ok INTEGER DEFAULT 0,error TEXT,available INTEGER DEFAULT 1);
      CREATE TABLE IF NOT EXISTS job(repo_id INTEGER PRIMARY KEY,kind TEXT,due INTEGER,
        attempts INTEGER DEFAULT 0,reason TEXT,claimed INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS resource(url TEXT PRIMARY KEY,etag TEXT,body BLOB,
        checked INTEGER,changed INTEGER);
      CREATE TABLE IF NOT EXISTS object(repo_id INTEGER,kind TEXT,id TEXT,body BLOB,
        updated INTEGER,PRIMARY KEY(repo_id,kind,id));
      CREATE TABLE IF NOT EXISTS mapping(repo_id INTEGER,kind TEXT,source_id TEXT,
        target_id TEXT,hash TEXT,PRIMARY KEY(repo_id,kind,source_id));
      CREATE TABLE IF NOT EXISTS presence(repo_id INTEGER,kind TEXT,id TEXT,scope TEXT,
        present INTEGER,checked INTEGER,PRIMARY KEY(repo_id,kind,id,scope));
      CREATE INDEX IF NOT EXISTS presence_scope ON presence(repo_id,kind,scope);
      CREATE TABLE IF NOT EXISTS event(delivery TEXT PRIMARY KEY,received INTEGER,event TEXT);
      CREATE TABLE IF NOT EXISTS setting(key TEXT PRIMARY KEY,value TEXT);
    """)
    if "priority" not in {x[1] for x in c.execute("PRAGMA table_info(job)")}:
        try:
            c.execute("ALTER TABLE job ADD COLUMN priority INTEGER DEFAULT 0")
            c.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise
    version = c.execute("SELECT value FROM setting WHERE key=?", ("data_version",)).fetchone()
    if not version or int(version[0]) < 3:
        c.execute("DELETE FROM setting WHERE key LIKE ?", ("deep:%",))
        c.execute("INSERT OR REPLACE INTO setting VALUES(?,?)", ("data_version", "3"))
        c.commit()
    namespace = json.dumps([OWNER.lower(), GITEA_OWNER.lower()])
    if not c.execute("SELECT 1 FROM setting WHERE key=?", ("archive_namespace",)).fetchone():
        for (source,) in c.execute("SELECT source FROM repo"):
            source_owner = (decode(source).get("owner") or {}).get("login", "")
            if source_owner.lower() != OWNER.lower():
                c.close()
                raise RuntimeError(
                    "legacy archive source ownership differs; use a fresh ARCHIVE_ROOT"
                )
    c.execute("INSERT OR IGNORE INTO setting VALUES(?,?)", ("archive_namespace", namespace))
    saved = c.execute("SELECT value FROM setting WHERE key=?", ("archive_namespace",)).fetchone()[0]
    c.commit()
    if saved != namespace:
        c.close()
        raise RuntimeError(
            "ARCHIVE_ROOT belongs to another GitHub/Gitea account; use a separate directory"
        )
    (ROOT / "state.db").chmod(0o600)
    return c


def encode(x):
    return zlib.compress(json.dumps(x, ensure_ascii=False).encode(), 6)


def decode(x):
    return json.loads(zlib.decompress(x))


def setting(c, key, value=None):
    if value is not None:
        c.execute("INSERT OR REPLACE INTO setting VALUES (?,?)", (key, str(value)))
        c.commit()
    row = c.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def enqueue(c, rid, kind="all", reason="reconcile"):
    priority = 10 if reason.startswith("webhook:") else 5 if reason == "discovery-change" else 0
    event = reason.split(":", 2)[1] if reason.count(":") >= 2 else None
    deep_events = {
        "issue_comment",
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_review_thread",
    }
    if reason.startswith("webhook:") and event in {"gollum", "repository"}:
        c.execute("DELETE FROM setting WHERE key=?", (f"wiki_checked:{rid}",))
    if reason.startswith("webhook:") and kind != "code" and (event is None or event in deep_events):
        # Review/comment edits need not advance the parent issue's updated_at.
        c.execute("DELETE FROM setting WHERE key=?", (f"deep:{rid}",))
    c.execute(
        """INSERT INTO job(repo_id,kind,due,reason,priority) VALUES(?,?,?,?,?)
      ON CONFLICT(repo_id) DO UPDATE SET kind=CASE WHEN job.kind=excluded.kind THEN job.kind
      ELSE 'all' END,due=min(job.due,excluded.due),reason=excluded.reason,
      priority=max(job.priority,excluded.priority)""",
        (rid, kind, now(), reason, priority),
    )
    c.commit()


def inbox_db(initialize=False):
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    ROOT.chmod(0o700)
    c = sqlite3.connect(ROOT / "inbox.db", timeout=2)
    c.row_factory = sqlite3.Row
    if initialize:
        c.executescript(
            "PRAGMA journal_mode=WAL;\nCREATE TABLE IF NOT EXISTS delivery(delivery TEXT PRIMARY KEY,received INTEGER,event TEXT,payload BLOB,\n done INTEGER DEFAULT 0,privacy_pending INTEGER DEFAULT 0);\nCREATE INDEX IF NOT EXISTS delivery_pending ON delivery(done,privacy_pending,received);"
        )
    (ROOT / "inbox.db").chmod(0o600)
    c.execute("PRAGMA synchronous=FULL")
    return c


def privacy_pending(payload):
    r = payload.get("repository")
    if (
        not r
        or r.get("private") is not True
        or r.get("owner", {}).get("login", "").lower() != OWNER.lower()
    ):
        return False
    # A read-only WAL lookup never queues behind the worker's write transaction.
    try:
        with contextlib.closing(
            sqlite3.connect("file:" + str(ROOT / "state.db") + "?mode=ro", uri=True, timeout=1)
        ) as c:
            row = c.execute("SELECT private FROM repo WHERE id=?", (r["id"],)).fetchone()
        return not row or not row[0]
    except (sqlite3.Error, KeyError):
        return True  # Fail closed if we cannot establish previously protected privacy.


def receive_delivery(payload, raw, delivery, event):
    with contextlib.closing(inbox_db()) as c:
        if c.execute("SELECT 1 FROM delivery WHERE delivery=?", (delivery,)).fetchone():
            return False
        pending = c.execute(
            "SELECT count(*),coalesce(sum(length(payload)),0) FROM delivery WHERE done=0"
        ).fetchone()
        if free() < LOW or pending[0] >= 10000 or pending[1] >= 64 * 1024**2:
            raise RuntimeError("inbox capacity exceeded")
        protected = privacy_pending(payload)
        stored = zlib.compress(raw, 3)
        c.execute(
            "INSERT OR IGNORE INTO delivery(delivery,received,event,payload,privacy_pending) VALUES(?,?,?,?,?)",
            (delivery, now(), event, stored, int(protected)),
        )
        inserted = c.execute("SELECT changes()").fetchone()[0]
        c.commit()  # The only durable write on the response path; independent of state.db.
        return bool(inserted)


def apply_delivery(c, payload, delivery, event):
    if c.execute("SELECT 1 FROM event WHERE delivery=?", (delivery,)).fetchone():
        return
    r = payload.get("repository")
    if event in ("repository", "gollum"):
        setting(c, "discovery_ok", "0")
    if r and r.get("owner", {}).get("login", "").lower() == OWNER.lower():
        rid = r["id"]
        known = c.execute("SELECT * FROM repo WHERE id=?", (rid,)).fetchone()
        if not known:
            c.execute(
                "INSERT INTO repo(id,name,local_name,private,source,seen) VALUES(?,?,?,?,?,?)",
                (rid, r["name"], r["name"], r.get("private", True), encode(r), now()),
            )
            c.commit()
            setting(c, "discovery_ok", "0")
        elif r.get("private") is True:
            # Signed payloads can announce privacy before the next
            # inventory poll. Never fetch/project new private content
            # using stale public source state.
            with visibility_lock(rid):
                current = c.execute("SELECT * FROM repo WHERE id=?", (rid,)).fetchone()
                source = decode(current["source"])
                source["private"] = True
                c.execute("UPDATE repo SET private=1,source=? WHERE id=?", (encode(source), rid))
                c.execute(
                    "INSERT OR REPLACE INTO setting VALUES(?,?)",
                    (f"private_event:{rid}", str(time.time_ns())),
                )
                c.commit()
                guarded = dict(current)
                guarded["private"] = 1
                try:
                    protect_private_repo(API(c), guarded)
                except Exception:
                    # Keep the delivery queued; the worker privacy guard
                    # prevents native writes until protection succeeds.
                    c.execute(
                        "UPDATE repo SET error=? WHERE id=?",
                        ("private visibility update pending", rid),
                    )
                    c.commit()
                    raise
            if not known["private"]:
                setting(c, "discovery_ok", "0")
        enqueue(c, rid, "code" if event == "push" else "all", f"webhook:{event}:{delivery}")
    elif event in ("installation", "installation_repositories"):
        setting(c, "discovery_ok", "0")
    c.execute("INSERT INTO event VALUES(?,?,?)", (delivery, now(), event))
    c.commit()


def ingest(c, limit=100):
    with open(ROOT / "ingest.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        with contextlib.closing(inbox_db(initialize=True)) as queue:
            completed = 0
            for _ in range(limit):
                item = queue.execute(
                    "SELECT * FROM delivery WHERE done=0 ORDER BY privacy_pending DESC,received,rowid LIMIT 1"
                ).fetchone()
                if not item:
                    break
                try:
                    payload = decode(item["payload"])
                    if item["privacy_pending"]:
                        r = payload["repository"]
                        known = c.execute("SELECT * FROM repo WHERE id=?", (r["id"],)).fetchone()
                        guarded = (
                            dict(known)
                            if known
                            else dict(id=r["id"], name=r["name"], local_name=r["name"])
                        )
                        guarded["private"] = 1
                        # HTTP privacy protection precedes all main database writes.
                        protect_private_repo(API(c), guarded)
                    apply_delivery(c, payload, item["delivery"], item["event"])
                    queue.execute(
                        "UPDATE delivery SET done=1,payload=NULL,privacy_pending=0 WHERE delivery=?",
                        (item["delivery"],),
                    )
                    queue.commit()
                    completed += 1
                except Exception:
                    c.rollback()
                    queue.rollback()
                    # Keep the payload and any privacy gate until a successful retry.
                    print("INBOX retry pending", flush=True)
                    break
            queue.execute("DELETE FROM delivery WHERE done=1 AND received<?", (now() - 7 * 86400,))
            queue.commit()
            return completed


class API:
    def __init__(self, c):
        self.c = c
        self.app_token = None
        self.app_expiry = 0

    def github_token(self):
        appfile = ROOT / "app.json"
        if not appfile.exists():
            return os.environ.get("GITHUB_TOKEN", "")
        if self.app_token and self.app_expiry > now() + 120:
            return self.app_token
        app = json.loads(appfile.read_text())

        def b64(v):
            return base64.urlsafe_b64encode(v).rstrip(b"=").decode()

        header = b64(b'{"alg":"RS256","typ":"JWT"}')
        payload = b64(
            json.dumps({"iat": now() - 60, "exp": now() + 540, "iss": app["id"]}).encode()
        )
        message = (header + "." + payload).encode()
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(ROOT / "app-key.pem")],
            input=message,
            capture_output=True,
            check=True,
        ).stdout
        jwt = message.decode() + "." + b64(signed)

        def app_request(path, data=None):
            headers = {
                "Authorization": "Bearer " + jwt,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "github-account-archive/0.1",
            }
            req = urllib.request.Request(
                GITHUB_API + path, json.dumps(data).encode() if data is not None else None, headers
            )
            with http_open(req, timeout=30) as r:
                return json.load(r)

        installation = None
        for page in range(1, 10001):
            installations = app_request(f"/app/installations?per_page=100&page={page}")
            installation = next(
                (x for x in installations if x["account"]["login"].lower() == OWNER.lower()), None
            )
            if installation or len(installations) < 100:
                break
        if not installation or installation["repository_selection"] != "all":
            raise RuntimeError("GitHub App must be installed for all owner repositories")
        result = app_request(
            f"/app/installations/{installation['id']}/access_tokens",
            {
                "permissions": {
                    "contents": "read",
                    "issues": "read",
                    "pull_requests": "read",
                    "metadata": "read",
                }
            },
        )
        self.app_token = result["token"]
        self.app_expiry = now() + 3300
        return self.app_token

    def repositories(self):
        """Enumerate authorized owned repositories, including private ones when available."""
        if (ROOT / "app.json").exists():
            return self.pages("/installation/repositories", cached=False)
        account_type = CONFIG.account_type
        if account_type == "auto":
            account = self.request(GITHUB_API + "/users/" + OWNER)
            account_type = "organization" if account.get("type") == "Organization" else "user"
        if account_type == "organization":
            return self.pages("/orgs/" + OWNER + "/repos?type=all", cached=False)
        if self.github_token():
            user = self.request(GITHUB_API + "/user")
            if user.get("login", "").lower() == OWNER.lower():
                return self.pages(
                    "/user/repos?affiliation=owner&visibility=all&sort=updated", cached=False
                )
        return self.pages("/users/" + OWNER + "/repos?type=owner&sort=updated", cached=False)

    def request(self, url, method="GET", data=None, gitea=False, cached=False, accept=None):
        target = urllib.parse.urlsplit(url)
        allowed = urllib.parse.urlsplit(GITEA if gitea else GITHUB_API)
        if (target.scheme, target.netloc) != (allowed.scheme, allowed.netloc):
            raise RuntimeError("credentialed requests must stay on the configured API origin")
        if not gitea and method != "GET":
            raise RuntimeError("GitHub source writes are forbidden")
        if free() < LOW:
            raise RuntimeError("capacity: free space below configured minimum")
        if not gitea and int(setting(self.c, "github_backoff_until") or 0) > now():
            raise RuntimeError("GitHub rate-limit backoff active; queued for retry")
        token = os.environ.get("GITEA_TOKEN", "") if gitea else self.github_token()
        headers = {
            "Accept": accept or "application/vnd.github+json",
            "User-Agent": "github-account-archive/0.1",
        }
        if token:
            headers["Authorization"] = ("token " if gitea else "Bearer ") + token
        if not gitea:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        old = (
            self.c.execute("SELECT * FROM resource WHERE url=?", (url,)).fetchone()
            if cached
            else None
        )
        if old and old["etag"]:
            headers["If-None-Match"] = old["etag"]
        payload = json.dumps(data).encode() if data is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, payload, headers, method=method)
                with http_open(req, timeout=120) as r:
                    raw = r.read(16 * 1024**2 + 1)
                    if len(raw) > 16 * 1024**2:
                        raise RuntimeError("response exceeds bounded 16 MiB limit")
                    result = json.loads(raw) if raw else None
                    if cached:
                        body = encode(result)
                        changed = now() if not old or old["body"] != body else old["changed"]
                        self.c.execute(
                            "INSERT OR REPLACE INTO resource VALUES(?,?,?,?,?)",
                            (url, r.headers.get("ETag"), body, now(), changed),
                        )
                        self.c.commit()
                    return result
            except urllib.error.HTTPError as e:
                if e.code == 304 and old:
                    self.c.execute("UPDATE resource SET checked=? WHERE url=?", (now(), url))
                    self.c.commit()
                    return decode(old["body"])
                if e.code in (403, 429):
                    delay = max(
                        int(e.headers.get("Retry-After", "60")),
                        int(e.headers.get("X-RateLimit-Reset", "0")) - now(),
                    )
                    if (
                        e.code == 403
                        and e.headers.get("X-RateLimit-Remaining") != "0"
                        and not e.headers.get("Retry-After")
                    ):
                        raise RuntimeError("API 403: permissions or secondary rate limit") from None
                    setting(self.c, "github_backoff_until", now() + min(max(delay, 60), 3600))
                    raise RuntimeError("API rate limit; retry scheduled") from None
                if e.code >= 500 and attempt < 3 and method != "POST":
                    time.sleep(2**attempt)
                    continue
                raise RuntimeError(
                    f"API {method} {urllib.parse.urlparse(url).path}: HTTP {e.code}"
                ) from None
            except (TimeoutError, urllib.error.URLError, http.client.RemoteDisconnected):
                if method == "POST":
                    raise RuntimeError(
                        "POST outcome uncertain; reconcile source-ID markers before retry"
                    ) from None
                if attempt == 3:
                    raise RuntimeError("API transport error; retry scheduled") from None
                time.sleep(2**attempt)

    def pages(self, path, cached=True):
        base = GITHUB_API + path
        sep = "&" if "?" in base else "?"
        output = []
        for page in range(1, 10001):
            rows = self.request(f"{base}{sep}per_page=100&page={page}", cached=cached)
            if isinstance(rows, dict) and "repositories" in rows:
                rows = rows["repositories"]
            if not isinstance(rows, list):
                raise RuntimeError("expected paginated array")
            output.extend(rows)
            if len(rows) < 100:
                return output
        raise RuntimeError("pagination safety limit reached")

    def gt(self, path, method="GET", data=None):
        return self.request(GITEA + "/api/v1" + path, method, data, gitea=True)

    def parallel_pages(self, paths):
        # Only lightweight HTTP reads run concurrently. Each thread has its own
        # SQLite connection; the parent shares one refreshed installation token.
        token = self.github_token()

        def fetch(item):
            key, path = item
            with contextlib.closing(db()) as conn:
                child = API(conn)
                child.app_token = token
                child.app_expiry = getattr(self, "app_expiry", 0)
                return key, child.pages(path)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            return dict(executor.map(fetch, paths.items()))

    def gt_pages(self, path):
        out = []
        sep = "&" if "?" in path else "?"
        for page in range(1, 10001):
            batch = self.gt(f"{path}{sep}limit=50&page={page}")
            out.extend(batch)
            if len(batch) < 50:
                return out
        raise RuntimeError("Gitea pagination safety limit reached")


def discover(c, api):
    started_ns = time.time_ns()
    rows = api.repositories()
    ids = set()
    for r in rows:
        if r["owner"]["login"].lower() != OWNER.lower():
            continue
        ids.add(r["id"])
        with visibility_lock(r["id"]):
            old = c.execute("SELECT * FROM repo WHERE id=?", (r["id"],)).fetchone()
            if int(setting(c, f"private_event:{r['id']}") or 0) > started_ns:
                # Do not let an inventory fetched before a signed privatization
                # announcement replace that newer signal with stale public state.
                r["private"] = True
            c.execute(
                """INSERT INTO repo(id,name,local_name,private,source,seen) VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name,private=excluded.private,
                source=excluded.source,seen=excluded.seen,available=1""",
                (r["id"], r["name"], r["name"], r["private"], encode(r), now()),
            )
            c.commit()
        if not old or old["name"] != r["name"] or old["source"] != encode(r):
            enqueue(c, r["id"], "all", "discovery-change")
        elif (
            now() - old["metadata_ok"] >= 3600
            or now() - old["code_ok"] >= 3600
            or not setting(c, f"projection_ok:{r['id']}")
        ):
            enqueue(c, r["id"], "all", "hourly-reconcile")
    for row in c.execute("SELECT id FROM repo").fetchall():
        if row["id"] not in ids:
            c.execute(
                "UPDATE repo SET available=0,error=? WHERE id=?",
                ("not visible on GitHub; local copy retained", row["id"]),
            )
    setting(c, "discovery_ok", now())
    setting(c, "discovery_error", "")
    c.commit()


def git(args, token=None, timeout=1800, absent_ok=False, input_data=None, raw_output=False):
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        env.update(
            GIT_CONFIG_COUNT="4",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraHeader",
            GIT_CONFIG_VALUE_0="Authorization: Basic "
            + base64.b64encode(("x-access-token:" + token).encode()).decode(),
            GIT_CONFIG_KEY_1="gc.auto",
            GIT_CONFIG_VALUE_1="0",
            GIT_CONFIG_KEY_2="maintenance.auto",
            GIT_CONFIG_VALUE_2="false",
            GIT_CONFIG_KEY_3="http.followRedirects",
            GIT_CONFIG_VALUE_3="false",
        )
    p = subprocess.Popen(
        ["git"] + args,
        env=env,
        stdin=subprocess.PIPE if input_data is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stdout, stderr = (
                    p.communicate(input=input_data, timeout=1)
                    if input_data is not None
                    else p.communicate(timeout=1)
                )
                break
            except subprocess.TimeoutExpired:
                input_data = None
                if free() < LOW or time.monotonic() > deadline:
                    raise RuntimeError("git stopped: capacity protection or operation timeout")
        if p.returncode:
            if absent_ok and (
                b"Repository not found" in stderr or b"repository not found" in stderr
            ):
                return None
            raise RuntimeError(
                f"git operation failed ({p.returncode}); credentials and remote output withheld"
            )
        return stdout if raw_output else stdout.decode(errors="replace")
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.communicate()


@contextlib.contextmanager
def visibility_lock(rid):
    with open(ROOT / f"visibility-{rid}.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def protect_private_repo(api, row):
    """Confirm native privacy before copying any private source content."""
    if not row["private"]:
        return
    name = row["local_name"] or row["name"]
    if not (GITROOT / (name.lower() + ".git")).is_dir():
        return  # Initial migration creates the repository with source privacy.
    result = api.gt(f"/repos/{GITEA_OWNER}/{urllib.parse.quote(name)}", "PATCH", {"private": True})
    if not result or result.get("private") is not True:
        raise RuntimeError("native private repository protection not confirmed")


def branch_hook_updates(current, recorded):
    """Notify Gitea of real branch changes without creating any commits."""
    updates = []
    for name in sorted(set(current) | set(recorded)):
        target = current.get(name)
        previous, deleted = recorded.get(name, (None, False))
        if target and (target != previous or deleted):
            zero = "0" * len(target)
            updates.append(
                f"{previous if previous and not deleted else zero} {target} refs/heads/{name}"
            )
        elif not target and previous and not deleted:
            updates.append(f"{previous} {'0' * len(previous)} refs/heads/{name}")
    return updates


def pending_private_signal(rid):
    if not (ROOT / "inbox.db").exists():
        return False
    with contextlib.closing(inbox_db()) as queue:
        for row in queue.execute("SELECT payload FROM delivery WHERE done=0 AND privacy_pending=1"):
            if (decode(row[0]).get("repository") or {}).get("id") == rid:
                return True
    return False


def refresh_gitea_branches(bare):
    # fetch does not execute receive hooks. Read the native ledger, then let
    # Gitea's own hook update its database and invalidate caches.
    lines = git(
        ["--git-dir", str(bare), "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads"]
    )
    current = {
        ref.removeprefix("refs/heads/"): sha
        for sha, ref in (line.split(" ", 1) for line in lines.splitlines())
    }
    database = GITEA_DB
    with contextlib.closing(
        sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True, timeout=30)
    ) as native:
        row = native.execute(
            "SELECT r.id,r.name,u.id,u.name FROM repository r JOIN user u ON u.id=r.owner_id "
            "WHERE u.lower_name=? AND r.lower_name=?",
            (GITEA_OWNER.lower(), bare.name.removesuffix(".git").lower()),
        ).fetchone()
        if not row:
            raise RuntimeError("native repository not found for branch refresh")
        recorded = {
            name: (sha, bool(deleted))
            for name, sha, deleted in native.execute(
                "SELECT name,commit_id,is_deleted FROM branch WHERE repo_id=?", (row[0],)
            )
        }
        pusher = native.execute(
            "SELECT id,name FROM user WHERE lower_name=?", (ADMIN_USER.lower(),)
        ).fetchone()
        if not pusher:
            raise RuntimeError("ARCHIVE_ADMIN_USER not found in native Gitea database")
    changes = branch_hook_updates(current, recorded)
    if not changes:
        return 0
    hook = bare / "hooks/post-receive.d/gitea"
    if not hook.is_file() or not os.access(hook, os.X_OK):
        raise RuntimeError("Gitea post-receive hook missing or not executable")
    env = os.environ.copy()
    env.update(
        GIT_DIR=str(bare),
        SSH_ORIGINAL_COMMAND="gitea-internal",
        GITEA_REPO_ID=str(row[0]),
        GITEA_REPO_NAME=row[1],
        GITEA_REPO_USER_NAME=row[3],
        GITEA_REPO_IS_WIKI="false",
        GITEA_PUSHER_ID=str(pusher[0]),
        GITEA_PUSHER_NAME=pusher[1],
        GITEA_INTERNAL_PUSH="false",
    )
    result = subprocess.run(
        [str(hook)],
        input=("\n".join(changes) + "\n").encode(),
        cwd=bare,
        env=env,
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError("Gitea branch refresh hook failed; output withheld")
    # Gitea can log a DB failure and return success; verify the actual ledger.
    with contextlib.closing(
        sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True, timeout=30)
    ) as native:
        refreshed = {
            name: sha
            for name, sha in native.execute(
                "SELECT name,commit_id FROM branch WHERE repo_id=? AND is_deleted=0", (row[0],)
            )
        }
    if refreshed != current:
        raise RuntimeError("native branch records differ after hook; queued for retry")
    return len(changes)


def sync_code(c, api, row):
    r = decode(row["source"])
    name = row["name"]
    local = row["local_name"]
    bare = GITROOT / (local.lower() + ".git")
    if not bare.is_dir():
        estimated = r.get("size", 0) * 1024 * 3 + 100 * 1024**2
        if free() - estimated < LOW:
            raise RuntimeError("capacity: new repository requires more working space")
        api.gt(
            "/repos/migrate",
            "POST",
            {
                "clone_addr": r["clone_url"],
                "auth_token": api.github_token(),
                "repo_name": name,
                "repo_owner": GITEA_OWNER,
                "service": "github",
                "private": r["private"],
                "mirror": False,
                "issues": False,
                "pull_requests": False,
                "releases": False,
                "labels": False,
                "milestones": False,
                "wiki": False,
                "lfs": False,
            },
        )
        bare = GITROOT / (name.lower() + ".git")
    if local != name:
        api.gt(f"/repos/{GITEA_OWNER}/{urllib.parse.quote(local)}", "PATCH", {"name": name})
        c.execute("UPDATE repo SET local_name=? WHERE id=?", (name, row["id"]))
        c.commit()
        bare = GITROOT / (name.lower() + ".git")
    # Keep prior tips reachable before force-pushes or upstream deletions replace them.
    prior = git(
        [
            "--git-dir",
            str(bare),
            "for-each-ref",
            "--format=%(objectname) %(refname)",
            "refs/heads",
            "refs/tags",
            "refs/archive/pull",
        ]
    )
    updates = []
    references = []
    existing_history = set(
        git(
            ["--git-dir", str(bare), "for-each-ref", "--format=%(refname)", "refs/archive/history"]
        ).splitlines()
    )
    for line in prior.splitlines():
        sha, ref = line.split(" ", 1)
        key = hashlib.sha256(ref.encode()).hexdigest()
        history_ref = f"refs/archive/history/{key}/{sha}"
        if history_ref not in existing_history:
            updates.append(f"update {history_ref} {sha}")
        references.append({"id": ref + ":" + sha, "ref": ref, "sha": sha})
    if updates:
        git(
            ["--git-dir", str(bare), "update-ref", "--stdin"],
            input_data=("\n".join(updates) + "\n").encode(),
        )
    # Reconstruct missing metadata after an interrupted Git-ref / SQLite handoff.
    # Existing history refs need no rewrite or thousands of transient lock files.
    for item in references:
        c.execute(
            "INSERT OR IGNORE INTO object VALUES(?,?,?,?,?)",
            (row["id"], "retained_ref", item["id"], encode(item), now()),
        )
    c.commit()
    git(
        [
            "--git-dir",
            str(bare),
            "fetch",
            "--atomic",
            "--prune",
            r["clone_url"],
            "+refs/heads/*:refs/heads/*",
            "+refs/tags/*:refs/tags/*",
            "+refs/pull/*/head:refs/archive/pull/*/head",
        ],
        api.github_token(),
    )
    branch = r.get("default_branch")
    branch_exists = (
        branch
        and git(
            ["--git-dir", str(bare), "for-each-ref", "--format=%(refname)", "refs/heads/" + branch]
        ).strip()
    )
    if branch_exists:
        git(["--git-dir", str(bare), "symbolic-ref", "HEAD", "refs/heads/" + branch])
    git(["--git-dir", str(bare), "update-server-info"])
    updates = {
        "description": r.get("description") or "",
        "website": r.get("homepage") or "",
        "private": r["private"],
        "has_actions": False,
    }
    if branch_exists:
        updates["default_branch"] = branch
    with visibility_lock(row["id"]):
        latest = c.execute("SELECT private FROM repo WHERE id=?", (row["id"],)).fetchone()
        updates["private"] = bool(
            updates["private"] or (latest and latest[0]) or pending_private_signal(row["id"])
        )
        guarded = dict(row)
        guarded["private"] = updates["private"]
        protect_private_repo(api, guarded)
        # Refresh before any further upstream round trip, and before PATCHing a
        # newly selected default branch that does not yet exist in Gitea's ledger.
        refresh_gitea_branches(bare)
        api.gt(f"/repos/{GITEA_OWNER}/{urllib.parse.quote(name)}", "PATCH", updates)
    upstream = git(
        ["ls-remote", "--heads", "--tags", r["clone_url"]], api.github_token(), timeout=120
    )
    remote_refs = {
        line.split()[1]: line.split()[0]
        for line in upstream.splitlines()
        if len(line.split()) == 2 and not line.split()[1].endswith("^{}")
    }
    local = git(
        [
            "--git-dir",
            str(bare),
            "for-each-ref",
            "--format=%(objectname) %(refname)",
            "refs/heads",
            "refs/tags",
        ]
    )
    local_refs = {
        line.split()[1]: line.split()[0] for line in local.splitlines() if len(line.split()) == 2
    }
    if remote_refs != local_refs:
        raise RuntimeError(
            "branch/tag verification differs; upstream may have changed during fetch"
        )
    setting(c, f"git_storage:{row['id']}", git(["--git-dir", str(bare), "count-objects", "-v"]))
    setting(c, f"code_verified:{row['id']}", now())
    head_sha = local_refs.get("refs/heads/" + branch) if branch_exists else None
    old_lfs = c.execute(
        "SELECT body FROM object WHERE repo_id=? AND kind='lfs_manifest' AND id='default-branch'",
        (row["id"],),
    ).fetchone()
    old_lfs = decode(old_lfs[0]) if old_lfs else {}
    if (
        "head_sha" not in old_lfs
        or old_lfs["head_sha"] != head_sha
        or now() - int(setting(c, f"lfs_checked:{row['id']}") or 0) >= 86400
    ):
        inventory_lfs(c, row, bare, bool(branch_exists), head_sha)
    c.execute("UPDATE repo SET code_ok=? WHERE id=?", (now(), row["id"]))
    c.commit()


def parse_lfs_pointer(body):
    try:
        lines = body.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    fields = {}
    for line in lines:
        key, separator, value = line.partition(" ")
        if not separator or not re.fullmatch("[a-z0-9.-]+", key) or key in fields:
            return None
        fields[key] = value
    if (
        not fields
        or next(iter(fields)) != "version"
        or fields["version"]
        not in ("https://git-lfs.github.com/spec/v1", "https://hawser.github.com/spec/v1")
        or list(fields)[1:] != sorted(list(fields)[1:])
        or not re.fullmatch("sha256:[0-9a-f]{64}", fields.get("oid", ""))
        or not re.fullmatch("[0-9]+", fields.get("size", ""))
    ):
        return None
    return fields


def inventory_lfs(c, row, bare, has_head, head_sha=None):
    pointers = []
    candidates = {}
    if has_head:
        tree = git(["--git-dir", str(bare), "ls-tree", "-r", "-l", "-z", "HEAD"])
        for entry in tree.split("\0"):
            if not entry or "\t" not in entry:
                continue
            info, path = entry.split("\t", 1)
            fields = info.split()
            if len(fields) == 4 and fields[1] == "blob" and 100 <= int(fields[3]) <= 1024:
                candidates.setdefault(fields[2], []).append(path)
        all_candidates = list(candidates)
        for start in range(0, len(all_candidates), 1000):
            selected = all_candidates[start : start + 1000]
            raw = git(
                ["--git-dir", str(bare), "cat-file", "--batch"],
                input_data=("\n".join(selected) + "\n").encode(),
                raw_output=True,
            )
            # Decode by bytes, because a small ordinary blob can contain multibyte text.
            # Batch parser only uses LF framing for headers; body boundaries are byte counts.
            offset = 0
            for sha in selected:
                end = raw.index(b"\n", offset)
                header = raw[offset:end].split()
                length = int(header[2])
                body = raw[end + 1 : end + 1 + length]
                offset = end + length + 2
                fields = parse_lfs_pointer(body)
                if fields:
                    pointers.append(
                        {
                            "oid": fields["oid"].removeprefix("sha256:"),
                            "size": int(fields["size"]),
                            "paths": candidates[sha],
                            "pointer_blob": sha,
                            "pointer_fields": fields,
                        }
                    )
    save_objects(
        c,
        row["id"],
        "lfs_manifest",
        [
            {
                "id": "default-branch",
                "scope": "default branch HEAD",
                "policy": "pointer-only; no LFS object downloads",
                "inventory_complete": True,
                "candidate_blobs": len(candidates),
                "head_sha": head_sha,
                "pointers": pointers,
                "lfs_endpoint": f"https://github.com/{OWNER}/{row['name']}.git/info/lfs",
                "historical_inventory": "historical pointers retained in Git history; not enumerated by this bounded inventory",
            }
        ],
    )
    setting(c, f"lfs_checked:{row['id']}", now())


def sync_wiki(c, api, row):
    key = f"wiki_checked:{row['id']}"
    if now() - int(setting(c, key) or 0) < 86400:
        return
    source = decode(row["source"])
    if not source.get("has_wiki"):
        setting(c, f"wiki_status:{row['id']}", "disabled upstream")
        setting(c, key, now())
        return
    url = f"https://github.com/{OWNER}/{row['name']}.wiki.git"
    refs = git(["ls-remote", url], api.github_token(), timeout=120, absent_ok=True)
    if refs is None or not refs.strip():
        setting(c, f"wiki_status:{row['id']}", "no accessible wiki repository")
        setting(c, key, now())
        return
    target = GITROOT / (row["name"].lower() + ".wiki.git")
    if not target.exists():
        stage = ROOT / ("wiki-stage-" + str(row["id"]) + ".git")
        if stage.exists():
            # Resume a failed clone only after validating that it is a bare Git repository.
            git(["--git-dir", str(stage), "rev-parse", "--is-bare-repository"])
            git(
                ["--git-dir", str(stage), "fetch", "--prune", url, "+refs/heads/*:refs/heads/*"],
                api.github_token(),
            )
        else:
            git(["clone", "--bare", url, str(stage)], api.github_token())
        stage.rename(target)
    else:
        git(
            ["--git-dir", str(target), "fetch", "--prune", url, "+refs/heads/*:refs/heads/*"],
            api.github_token(),
        )
    setting(c, f"wiki_status:{row['id']}", "synchronized")
    setting(c, key, now())


def save_objects(c, rid, kind, objects, scope=None):
    scope = scope or kind
    c.execute(
        "UPDATE presence SET present=0,checked=? WHERE repo_id=? AND kind=? AND scope=?",
        (now(), rid, kind, scope),
    )
    for x in objects:
        oid = str(
            x.get("id")
            or x.get("sha")
            or x.get("number")
            or hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()
        )
        c.execute(
            """INSERT INTO object VALUES(?,?,?,?,?)
                  ON CONFLICT(repo_id,kind,id) DO UPDATE SET body=excluded.body,updated=excluded.updated
                  WHERE object.body!=excluded.body""",
            (rid, kind, oid, encode(x), now()),
        )
        c.execute(
            "INSERT OR REPLACE INTO presence VALUES(?,?,?,?,1,?)", (rid, kind, oid, scope, now())
        )
    c.commit()


def sync_metadata(c, api, row):
    rid = row["id"]
    base = "/repos/" + OWNER + "/" + urllib.parse.quote(row["name"])
    issues = api.pages(base + "/issues?state=all&sort=updated&direction=desc")
    save_objects(c, rid, "issue", [x for x in issues if "pull_request" not in x])
    feature_gaps = []
    pulls_accessible = True
    pulls = []
    try:
        pulls = api.pages(base + "/pulls?state=all&sort=updated&direction=desc")
        save_objects(c, rid, "pull", pulls)
        c.execute(
            """UPDATE presence SET present=0,checked=? WHERE repo_id=? AND kind='pull_detail'
            AND id IN (SELECT id FROM presence WHERE repo_id=? AND kind='pull' AND scope='pull' AND present=0)""",
            (now(), rid, rid),
        )
        c.commit()
    except RuntimeError as e:
        if not ("HTTP 404" in str(e) and decode(row["source"]).get("has_pull_requests") is False):
            raise
        feature_gaps.append(
            "GitHub pull requests disabled; endpoint returns 404; previously archived records retained"
        )
        pulls_accessible = False
    setting(c, f"feature_gaps:{rid}", json.dumps(feature_gaps))
    daily = now() - int(setting(c, f"deep:{rid}") or 0) > 86400
    for kind, endpoint in [
        ("release", "/releases"),
        ("label", "/labels"),
        ("milestone", "/milestones?state=all"),
    ]:
        values = api.pages(base + endpoint)
        if kind == "release":
            for value in values:
                previous = c.execute(
                    "SELECT body FROM object WHERE repo_id=? AND kind=? AND id=?",
                    (rid, "release", str(value["id"])),
                ).fetchone()
                previous_assets = decode(previous["body"]).get("assets") if previous else None
                inline_assets = value.get("assets")
                # Reuse a previously enumerated complete inventory only when the embedded
                # collection matches it exactly. New/changed collections and daily deep
                # scans still enumerate the dedicated asset endpoint, including all pages.
                if daily or not isinstance(inline_assets, list) or previous_assets != inline_assets:
                    value["assets"] = api.pages(base + f"/releases/{value['id']}/assets")
                save_objects(c, rid, "release_asset", value["assets"], f"release:{value['id']}")
        save_objects(c, rid, kind, values)
    # Conditional requests avoid re-downloading, but daily deep scans catch edits to reviews.
    pull_ids = {x["number"]: str(x["id"]) for x in pulls}
    listed_prs = {x["number"] for x in issues if "pull_request" in x}
    # An issue-list / PR-list race must not leave a newly observed PR without details.
    issues.extend(dict(x, pull_request={}) for x in pulls if x["number"] not in listed_prs)
    for item in issues:
        key = f"issue:{rid}:{item['id']}"
        number = item["number"]
        scope = f"issue:{number}"
        detail = None
        if "pull_request" in item and pulls_accessible:
            known = c.execute(
                "SELECT 1 FROM object WHERE repo_id=? AND kind=? AND id=?",
                (rid, "pull_detail", pull_ids.get(number, "")),
            ).fetchone()
            if not known:
                detail = api.request(GITHUB_API + base + f"/pulls/{number}", cached=True)
                save_objects(c, rid, "pull_detail", [detail], f"pull-detail:{number}")
                save_objects(c, rid, "pull", [detail], f"pull-detail:{number}")
        content_known = (
            "pull_request" not in item
            or not pulls_accessible
            or c.execute(
                "SELECT 1 FROM object WHERE repo_id=? AND kind='pull_content_manifest' AND id=?",
                (rid, str(number)),
            ).fetchone()
            is not None
        )
        if not daily and content_known and setting(c, key) == item["updated_at"]:
            continue
        issue_parts = api.parallel_pages(
            {
                "comments": base + f"/issues/{number}/comments",
                "events": base + f"/issues/{number}/events",
                "timeline": base + f"/issues/{number}/timeline",
                "reactions": base + f"/issues/{number}/reactions",
            }
        )
        comments = issue_parts["comments"]
        save_objects(c, rid, "issue_comment", comments, scope)
        save_objects(c, rid, "issue_event", issue_parts["events"], scope)
        save_objects(c, rid, "issue_timeline", issue_parts["timeline"], scope)
        save_objects(c, rid, "issue_reaction", issue_parts["reactions"], scope)
        for comment in comments:
            save_objects(
                c,
                rid,
                "comment_reaction",
                api.pages(base + f"/issues/comments/{comment['id']}/reactions"),
                f"comment:{comment['id']}",
            )
        if "pull_request" in item and pulls_accessible:
            detail = detail or api.request(GITHUB_API + base + f"/pulls/{number}", cached=True)
            save_objects(c, rid, "pull_detail", [detail], f"pull-detail:{number}")
            save_objects(c, rid, "pull", [detail], f"pull-detail:{number}")
            pull_parts = api.parallel_pages(
                {
                    kind: base + f"/pulls/{number}/{endpoint}"
                    for kind, endpoint in [
                        ("review", "reviews"),
                        ("review_comment", "comments"),
                        ("pull_commit", "commits"),
                        ("pull_file", "files"),
                    ]
                }
            )
            for kind, values in pull_parts.items():
                # Commit SHA/file name need composite identity across PRs.
                for v in values:
                    v["_archive_pull_number"] = number
                    if kind in ("pull_commit", "pull_file"):
                        v["id"] = f"{number}:" + (
                            v.get("filename", "") if kind == "pull_file" else v.get("sha", "")
                        )
                save_objects(c, rid, kind, values, f"pull:{number}")
            for comment in pull_parts["review_comment"]:
                save_objects(
                    c,
                    rid,
                    "review_comment_reaction",
                    api.pages(base + f"/pulls/comments/{comment['id']}/reactions"),
                    f"review-comment:{comment['id']}",
                )
            files = pull_parts["pull_file"]
            save_objects(
                c,
                rid,
                "pull_content_manifest",
                [
                    {
                        "id": number,
                        "number": number,
                        "source": detail.get("html_url"),
                        "base_sha": (detail.get("base") or {}).get("sha"),
                        "head_sha": (detail.get("head") or {}).get("sha"),
                        "expected_files": detail.get("changed_files"),
                        "archived_files": len(files),
                        "files_without_patch": [v["filename"] for v in files if not v.get("patch")],
                        "file_list_complete": detail.get("changed_files") == len(files)
                        if "changed_files" in detail
                        else None,
                        "policy": "Git PR head refs and API file patches retained; binary contents use Git objects; API patches may be truncated",
                    }
                ],
                f"pull:{number}",
            )
        setting(c, key, item["updated_at"])
    if daily:
        setting(c, f"deep:{rid}", now())
    inventory_links(c, rid)
    housekeeping(c)
    c.execute("UPDATE repo SET metadata_ok=? WHERE id=?", (now(), rid))
    c.commit()


def inventory_links(c, rid):
    links = {}
    for row in c.execute(
        "SELECT kind,id,body FROM object WHERE repo_id=? AND kind!=?", (rid, "attachment_manifest")
    ):
        item = decode(row["body"])
        urls = re.findall(r'https://[^\s<>"\)]+', item.get("body") or "")
        if row["kind"] == "release_asset" and item.get("browser_download_url"):
            urls.append(item["browser_download_url"])
        for url in urls:
            if row["kind"] == "release_asset" or any(
                s in url
                for s in (
                    "github.com/user-attachments/",
                    "user-images.githubusercontent.com/",
                    "github.com/attachments/",
                    "github.com/files/",
                )
            ):
                entry = links.setdefault(
                    url,
                    {
                        "id": hashlib.sha256(url.encode()).hexdigest(),
                        "url": url,
                        "policy": "link-only",
                        "sources": [],
                    },
                )
                entry["sources"].append({"kind": row["kind"], "id": row["id"]})
                if row["kind"] == "release_asset":
                    entry.update(
                        size=item.get("size"), digest=item.get("digest"), name=item.get("name")
                    )
    save_objects(c, rid, "attachment_manifest", list(links.values()))
    setting(c, f"attachment_inventory:{rid}", now())


def housekeeping(c):
    # Cached responses are expendable; authoritative object records are retained.
    c.execute("DELETE FROM resource WHERE checked<?", (now() - 30 * 86400,))
    budget = 64 * 1024**2
    size = c.execute("SELECT coalesce(sum(length(body)),0) FROM resource").fetchone()[0]
    for row in c.execute(
        "SELECT url,length(body) AS size FROM resource ORDER BY checked"
    ).fetchall():
        if size <= budget:
            break
        c.execute("DELETE FROM resource WHERE url=?", (row["url"],))
        size -= row["size"]
    c.execute("DELETE FROM event WHERE received<?", (now() - 7 * 86400,))
    c.commit()
    c.execute("PRAGMA wal_checkpoint(PASSIVE)")


def project_gitea(c, api, row):
    """Project representable data through supported APIs, retaining raw author/time records.

    Existing migrated objects match by original number/body. New objects have durable
    source-ID markers to recover after a crash between a remote write and local commit.
    PRs that cannot be represented remain PR archives; never silently become issues.
    """
    rid = row["id"]
    base = f"/repos/{GITEA_OWNER}/{urllib.parse.quote(row['name'])}"

    def objects(kind):
        values = []
        for row in c.execute("SELECT id,body FROM object WHERE repo_id=? AND kind=?", (rid, kind)):
            value = decode(row["body"])
            if kind == "pull":
                detail = c.execute(
                    "SELECT body FROM object WHERE repo_id=? AND kind=? AND id=?",
                    (rid, "pull_detail", row["id"]),
                ).fetchone()
                if detail:
                    value = dict(decode(detail["body"]), **value)
                if value.get("merged_at"):
                    value["merged"] = True
            presence = c.execute(
                "SELECT present FROM presence WHERE repo_id=? AND kind=? AND id=? ORDER BY (scope=?) DESC,checked DESC LIMIT 1",
                (rid, kind, row["id"], kind),
            ).fetchone()
            if presence and not presence[0]:
                value["_archive_absent"] = True
            values.append(value)
        return values

    def mapped(kind, source_id):
        return c.execute(
            "SELECT * FROM mapping WHERE repo_id=? AND kind=? AND source_id=?",
            (rid, kind, str(source_id)),
        ).fetchone()

    def remember(kind, sid, tid, payload):
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        c.execute(
            "INSERT OR REPLACE INTO mapping VALUES(?,?,?,?,?)",
            (rid, kind, str(sid), str(tid), digest),
        )
        c.commit()

    def needs_update(m, payload):
        return (
            not m
            or m["hash"] != hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        )

    def source_body(x):
        author = (x.get("user") or x.get("author") or {}).get("login", "")
        state = (
            "\nGitHub PR status: merged, merge commit " + str(x.get("merge_commit_sha") or "")
            if x.get("merged")
            else ""
        )
        if x.get("_archive_absent"):
            state += "\nSource record absent at last check; local archive retained."
        return (
            (x.get("body") or "")
            + f"\n\n---\nGitHub source: {x['html_url']}\nAuthor: {author} · Created: {x.get('created_at', '')}{state}\n<!-- github-archive:{x['id']} -->"
        )

    label_ids = {}
    existing_labels = {x["name"]: x for x in api.gt_pages(base + "/labels")}
    labels_all = objects("label")
    active_label_names = {x["name"] for x in labels_all if not x.get("_archive_absent")}
    for x in labels_all:
        if x.get("_archive_absent") and x["name"] in active_label_names:
            continue
        payload = {
            "name": x["name"],
            "color": x["color"],
            "description": x.get("description") or "",
        }
        if x.get("_archive_absent"):
            payload["description"] += " [Source label deleted; archive retained]"
        m = mapped("label", x["id"])
        target = existing_labels.get(x["name"])
        if m:
            tid = m["target_id"]
        elif target:
            tid = target["id"]
        else:
            tid = api.gt(base + "/labels", "POST", payload)["id"]
        if needs_update(m, payload):
            api.gt(base + f"/labels/{tid}", "PATCH", payload)
            remember("label", x["id"], tid, payload)
        label_ids[x["id"]] = int(tid)
    milestones = {}
    existing_ms = {x["title"]: x for x in api.gt_pages(base + "/milestones?state=all")}
    for x in objects("milestone"):
        payload = {
            "title": x["title"],
            "description": x.get("description") or "",
            "state": x["state"],
        }
        if x.get("_archive_absent"):
            payload["description"] += "\n[Source milestone deleted; archive retained]"
        if x.get("due_on"):
            payload["due_on"] = x["due_on"]
        m = mapped("milestone", x["id"])
        target = existing_ms.get(x["title"])
        tid = (
            m["target_id"]
            if m
            else target["id"]
            if target
            else api.gt(base + "/milestones", "POST", payload)["id"]
        )
        if needs_update(m, payload):
            api.gt(base + f"/milestones/{tid}", "PATCH", payload)
            remember("milestone", x["id"], tid, payload)
        milestones[x["id"]] = int(tid)
    existing = api.gt_pages(base + "/issues?state=all&type=all")
    by_number = {x["number"]: x for x in existing}
    unavailable_prs = []
    for kind in ("issue", "pull"):
        for x in sorted(objects(kind), key=lambda v: v["number"]):
            marker = f"<!-- github-archive:{x['id']} -->"
            m = mapped(kind, x["id"])
            target = next((v for v in existing if marker in (v.get("body") or "")), None)
            if not target and m:
                target = by_number.get(int(m["target_id"]))
            if not target and not m:
                candidate = by_number.get(x["number"])
                # Original migration preserved numbering; require matching type/title.
                original_author = (x.get("user") or {}).get("login", "")
                migrated_author = candidate and candidate.get("original_author")
                if (
                    candidate
                    and bool(candidate.get("pull_request")) == (kind == "pull")
                    and (
                        candidate["title"] == x["title"]
                        or (migrated_author and migrated_author == original_author)
                    )
                ):
                    target = candidate
            number = int(m["target_id"]) if m else target["number"] if target else None
            payload = {
                "title": x["title"],
                "body": source_body(x),
                "state": x["state"],
                "milestone": milestones.get((x.get("milestone") or {}).get("id"), 0),
            }
            labels = [label_ids[v["id"]] for v in x.get("labels", []) if v["id"] in label_ids]
            if number is None:
                if kind == "pull":
                    head = x.get("head") or {}
                    head_repo = head.get("repo") or {}
                    if x["state"] == "open" and head_repo.get("id") == rid:
                        try:
                            created = api.gt(
                                base + "/pulls",
                                "POST",
                                {
                                    "title": payload["title"],
                                    "body": payload["body"],
                                    "base": x["base"]["ref"],
                                    "head": head["ref"],
                                    "labels": labels,
                                    "milestone": payload["milestone"],
                                },
                            )
                            number = created["number"]
                        except RuntimeError as e:
                            # Only validation conflicts permit archive fallback.
                            if not ("HTTP 409" in str(e) or "HTTP 422" in str(e)):
                                raise
                    # Merged/closed PRs, missing fork refs and no-diff PRs are represented
                    # by the complete archive, not by fake native merge operations.
                    if number is None:
                        unavailable_prs.append(
                            {
                                "number": x["number"],
                                "source": x["html_url"],
                                "reason": "native PR not present; original PR and review records archived",
                            }
                        )
                        continue
                else:
                    created = api.gt(
                        base + "/issues",
                        "POST",
                        {
                            "title": payload["title"],
                            "body": payload["body"],
                            "closed": x["state"] == "closed",
                            "labels": labels,
                            "milestone": payload["milestone"],
                        },
                    )
                    number = created["number"]
            fingerprint = dict(payload, _labels=labels)
            if needs_update(m, fingerprint):
                current = api.gt(base + f"/issues/{number}")
                update = {
                    k: v
                    for k, v in payload.items()
                    if (
                        int((current.get("milestone") or {}).get("id", 0)) != v
                        if k == "milestone"
                        else current.get(k) != v
                    )
                }
                if update:
                    update["content_version"] = current.get("content_version", 0)
                    api.gt(base + f"/issues/{number}", "PATCH", update)
                api.gt(base + f"/issues/{number}/labels", "PUT", {"labels": labels})
                remember(kind, x["id"], number, fingerprint)
            if (
                kind == "pull"
                and x.get("merged")
                and target
                and not (target.get("pull_request") or {}).get("merged")
            ):
                unavailable_prs.append(
                    {
                        "number": x["number"],
                        "source": x["html_url"],
                        "reason": "GitHub merged state retained in archive/body; native merge flag not changed",
                    }
                )
            comments = [
                v
                for v in objects("issue_comment")
                if v.get("issue_url", "").endswith("/" + str(x["number"]))
            ]
            existing_comments = (
                api.gt_pages(base + f"/issues/{number}/comments") if comments else []
            )
            used = {
                v["target_id"]
                for v in c.execute(
                    "SELECT target_id FROM mapping WHERE repo_id=? AND kind=?", (rid, "comment")
                )
            }
            for comment in comments:
                if x.get("_archive_absent"):
                    comment["_archive_absent"] = True
                cm = mapped("comment", comment["id"])
                cm_marker = f"<!-- github-archive:{comment['id']} -->"
                match = next(
                    (v for v in existing_comments if cm_marker in (v.get("body") or "")), None
                )
                if not match and not cm:
                    match = next(
                        (
                            v
                            for v in existing_comments
                            if str(v["id"]) not in used
                            and (v.get("body") or "") == (comment.get("body") or "")
                        ),
                        None,
                    )
                cp = {"body": source_body(comment)}
                cid = cm["target_id"] if cm else match["id"] if match else None
                if cid is None:
                    cid = api.gt(base + f"/issues/{number}/comments", "POST", cp)["id"]
                elif needs_update(cm, cp):
                    api.gt(base + f"/issues/comments/{cid}", "PATCH", cp)
                remember("comment", comment["id"], cid, cp)
                used.add(str(cid))
    existing_releases = {x["tag_name"]: x for x in api.gt_pages(base + "/releases")}
    releases_all = objects("release")
    active_release_tags = {x["tag_name"] for x in releases_all if not x.get("_archive_absent")}
    for x in releases_all:
        if x.get("_archive_absent") and x["tag_name"] in active_release_tags:
            continue
        links = "\n".join(
            f"- [{v['name']}]({v['browser_download_url']}) · {v['size']} bytes · {v.get('digest') or 'upstream checksum unavailable'}"
            for v in x.get("assets", [])
        )
        payload = {
            "tag_name": x["tag_name"],
            "target_commitish": x["target_commitish"],
            "name": x.get("name") or x["tag_name"],
            "body": source_body(x) + "\n\nAssets (links only):\n" + links,
            "draft": x["draft"],
            "prerelease": x["prerelease"],
        }
        m = mapped("release", x["id"])
        target = existing_releases.get(x["tag_name"])
        tid = m["target_id"] if m else target["id"] if target else None
        if needs_update(m, payload):
            if tid is None:
                tid = api.gt(base + "/releases", "POST", payload)["id"]
            else:
                api.gt(base + f"/releases/{tid}", "PATCH", payload)
            remember("release", x["id"], tid, payload)
    for kind in (
        "review",
        "review_comment",
        "review_comment_reaction",
        "issue_reaction",
        "comment_reaction",
        "issue_timeline",
    ):
        if c.execute(
            "SELECT 1 FROM object WHERE repo_id=? AND kind=? LIMIT 1", (rid, kind)
        ).fetchone():
            unavailable_prs.append(
                {
                    "category": kind,
                    "reason": "original records retained in archive; native Gitea activity not recreated",
                }
            )
    setting(c, f"projection_gaps:{rid}", json.dumps(unavailable_prs))
    setting(c, f"projection_ok:{rid}", now())


def worker(c, limit=5):
    with open(ROOT / "worker.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        api = API(c)
        if int(setting(c, "github_backoff_until") or 0) > now():
            return 0
        if now() - int(setting(c, "discovery_ok") or 0) >= 600:
            try:
                discover(c, api)
            except Exception as e:
                setting(c, "discovery_error", str(e)[:250])
                raise
        failures = 0
        for _ in range(limit):
            if now() - int(setting(c, "discovery_ok") or 0) >= 600:
                try:
                    discover(c, api)
                except Exception as e:
                    setting(c, "discovery_error", str(e)[:250])
                    raise
            job = c.execute(
                "SELECT * FROM job WHERE due<=? ORDER BY priority DESC,attempts,due LIMIT 1",
                (now(),),
            ).fetchone()
            if not job:
                break
            row = c.execute("SELECT * FROM repo WHERE id=?", (job["repo_id"],)).fetchone()
            if not row or not row["available"]:
                c.execute("DELETE FROM job WHERE repo_id=?", (job["repo_id"],))
                c.commit()
                continue
            start = now()
            c.execute("UPDATE job SET claimed=? WHERE repo_id=?", (start, row["id"]))
            c.commit()
            try:
                with visibility_lock(row["id"]):
                    latest = c.execute("SELECT * FROM repo WHERE id=?", (row["id"],)).fetchone()
                    protect_private_repo(api, latest or row)
                code_error = None
                if job["kind"] in ("all", "code"):
                    try:
                        sync_code(c, api, row)
                        sync_wiki(c, api, row)
                    except Exception as e:
                        code_error = e
                if job["kind"] in ("all", "metadata"):
                    sync_metadata(c, api, row)
                    if (GITROOT / (row["name"].lower() + ".git")).exists():
                        with visibility_lock(row["id"]):
                            latest = c.execute(
                                "SELECT * FROM repo WHERE id=?", (row["id"],)
                            ).fetchone()
                            protect_private_repo(api, latest or row)
                        project_gitea(c, api, row)
                if code_error:
                    raise code_error
                # Do not discard a webhook received during this job.
                c.execute(
                    "DELETE FROM job WHERE repo_id=? AND reason=? AND due=?",
                    (row["id"], job["reason"], job["due"]),
                )
                c.execute("UPDATE job SET claimed=0 WHERE repo_id=?", (row["id"],))
                c.execute("UPDATE repo SET error=NULL WHERE id=?", (row["id"],))
                print("OK", row["name"], flush=True)
            except Exception as e:
                # A concurrent writer can invalidate an existing WAL read
                # snapshot. Release any partial transaction before recording
                # the retry, otherwise bookkeeping can repeat BUSY_SNAPSHOT.
                c.rollback()
                failures += 1
                message = str(e)[:250]
                c.execute("UPDATE repo SET error=? WHERE id=?", (message, row["id"]))
                attempts = job["attempts"] + 1
                delay = min(3600, 60 * 2 ** min(attempts, 6))
                c.execute(
                    "UPDATE job SET attempts=?,due=?,claimed=0 WHERE repo_id=?",
                    (attempts, now() + delay, row["id"]),
                )
                print("FAIL", row["name"], message, flush=True)
            c.commit()
        housekeeping(c)
        setting(c, "worker_at", now())
        return 1 if failures else 0


def status(c):
    repos = [
        dict(x)
        for x in c.execute(
            "SELECT id,name,private,available,code_ok,metadata_ok,error FROM repo ORDER BY name"
        )
    ]
    for row in repos:
        row["gitea_projection_ok"] = setting(c, f"projection_ok:{row['id']}")
        row["gitea_projection_gaps"] = json.loads(
            setting(c, f"projection_gaps:{row['id']}") or "[]"
        )
        row["wiki_status"] = setting(c, f"wiki_status:{row['id']}")
        row["wiki_checked"] = setting(c, f"wiki_checked:{row['id']}")
        row["code_verified"] = setting(c, f"code_verified:{row['id']}")
        row["source_feature_gaps"] = json.loads(setting(c, f"feature_gaps:{row['id']}") or "[]")
        row["lfs_checked"] = setting(c, f"lfs_checked:{row['id']}")
        row["attachment_inventory"] = setting(c, f"attachment_inventory:{row['id']}")
    warnings = []
    if free() < LOW:
        warnings.append("Free space below configured minimum; synchronization paused")
    if now() - int(setting(c, "snapshot_ok") or 0) > 26 * 3600:
        warnings.append("Recovery snapshot missing or older than 26 hours")
    if now() - int(setting(c, "discovery_ok") or 0) > 1200:
        warnings.append("Repository discovery stale for more than 20 minutes")
    if any(row["error"] for row in repos):
        warnings.append("Some repositories have synchronization errors")
    if int(setting(c, "github_backoff_until") or 0) > now():
        warnings.append("GitHub rate limit active; jobs will retry after backoff")
    return {
        "free_bytes": free(),
        "attachment_policy": "links-only",
        "warnings": warnings,
        "github_backoff_until": setting(c, "github_backoff_until"),
        "snapshot_ok": setting(c, "snapshot_ok"),
        "discovery_error": setting(c, "discovery_error"),
        "capacity_state": "halt" if free() < LOW else "restricted" if free() < NORMAL else "normal",
        "discovery_ok": setting(c, "discovery_ok"),
        "repositories": repos,
        "queue": [dict(x) for x in c.execute("SELECT * FROM job ORDER BY due")],
        "objects": [
            dict(x) for x in c.execute("SELECT kind,count(*) AS count FROM object GROUP BY kind")
        ],
    }


def compressed_snapshot_copy(source, destination):
    original = hashlib.sha256()
    with source.open("rb") as src, gzip.open(destination, "wb", compresslevel=3) as dst:
        while block := src.read(1024**2):
            original.update(block)
            dst.write(block)
    restored = hashlib.sha256()
    with gzip.open(destination, "rb") as src:
        while block := src.read(1024**2):
            restored.update(block)
    if original.digest() != restored.digest():
        raise RuntimeError("compressed snapshot round-trip validation failed")
    destination.chmod(0o600)


def snapshot(c):
    # Both databases describe one projection. Copy them while no archive worker
    # can create a Gitea item and then a mapping between the two snapshots.
    with open(ROOT / "worker.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(ROOT / "ingest.lock", "a") as inbox_lock:
            fcntl.flock(inbox_lock, fcntl.LOCK_EX)
            _snapshot(c)


def snapshot_path(name, generation="current"):
    """Resolve a committed generation, with read compatibility for old flat snapshots."""
    directory = ROOT / "snapshots"
    index = directory / "generations.json"
    if index.exists():
        record = json.loads(index.read_text())
        value = record.get(generation)
        if value is not None:
            if not re.fullmatch(r"generation-[0-9]+-[a-f0-9]{16}", value):
                raise RuntimeError("invalid snapshot generation index")
            return directory / value / (name + (".tgz" if name == "configuration" else ".db.gz"))
        # An old flat current becomes the previous generation after first upgrade.
        if generation == "previous":
            old = directory / (
                name + ".current" + (".tgz" if name == "configuration" else ".db.gz")
            )
            if old.exists():
                return old
    return directory / (name + "." + generation + (".tgz" if name == "configuration" else ".db.gz"))


def _snapshot(c):
    targets = [("gitea", GITEA_DB), ("archive", ROOT / "state.db")]
    if (ROOT / "inbox.db").exists():
        targets.append(("inbox", ROOT / "inbox.db"))
    required = sum(p.stat().st_size for _, p in targets)
    if required > CONFIG.snapshot_budget or free() - 2 * required < LOW:
        raise RuntimeError("capacity: snapshot budget exceeded")
    directory = ROOT / "snapshots"
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    index = directory / "generations.json"
    previous = json.loads(index.read_text()).get("current") if index.exists() else None
    if previous and not re.fullmatch(r"generation-[0-9]+-[a-f0-9]{16}", previous):
        raise RuntimeError("invalid previous snapshot generation")
    generation = f"generation-{now()}-{secrets.token_hex(8)}"
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=directory) as staging:
        staging = Path(staging)
        for name, source in targets:
            temp = staging / (name + ".db")
            with contextlib.closing(
                sqlite3.connect("file:" + str(source) + "?mode=ro", uri=True)
            ) as src:
                src.execute("BEGIN")
                src.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                deadline = time.monotonic() + 120

                def backup_progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise RuntimeError("snapshot copy deadline exceeded")

                with contextlib.closing(sqlite3.connect(temp)) as dst:
                    src.backup(dst, pages=256, sleep=0.01, progress=backup_progress)
                    dst.commit()
                    dst.execute("PRAGMA journal_mode=DELETE")
                    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("snapshot integrity failed")
            compressed_snapshot_copy(temp, staging / (name + ".db.gz"))
            temp.unlink()
        configs = [
            Path(os.environ.get("GITEA_CONFIG_PATH", "/etc/gitea/app.ini")),
            Path(os.environ.get("ARCHIVE_ENV_FILE", "/etc/gitea/github-archive.env")),
            ROOT / "app.json",
            ROOT / "app-key.pem",
            ROOT / "webhook-secret",
        ]
        configs.extend(Path(__file__).parent.glob("*.py"))
        configs.extend(Path("/etc/systemd/system").glob("github-archive-*"))
        configs.append(Path("/etc/systemd/system/gitea.service.d/60-memory-budget.conf"))
        configs.extend(CONFIG.snapshot_paths)
        for subdir in ("templates", "public"):
            configs.extend((CONFIG.custom_dir / subdir).rglob("*"))
        config = staging / "configuration.tgz"
        with tarfile.open(config, "w:gz", dereference=True) as archive:
            for path in sorted(set(configs)):
                if path.is_file():
                    archive.add(path, arcname=str(path).lstrip("/"), recursive=False)
        config.chmod(0o600)
        with tarfile.open(config, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile() or member.size > 1024**2:
                    raise RuntimeError("unexpected configuration snapshot member")
                archive.extractfile(member).read()
        staging.rename(directory / generation)
    # Publish all DBs and configuration together, only after every check succeeds.
    # A failed copy leaves the last committed current/previous pair untouched.
    pending = directory / "generations.next.json"
    pending.write_text(json.dumps({"current": generation, "previous": previous}))
    pending.chmod(0o600)
    pending.replace(index)
    setting(c, "snapshot_ok", now())
    for path in directory.iterdir():
        if (
            path.name not in (generation, previous)
            and re.fullmatch(r"generation-[0-9]+-[a-f0-9]{16}", path.name)
            and path.is_dir()
            and not path.is_symlink()
        ):
            shutil.rmtree(path)


def format_time(value):
    if not value:
        return "Waiting for synchronization"
    return datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc).isoformat(
        timespec="seconds"
    )


class SessionProfile(HTMLParser):
    def __init__(self):
        super().__init__()
        self.usernames = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("id") == "username" and attrs.get("name") == "name":
            self.usernames.append(attrs.get("value", ""))


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authenticated_owner(headers):
    authorization = headers.get("Authorization", "")
    if authorization:
        req = urllib.request.Request(
            GITEA + "/api/v1/user", headers={"Authorization": authorization}
        )
        with http_open(req, timeout=10) as response:
            return json.load(response).get("login", "").lower() == ADMIN_USER.lower()
    cookie = headers.get("Cookie", "")
    if not cookie:
        return False
    # Gitea's API rejects browser sessions. Its protected profile form validates
    # the session and renders SignedUser.Name in a fixed server-controlled input.
    req = urllib.request.Request(GITEA + "/user/settings", headers={"Cookie": cookie})
    with urllib.request.build_opener(NoAuthRedirect()).open(req, timeout=10) as response:
        raw = response.read(1024**2 + 1)
        if response.status != 200 or len(raw) > 1024**2:
            return False
        profile = SessionProfile()
        profile.feed(raw.decode("utf-8"))
        return len(profile.usernames) == 1 and profile.usernames[0].lower() == ADMIN_USER.lower()


def app_manifest():
    if not CONFIG.public_url:
        raise RuntimeError("ARCHIVE_PUBLIC_URL is required for GitHub App setup")
    return {
        "name": CONFIG.app_name,
        "url": CONFIG.public_url,
        "public": False,
        "redirect_url": CONFIG.public_url + "/github-archive/callback",
        "hook_attributes": {"url": CONFIG.public_url + "/github-archive/webhook", "active": True},
        "default_permissions": {
            "contents": "read",
            "issues": "read",
            "pull_requests": "read",
            "metadata": "read",
        },
        "default_events": [
            "push",
            "repository",
            "issues",
            "issue_comment",
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "release",
            "label",
            "milestone",
            "gollum",
        ],
        "description": f"Read-only continuous archive of repositories owned by {OWNER}.",
    }


def consume_setup_token(c, key, expiry_key, supplied):
    """Atomically expire and consume setup capabilities before external side effects."""
    c.execute("BEGIN IMMEDIATE")
    expected = c.execute("SELECT value FROM setting WHERE key=?", (key,)).fetchone()
    expiry = c.execute("SELECT value FROM setting WHERE key=?", (expiry_key,)).fetchone()
    valid = bool(
        expected
        and expected[0]
        and expiry
        and int(expiry[0]) >= now()
        and hmac.compare_digest(expected[0], supplied)
    )
    if valid:
        c.execute("DELETE FROM setting WHERE key IN (?,?)", (key, expiry_key))
    c.commit()
    return valid


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # Do not log URLs, callback codes, credentials or raw webhook bodies.
        pass

    def send(self, code, body, kind="application/json"):
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/privacy-guard":
            try:
                with contextlib.closing(inbox_db()) as queue:
                    pending = queue.execute(
                        "SELECT 1 FROM delivery WHERE done=0 AND privacy_pending=1 LIMIT 1"
                    ).fetchone()
                self.send(
                    403 if pending else 204,
                    {"error": "privacy transition pending"} if pending else "",
                )
            except (sqlite3.Error, OSError):
                self.send(403, {"error": "privacy protection unavailable"})
            return
        with contextlib.closing(db()) as c:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/callback":
                query = urllib.parse.parse_qs(parsed.query)
                state = query.get("state", [""])[0]
                code = query.get("code", [""])[0]
                if not code:
                    self.send(400, {"error": "registration code required"})
                    return
                if not consume_setup_token(c, "app_setup_state", "app_setup_expiry", state):
                    self.send(403, {"error": "invalid, expired or consumed registration state"})
                    return
                req = urllib.request.Request(
                    GITHUB_API
                    + "/app-manifests/"
                    + urllib.parse.quote(code, safe="")
                    + "/conversions",
                    b"{}",
                    {
                        "Accept": "application/vnd.github+json",
                        "User-Agent": "github-account-archive/0.1",
                    },
                )
                try:
                    with http_open(req, timeout=30) as r:
                        app = json.load(r)
                except urllib.error.URLError:
                    self.send(502, {"error": "registration exchange failed"})
                    return
                for filename, data in [
                    ("app-key.pem", app["pem"]),
                    ("app.json", json.dumps({"id": app["id"], "slug": app["slug"]})),
                    ("webhook-secret", app["webhook_secret"]),
                ]:
                    target = ROOT / filename
                    target.write_text(data)
                    target.chmod(0o600)
                setting(c, "app_setup_state", "")
                self.send(
                    200,
                    '<!doctype html><meta charset="utf-8"><h1>GitHub App created</h1>'
                    "<p>Install for "
                    + html.escape(OWNER)
                    + ', selecting All repositories.</p><a href="https://github.com/apps/'
                    + html.escape(app["slug"])
                    + '/installations/new">Install GitHub App</a>',
                    "text/html",
                )
                return
            if self.path == "/auth":
                # Validate the owner via Gitea; never trust a client identity header.
                try:
                    if not authenticated_owner(self.headers):
                        raise ValueError("not owner")
                except (urllib.error.URLError, ValueError):
                    self.send(401, {"error": "sign in to Gitea as owner"})
                    return
                self.send_response(204)
                self.send_header("X-Archive-User", ADMIN_USER)
                self.end_headers()
                return
            if self.path == "/health":
                # Readiness must remain cheap while initial archiving grows the DB.
                # Full integrity checks belong to the bounded snapshot job.
                self.send(
                    200,
                    {
                        "service": "github-archive",
                        "database_available": c.execute("SELECT 1").fetchone()[0] == 1,
                    },
                )
                return
            # Nginx supplies trusted, authenticated identity, strips all client headers.
            bootstrap = parsed.path.startswith("/bootstrap/")
            if bootstrap:
                token = parsed.path.rsplit("/", 1)[-1]
                if not consume_setup_token(c, "bootstrap_token", "bootstrap_expiry", token):
                    self.send(403, {"error": "invalid or expired setup link"})
                    return
            if not bootstrap and self.headers.get("X-Archive-User") != ADMIN_USER:
                self.send(403, {"error": "owner authentication required"})
                return
            path = urllib.parse.urlparse(self.path).path
            if path == "/setup" or bootstrap:
                if (ROOT / "app.json").exists():
                    self.send(409, {"error": "GitHub App already configured"})
                    return
                try:
                    manifest = app_manifest()
                except RuntimeError:
                    self.send(503, {"error": "ARCHIVE_PUBLIC_URL must be configured"})
                    return
                state = secrets.token_urlsafe(32)
                setting(c, "app_setup_state", state)
                setting(c, "app_setup_expiry", now() + 900)
                registration = (
                    "https://github.com/organizations/" + OWNER + "/settings/apps/new"
                    if CONFIG.account_type == "organization"
                    else "https://github.com/settings/apps/new"
                )
                self.send(
                    200,
                    '<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                    "<h1>Create a read-only GitHub Archive App</h1><p>Read code, issues, PRs and metadata without granting GitHub write access.</p>"
                    '<form method="post" action="'
                    + html.escape(registration)
                    + "?state="
                    + state
                    + '">'
                    '<input type="hidden" name="manifest" value="'
                    + html.escape(json.dumps(manifest), quote=True)
                    + '"><button type="submit">Review and create on GitHub</button></form>',
                    "text/html",
                )
            elif path == "/status.json":
                self.send(200, status(c))
            elif path == "/":
                s = status(c)
                rows = "".join(
                    '<tr><td><a href="repo/'
                    + str(x["id"])
                    + '">'
                    + html.escape(x["name"])
                    + "</a></td><td>"
                    + format_time(x["code_ok"])
                    + "</td><td>"
                    + format_time(x["metadata_ok"])
                    + "</td><td>"
                    + html.escape(x["error"] or "")
                    + "</td></tr>"
                    for x in s["repositories"]
                )
                completed_code = sum(bool(x["code_ok"]) for x in s["repositories"])
                completed_text = sum(bool(x["metadata_ok"]) for x in s["repositories"])
                self.send(
                    200,
                    '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60"><title>GitHub archive</title>'
                    "<style>body{margin:2rem auto;max-width:1200px;padding:0 1rem;font:15px system-ui;line-height:1.6}"
                    "table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.4rem .7rem;border-bottom:1px solid #ddd}"
                    "a{color:#1768ad}[role=alert]{color:#a21b1b}</style>"
                    "<h1>Continuous GitHub archive</h1><p>Attachment policy: source links only; free space: "
                    + str(round(free() / 1024**2))
                    + " MiB</p>"
                    + f"<p>Discovered {len(s['repositories'])} repositories · code synchronized: {completed_code} · metadata archived: {completed_text} · refreshes every minute</p>"
                    + "".join('<p role="alert">' + html.escape(w) + "</p>" for w in s["warnings"])
                    + "<p>Recovery snapshot: "
                    + format_time(s["snapshot_ok"])
                    + '</p><p><a href="status.json">Full status</a></p>'
                    "<table><tr><th>Repositories</th><th>Code synchronized</th><th>Metadata synchronized</th><th>Errors</th></tr>"
                    + rows
                    + "</table>",
                    "text/html",
                )
            elif path.startswith("/repo/"):
                try:
                    rid = int(path.rsplit("/", 1)[1].removesuffix(".json"))
                except ValueError:
                    self.send(400, {"error": "invalid repository"})
                    return
                objects = [
                    dict(
                        kind=x["kind"],
                        id=x["id"],
                        data=decode(x["body"]),
                        source_presence=[
                            dict(p)
                            for p in c.execute(
                                "SELECT scope,present,checked FROM presence WHERE repo_id=? AND kind=? AND id=?",
                                (rid, x["kind"], x["id"]),
                            )
                        ],
                    )
                    for x in c.execute(
                        "SELECT * FROM object WHERE repo_id=? ORDER BY kind,id", (rid,)
                    )
                ]
                if path.endswith(".json"):
                    self.send(200, objects)
                else:
                    repo = c.execute("SELECT name FROM repo WHERE id=?", (rid,)).fetchone()
                    if not repo:
                        self.send(404, {"error": "repository not found"})
                        return
                    sections = []
                    for obj in objects:
                        data = obj["data"]
                        title = str(
                            data.get("title")
                            or data.get("name")
                            or data.get("event")
                            or data.get("state")
                            or data.get("filename")
                            or obj["id"]
                        )
                        label = (
                            obj["kind"]
                            + " · "
                            + (str(data["number"]) + " · " if "number" in data else "")
                            + title
                        )
                        sections.append(
                            "<details><summary>"
                            + html.escape(label)
                            + "</summary><pre>"
                            + html.escape(json.dumps(obj, ensure_ascii=False, indent=2))
                            + "</pre></details>"
                        )
                    self.send(
                        200,
                        '<!doctype html><meta charset="utf-8"><title>'
                        + html.escape(repo["name"])
                        + " · GitHub archive</title><style>body{max-width:1100px;margin:2rem auto;padding:0 1rem;font:16px system-ui}"
                        "details{border-bottom:1px solid #ddd;padding:.6rem}summary{cursor:pointer}"
                        "pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}</style>"
                        '<a href="../">All repositories</a><h1>'
                        + html.escape(repo["name"])
                        + "</h1>"
                        "<p>Original GitHub records are retained; source_presence indicates presence at the latest check. Attachments are source links only.</p>"
                        '<a href="'
                        + str(rid)
                        + '.json">Download full JSON</a>'
                        + "".join(sections),
                        "text/html",
                    )
            else:
                self.send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/webhook":
            self.send(404, {"error": "not found"})
            return
        secretfile = ROOT / "webhook-secret"
        secret = (
            secretfile.read_text().strip()
            if secretfile.exists()
            else os.environ.get("WEBHOOK_SECRET", "")
        )
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0:
                raise ValueError
        except ValueError:
            self.send(400, {"error": "invalid content length"})
            return
        if size > 2 * 1024**2 or not secret:
            self.send(
                413 if size > 2 * 1024**2 else 503,
                {"error": "payload limit or webhook unconfigured"},
            )
            return
        raw = self.rfile.read(size)
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.headers.get("X-Hub-Signature-256", "")):
            self.send(401, {"error": "invalid signature"})
            return
        try:
            payload = json.loads(raw)
        except ValueError:
            self.send(400, {"error": "invalid JSON"})
            return
        delivery = self.headers.get("X-GitHub-Delivery", "")
        event = self.headers.get("X-GitHub-Event", "")
        if not delivery:
            self.send(400, {"error": "delivery ID required"})
            return
        if not isinstance(payload, dict) or len(delivery) > 200 or len(event) > 100:
            self.send(400, {"error": "invalid envelope"})
            return
        try:
            inserted = receive_delivery(payload, raw, delivery, event)
        except (sqlite3.Error, OSError, RuntimeError):
            self.send(503, {"error": "durable inbox unavailable; retry delivery"})
            return
        if not inserted:
            self.send(200, {"duplicate": True})
            return
        self.send(202, {"queued": True})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--env-file", type=Path, help="literal environment file; never executed as shell"
    )
    p.add_argument(
        "command",
        choices=[
            "work",
            "discover",
            "status",
            "serve",
            "snapshot",
            "ingest",
            "bootstrap",
            "manifest",
            "check",
        ],
    )
    p.add_argument("--limit", type=int, default=5)
    a = p.parse_args()
    if a.env_file:
        os.environ.update(read_env_file(a.env_file))
        os.environ["ARCHIVE_ENV_FILE"] = str(a.env_file.resolve())
    configure(load_config())
    CONFIG.validate()
    if a.limit < 1:
        p.error("--limit must be positive")
    if a.command == "manifest":
        print(json.dumps(app_manifest(), indent=2))
        return
    if a.command in ("work", "ingest") and not os.environ.get("GITEA_TOKEN"):
        raise RuntimeError("GITEA_TOKEN is required for native archive writes")
    if a.command == "check":
        required = ("git", "openssl")
        import shutil

        missing = [name for name in required if not shutil.which(name)]
        if missing:
            raise RuntimeError("required commands missing: " + ", ".join(missing))
        if not GITEA_DB.is_file():
            raise RuntimeError("GITEA_DB_PATH must point to the local Gitea SQLite database")
        with contextlib.closing(
            sqlite3.connect("file:" + str(GITEA_DB) + "?mode=ro", uri=True, timeout=30)
        ) as native:
            journal = native.execute("PRAGMA journal_mode").fetchone()[0]
            for table in ("user", "repository", "branch"):
                native.execute('SELECT 1 FROM "' + table + '" LIMIT 1').fetchone()
            for login in (GITEA_OWNER, ADMIN_USER):
                if not native.execute(
                    "SELECT id FROM user WHERE lower_name=?", (login.lower(),)
                ).fetchone():
                    raise RuntimeError("configured Gitea namespace/admin must already exist")
            native.execute("SELECT name,commit_id,is_deleted FROM branch LIMIT 0")
        print(
            json.dumps(
                {
                    "github_owner": OWNER,
                    "gitea_owner": GITEA_OWNER,
                    "archive_admin_user": ADMIN_USER,
                    "native_journal_mode": journal,
                    "github_auth": "app"
                    if (ROOT / "app.json").exists()
                    else "token"
                    if os.environ.get("GITHUB_TOKEN")
                    else "public",
                    "warnings": []
                    if journal == "wal"
                    else ["Enable SQLITE_JOURNAL_MODE=WAL in Gitea before synchronization"],
                }
            )
        )
        return
    if a.command == "serve":
        with contextlib.closing(db()):
            pass
        with contextlib.closing(inbox_db(initialize=True)):
            pass
        with ThreadingHTTPServer(("127.0.0.1", CONFIG.port), Handler) as server:
            server.serve_forever()
        return
    with contextlib.closing(db()) as c:
        if a.command == "status":
            print(json.dumps(status(c), indent=2))
        elif a.command == "discover":
            discover(c, API(c))
        elif a.command == "snapshot":
            snapshot(c)
        elif a.command == "ingest":
            ingest(c, a.limit)
        elif a.command == "work":
            raise SystemExit(worker(c, a.limit))
        elif a.command == "bootstrap":
            app_manifest()  # Require a configured HTTPS origin before issuing a capability.
            if (ROOT / "app.json").exists():
                raise RuntimeError("GitHub App already configured")
            token = secrets.token_urlsafe(32)
            setting(c, "bootstrap_token", token)
            setting(c, "bootstrap_expiry", now() + 900)
            print(CONFIG.public_url + "/github-archive/bootstrap/" + token)


def cli():
    try:
        main()
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
        print("ERROR:", error, file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    cli()

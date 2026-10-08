# GitHub Account Archive

A continuous, single-host archive of repositories owned by any GitHub **user or organization**, with a Gitea browsing interface. GitHub remains the source of truth. The service never pushes changes back to source repositories.

[中文说明](README.zh-CN.md) · [Deployment](docs/deployment.md) · [Configuration](docs/configuration.md) · [Architecture](docs/architecture.md) · [Recovery](docs/operations.md)

## What it preserves

| Source | Local archive | Gitea representation |
| --- | --- | --- |
| Code | Branches, tag objects, PR head refs, previously observed tips, accessible wiki Git repositories | Native Git repositories; branch records refreshed through Gitea's own hook |
| Issues | Current source JSON, comments, events, timeline, reactions, labels and milestones | Issues, comments, labels and milestones with source attribution |
| Pull requests | List and full detail records, reviews, review comments and reactions, commits, file manifests and available patches | Representable PRs; explicit gaps for merged, missing-fork or otherwise incompatible PRs |
| Releases | Release records and all enumerated asset metadata, links, sizes and available digests | Releases and source asset links |
| Attachments / LFS | Attachment links and default-branch LFS pointer inventory; historical pointers retained in Git | Links and manifests; binary payloads are **not downloaded** |

Public, private, forked and archived repositories are included when the configured GitHub credentials can read them. New repositories are discovered through signed webhooks and periodic inventory scans. Renames retain their numeric source identity. A deleted or inaccessible source never causes deletion of its local archive.

## Requirements

- Linux with systemd and Nginx, Python **3.11+**, Git and OpenSSL. Python runtime dependencies: **standard library only**.
- A **local Gitea 28 instance backed by SQLite**, using WAL mode. The hook and native audit integration was developed against Gitea 28; other versions and database engines have not been validated.
- A dedicated Gitea user or organization for the archive and a user API token with repository read/write and user-profile read permissions. No Gitea administrator token is required by the runtime.
- An optional read-only GitHub App or PAT for private repositories. Public accounts can be polled without credentials, subject to GitHub's unauthenticated limits.
- HTTPS and a public callback/webhook URL for the App setup and event-driven updates.

One installation archives **one account**. Use separate state directories, local namespaces, ports and systemd unit names for multiple installations.

## Quick start

```sh
git clone https://github.com/xixu-me/github-archive.git
cd github-archive
python3 -m unittest discover -s tests -p 'test_*.py'

# Install files without starting services or replacing an existing env file.
sudo python3 scripts/install.py
sudoedit /etc/gitea/github-archive.env

# After setting up the Gitea namespace, token and WAL mode:
sudo -u git python3 archive.py --env-file /etc/gitea/github-archive.env check
```

Continue with [deployment](docs/deployment.md) to configure Nginx, register/install a read-only GitHub App, and enable the discovery/ingest/worker/snapshot timers. A PAT deployment is also documented. The installer can render into a staging directory with `--root` and does not download Gitea, modify its configuration or start services.

For a runtime-only Python installation, `python3 -m pip install .` provides the `github-archive` command. Systemd deployments use the checked-out installer so the audits and deployment examples are installed together.

## Defaults and safeguards

- Durable webhook inbox commits before ACK; separate SQLite databases keep ACKs away from the worker's long writes. Signed delivery IDs are deduplicated for seven days.
- Private-source transitions fail closed through the reverse proxy until local visibility is confirmed. Include the supplied Nginx privacy guard; do not expose the Python backend directly.
- The raw archive dashboard and exports require the configured Gitea administrator user. They are marked `noindex`; public native Gitea repositories remain browsable.
- Git credentials stay out of process arguments. API tokens are restricted to the configured origin, and authenticated HTTP redirects are rejected.
- The state directory is bound to its GitHub/Gitea namespace; accidentally changing accounts cannot reuse the same database.
- Disk thresholds and memory limits bound work; recoverable jobs remain queued with backoff. Default low-space cutoff: 500 MiB.
- Daily current/previous database/configuration snapshots are local recovery aids. They do **not** protect against losing the host or disk.

## Scope and limits

This is a continuous archive of observed source state, not a point-in-time transaction across all GitHub endpoints. It preserves the latest captured JSON for each source object, presence/deletion evidence, and observed prior Git tips; it does not version every historical JSON edit or recover source data never observed. GitHub rate limits and large queues affect latency. Polling cannot capture a repository created and deleted between scans; webhooks reduce that gap but are not a guarantee.

It does not archive Actions runs/artifacts, packages, Discussions, Projects, release binaries, issue attachment bytes or LFS object bytes. Source authors and timestamps are retained as data and attribution rather than impersonated native activity. PR file manifest limits and native representation gaps are visible. Private credentials cannot make inaccessible repositories visible.

## Verification and development

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m compileall -q archive.py archive_config.py audit_*.py tests scripts
python3 scripts/check_public.py
```

Independent [acceptance tools](docs/operations.md#acceptance) compare live Git refs, source parent/child metadata, native projections and restored snapshots. Reports stay in the private state directory. Local tests use temporary repositories and fixtures; they do not write to GitHub.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE).

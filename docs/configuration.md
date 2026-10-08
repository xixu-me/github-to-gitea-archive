# Configuration

Configuration comes from environment variables or `--env-file`. Environment files contain literal `KEY=value` assignments, optional quotes and comments; they are parsed without shell execution or variable expansion. Do not put secrets in command arguments.

| Variable | Default / meaning |
| --- | --- |
| `GITHUB_OWNER` | **Required** GitHub user or organization login; no account is silently selected |
| `GITHUB_ACCOUNT_TYPE` | `auto`; or `user` / `organization` |
| `GITEA_OWNER` | Source login; target Gitea user or organization |
| `ARCHIVE_ADMIN_USER` | Target login; actual Gitea user authorized for the dashboard and hook pusher |
| `GITEA_URL` | `http://127.0.0.1:3000`; internal Gitea base URL |
| `GITEA_TOKEN` | Target namespace repository read/write plus `read:user` token; required for worker and ingest |
| `GITHUB_TOKEN` | Optional read-only PAT; ignored when App credentials exist |
| `ARCHIVE_PUBLIC_URL` | HTTPS origin; required for App manifest/bootstrap; subpath deployment is not supported |
| `GITHUB_APP_NAME` | Account-derived App name, maximum 34 characters |
| `WEBHOOK_SECRET` | PAT-mode HMAC secret; App-mode secret file takes precedence |
| `ARCHIVE_ROOT` | `/var/lib/github-archive`; private state/inbox, App credentials, snapshots and audit reports |
| `ARCHIVE_PORT` | `3091`; backend always binds to loopback |
| `GITEA_REPO_ROOT` | `/var/lib/gitea/repositories`; **parent** of local owner directory |
| `GITEA_DB_PATH` | `/var/lib/gitea/data/gitea.db`; local native SQLite database |
| `GITEA_CUSTOM_DIR` | `/var/lib/gitea/custom`; optional template/public files included in snapshots |
| `ARCHIVE_MIN_FREE_MIB` | `500`; refuse API work/webhook writes and stop long Git work below this |
| `ARCHIVE_NORMAL_FREE_MIB` | `1536`; threshold for restricted/normal status |
| `ARCHIVE_SNAPSHOT_BUDGET_MIB` | `512`; maximum source database bytes for local snapshots |
| `ARCHIVE_SNAPSHOT_PATHS` | Empty; colon-separated absolute configuration files to include in private snapshots |
| `GITEA_CONFIG_PATH` | `/etc/gitea/app.ini`; Gitea config snapshot source |
| `ARCHIVE_ENV_FILE` | `/etc/gitea/github-archive.env`; private environment snapshot source; set by CLI/installer |

State is bound to the case-insensitive source/target pair. Changing either requires a fresh state directory and dedicated target namespace, or an explicitly planned migration. Renaming repositories is handled by numeric source identity; changing account identity is not a rename.

Memory/CPU and schedule settings are systemd properties in `deploy/systemd`, not environment options. HTTP responses are bounded to 16 MiB; webhook payloads to 2 MiB; pending inbox payloads to 64 MiB/10,000 deliveries; ETag cache to 64 MiB and 30 days. Full raw object records are not removed by cache cleanup. These bounds deliberately prefer an explicit retry/error over unbounded memory or silent truncation.

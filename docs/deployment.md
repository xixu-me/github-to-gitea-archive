# Deployment

These instructions assume an existing Linux Gitea 28 + SQLite installation run by `git:git`, with its HTTP endpoint bound to `127.0.0.1:3000`. Adapt paths in the environment file before rerunning the installer. For another service user, pass `--user` and use that user in the commands below. Gitea, TLS certificates and DNS are prerequisites, not installed by this project.

## 1. Prepare Gitea and credentials

Create a **dedicated** Gitea archive user or organization through its administrator interface. GitHub remains authoritative and synchronization can replace the archive namespace's branches and metadata. Do not point it at repositories used for independent development.

Merge the settings in [`deploy/gitea-settings.ini`](../deploy/gitea-settings.ini) into the existing Gitea `app.ini`; retain all database paths and secrets. This enables SQLite WAL and disables self-registration, OpenID signup and OAuth auto-registration. Restart Gitea after backing up its configuration.

Create a Gitea API token for the human/service **user** that manages the archive. Give it repository read/write permissions for the target namespace and `read:user` permission for authenticated profile checks; dashboard authentication uses that same user's login. When the target is an organization, set `GITEA_OWNER` to the organization and `ARCHIVE_ADMIN_USER` to the managing user. These tokens do not require Gitea administrator API access.

## 2. Install and configure

```sh
sudo python3 scripts/install.py
sudoedit /etc/gitea/github-archive.env
sudo python3 scripts/install.py
sudo systemctl daemon-reload
```

Set `GITHUB_OWNER`, `GITEA_TOKEN` and `ARCHIVE_PUBLIC_URL`. Set the namespace/admin overrides if needed. The second installer pass renders sandbox paths and ports from the configured file without replacing it. The environment file is `root:git` 0640; state is `git:git` 0700. Files contain credentials and must not be copied into tickets or commits.

To review generated files without touching the system:

```sh
python3 scripts/install.py --root /tmp/github-archive-staging
```

A custom environment location is supported by `--env-file /absolute/path`. Runtime commands and audits accept the same flag. Changing custom state/DB/repository paths requires regenerating the units because systemd's write sandbox must match them. The installer currently creates one named instance; multiple installations require separate service names and Nginx routes in addition to isolated state, namespace and ports.

```sh
sudo -u git /usr/local/lib/github-archive/archive.py --env-file /etc/gitea/github-archive.env check
```

## 3. Protect the HTTPS reverse proxy

Install Nginx with the `auth_request` module. Include `/etc/nginx/snippets/github-archive.conf` **inside the existing HTTPS Gitea server block**, replacing its previous `location /` definition. Adapt its Gitea upstream if it differs from port 3000. The installer renders the archive backend port.

The snippet supplies all archive routes and places `auth_request /_github_archive_privacy` on native Gitea pages and API routes. Do not add another unguarded `location` for repositories/API; that would bypass privatization protection. Keep Gitea and the Python backend private to the host, close external access to their ports, and terminate HTTPS at Nginx. The archive validates Gitea credentials again at the backend and never accepts `X-Archive-User` as proof of identity. Preserve Cookie/Authorization headers as in the snippet.

```sh
sudo systemctl enable --now github-archive-web.service
sudo nginx -t
sudo systemctl reload nginx
```

The private archive is under `/archive/`; it requires the configured Gitea user's browser session or API token. Native private Gitea repositories also retain Gitea's own access controls. The privacy guard deliberately blocks native access while a public-to-private transition cannot be safely completed; a broken guard fails closed.

## 4A. Recommended: read-only GitHub App

```sh
sudo -u git /usr/local/lib/github-archive/archive.py --env-file /etc/gitea/github-archive.env manifest
sudo -u git /usr/local/lib/github-archive/archive.py --env-file /etc/gitea/github-archive.env bootstrap
```

The first command prints the permission manifest without secrets. The second prints a **secret, single-use URL**, valid for 15 minutes. Open it privately, review the manifest on GitHub, create the App, then follow the installation link. Install on `GITHUB_OWNER` and select **All repositories**, including future repositories. For an organization-owned App, explicitly set `GITHUB_ACCOUNT_TYPE=organization` before setup; a user-owned App installed into the organization also works.

The App requests read-only `contents`, `issues`, `pull_requests` and `metadata` permissions. These cover code/wiki/releases and issue/PR child records. Selected events are push, repository, issues, issue_comment, pull_request, pull_request_review, pull_request_review_comment, release, label, milestone and gollum. GitHub additionally sends installation events. Installation access tokens are refreshed automatically. Apps configured for selected repositories are rejected because they cannot fulfill an all-repository archive.

The callback exchanges the GitHub manifest code and saves `app.json`, `app-key.pem` and `webhook-secret` in the private state directory with mode 0600. Callback state also expires after 15 minutes and is consumed before exchange. A failed/expired exchange needs a new setup link; configured App keys cannot be overwritten through setup. For an existing App, place these three files into the state directory as the service user, with the same permissions; `app.json` contains `{"id": 123, "slug": "your-app-slug"}`.

Official references: [App manifest flow](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest), [installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

## 4B. Alternative: PAT or public polling

Without `app.json`, the runtime uses `GITHUB_TOKEN`. For a personal account, use a read-only token owned by that account with access to all intended repositories. Fine-grained tokens need read-only contents, issues, pull requests and metadata permissions. For organizations, obtain authorized read access and set `GITHUB_ACCOUNT_TYPE=organization` if you want to avoid auto-detection. A token for a different individual cannot enumerate that person's private repositories; the service falls back to their public inventory.

Without a token, only public repositories are visible; GitHub's low unauthenticated quota makes large archives slow. Polling-only operation needs no webhook. For event-driven PAT operation, generate a random webhook secret privately, set `WEBHOOK_SECRET`, and configure GitHub webhooks to `https://YOUR_DOMAIN/github-archive/webhook` with the events above. The runtime never creates or modifies source repository webhooks. Restart the web service after changing its environment.

## 5. Enable continuous work

```sh
sudo systemctl enable --now github-archive-ingest.timer github-archive-discovery.timer github-archive-worker.timer github-archive-snapshot.timer
sudo -u git /usr/local/lib/github-archive/archive.py --env-file /etc/gitea/github-archive.env discover
sudo systemctl start --no-block github-archive-worker.service
```

Ingest wakes every two seconds; independent discovery runs every ten minutes; the serial worker wakes one minute after its previous batch completes. Discovery also runs inside the worker as a fallback. Unchanged repositories receive hourly reconciliation; daily deep scans refresh issue/PR child collections. Webhook jobs take priority. Initial archival can take substantial time; inspect the authenticated status page and logs before accepting completion.

For constrained hosts, the included units use conservative memory/CPU budgets. [`deploy/gitea-memory-budget.conf`](../deploy/gitea-memory-budget.conf) and the installed `gitea-git.py` launcher ([source](../src/github_archive/gitea_git.py)) are optional Gitea safeguards. If enabling the wrapper, set Gitea `[git] PATH` only after installation; it bounds oversized web diff/show/log work without altering clone/fetch/packing or raw blob bytes. Adjust budgets to the machine rather than assuming a fixed host size.

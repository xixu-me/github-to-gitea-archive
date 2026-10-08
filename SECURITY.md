# Security

For vulnerabilities that could expose private repositories, tokens or archive records, use GitHub's private vulnerability reporting for this repository. Do not open a public issue containing exploit details or credentials. For non-sensitive bugs, open an ordinary issue with a minimal sanitized reproduction.

The supported security configuration uses one dedicated local archive namespace, loopback-only backends, HTTPS Nginx with the included authentication/privacy guard, a read-only GitHub App/PAT, a scoped Gitea repository token, a private 0700 state directory and restricted environment/key permissions. Gitea itself must be maintained and self-registration disabled for a personal archive deployment.

The backend verifies Gitea credentials on every protected request; forged proxy identity headers do not grant access. Keep backends private because native Gitea privacy transitions still require the Nginx guard. Exposing backend ports or bypassing that guard is unsupported. Local administrators/service-user compromise can read credentials and private archives. `noindex` is a crawler instruction, not authentication.

Do not attach database/configuration snapshots, raw archive exports, audit reports, App keys or environment files to issues. Rotate affected source/Gitea tokens and webhook secrets after accidental exposure; removing a committed secret from the latest tree does not remove it from Git history.

This is the initial 0.1 release. Only the latest release receives fixes; Python 3.11+ and local Gitea 28 with SQLite are the validated integration target. Other Gitea versions/database engines require independent compatibility testing.

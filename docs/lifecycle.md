# Source lifecycle and retained archives

The archive tracks additions, edits and removals. Removing source data does not authorize destroying its last local copy.

| Source change | Current view | Retained evidence |
| --- | --- | --- |
| New repository | Automatically discovered and synchronized | Stable GitHub repository ID |
| Rename or edit | Updates local repository name, description and captured metadata | Latest captured JSON; lifecycle transitions |
| Delete branch or tag | Fetch/prune removes the current ref | Previously observed tips under `refs/archive/history/` |
| Delete issue, comment, review or release | Source-presence state becomes absent; representable native records carry an absence notice | Last captured object remains available |
| Delete parent issue/PR/release | Descendant presence is marked absent, including comments/reactions, reviews/files and release assets | All previously captured descendants remain stored |
| Confirmed repository deletion | `deleted`; synchronization stops; Gitea description says archive retained | Last Git objects, raw records and lifecycle evidence |
| Confirmed transfer to another account | `transferred`, with destination when known; original archive retained | Repository identity and transfer evidence |
| Missing from a successful inventory or installation access removed | `unavailable`; never inferred to be deleted | Last observed data and the reason for unavailability |
| Reappears under the configured account with the same ID | `active`; resumes synchronization, removes description warning and forces a deep metadata/wiki refresh | Retained old records are not discarded |

`/archive/status.json` exposes `source_state` (`state`, `evidence`, `checked`, optional `destination`) per repository. The dashboard shows the state beside its name. Repository lifecycle transitions are stored as `repository_lifecycle` raw objects. Gitea descriptions distinguish retained unavailable repositories from active sources without changing private visibility.

A signed repository webhook can provide explicit deletion or transfer evidence. Installation removal only proves loss of access and cannot overwrite an already confirmed deletion or transfer, even when GitHub sends the events in that order. A missing inventory entry or HTTP 404 cannot distinguish deletion from a transfer or permission loss; the service keeps the honest `unavailable` state until stronger evidence arrives. Availability of specific webhook actions depends on GitHub and the installation's permissions. No extra GitHub write permissions are requested.

Inventories are only applied after successful complete enumeration. Enumeration errors do not mark all repositories absent. A lifecycle event received after an inventory started takes precedence over that stale inventory. Delivery deduplication and coalesced retry jobs cover replay and temporary Gitea failures.

Issue/PR/release and comment/review webhooks invalidate the deep-scan cache, so changes to descendants do not depend on a parent's `updated_at`. Periodic discovery, hourly reconciliation and daily deep scans still cover missed events. This is eventual synchronization: rate limits, unavailable endpoints and queue depth affect latency, and data created and removed between observations may never be captured.

The service retains the latest captured JSON, not every edit revision. A Gitea native PR cannot represent every GitHub PR state; raw records and projection gaps remain authoritative. Release attachments and LFS bytes remain links/manifests/pointers, not local binary copies. Permanent archive removal is a separate administrative action.

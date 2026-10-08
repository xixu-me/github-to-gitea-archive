# Operations and recovery

## Monitoring

Use the configured administrator's Gitea session to open `/archive/` or `/archive/status.json`. The dashboard exposes discovered repositories, code/metadata timestamps, queue state, projection gaps, attachment policy, free space, source errors and quota backoff. `/archive/repo/NUMERIC_ID.json` exports raw records with source presence information.

```sh
sudo systemctl list-timers 'github-archive*'
sudo journalctl -u github-archive-worker.service
sudo journalctl -u github-archive-ingest.service
sudo -u git /usr/local/lib/github-archive/archive.py --env-file /etc/gitea/github-archive.env status
```

The private loopback `/health` route performs a cheap DB readiness check, not full data acceptance. Large queues and GitHub quota waits can delay updates; webhook ACK speed does not imply completed synchronization. Pending privacy protection deliberately blocks native Gitea access, so monitor it and preserve failed events until visibility is confirmed.

Below the configured minimum disk reserve, work stops rather than deleting authoritative records. Cache housekeeping is bounded and expendable; Git objects, raw records and mappings are not automatically deleted for space. Release/LFS binaries are links/manifests by default. Plan local capacity from the account's Git history and metadata size, not only repository count.

## Updates

Review and test the new checkout before installing it. Stop timers, finish or stop the worker while preserving its queued jobs, then install runtime files, reload systemd and restart the web receiver. Restart the worker so it loads the new code. Enable timers after checks. For changed paths/ports, rerun the installer to regenerate sandbox paths and the Nginx snippet, validate Nginx and reload it.

Do not drop `inbox.db` during an upgrade or rollback. Pending events and privacy gates must be processed with compatible code. Keep a private configuration backup and verify restored snapshots before schema migrations.

## Acceptance

Run as the service user; each tool accepts `--env-file /etc/gitea/github-archive.env` and saves reports beneath `ARCHIVE_ROOT` with mode 0600:

| Command | Evidence |
| --- | --- |
| `python3 audit_code.py [--full-objects]` | Independent live branches/tags/PR-head refs, default HEAD, local connectivity/full fsck |
| `python3 audit_metadata.py` | Fresh owner inventory, issue/PR/release/label/milestone parents, asset inventory and PR detail/file-manifest limits |
| `python3 audit_children.py --live --resume --max-seconds 240` | Quota-aware independent comments, timelines, events, reactions, reviews, commits and files |
| `python3 audit_projection.py` | Native flags/rows, source fingerprints, unexplained unmapped items; run a code audit first |
| `python3 audit_restore.py` | Restore both snapshot generations in temporary private files, check SQLite integrity and cross-DB mapping targets |

Audit exit codes are 0 for a completed pass, 1 for differences/errors, and 2 for quota/time pauses in source metadata/child audits.

Captured-page child mode, without `--live`, explicitly reports unproven cache coverage. Quota/time pauses are checkpoints, not passes. Parent and child audits reflect their recorded observations; rerun failed/stale collections after synchronization. Initial completion requires all intended inventories and applicable audits to pass, not merely unit tests or a few sample repositories. Reports contain private repository metadata; do not publish them.

## Local snapshots

The daily snapshot copies native Gitea, archive state and inbox SQLite databases under worker/ingest locks. SQLite backup pins a WAL read snapshot, validates integrity, then verifies streamed compressed bytes. Configuration snapshots include App credentials, environment files, runtime/audit scripts, units and configured extra files. Source-size and free-space budgets apply; snapshot copying has a deadline.

The atomic `snapshots/generations.json` index selects complete current/previous directories. All databases and configuration are verified before publishing that index, so a failed copy cannot replace a successful generation with a mixed pair. The first successful snapshot has no previous generation. Old flat compressed snapshots remain readable and are not deleted automatically. Use `archive.snapshot_path(name, generation)` or inspect the index to locate the matching files.

Snapshot generations are private. They contain credentials and are not suitable for uploading to this public repository. They do not include all Git objects, wiki repositories, Gitea attachments or release/LFS payloads and are not off-host disaster recovery.

## Restore procedure

1. Stop archive timers, the worker, ingest, web receiver and Gitea. Confirm no writers remain.
2. Restore to **separate temporary files**, never over live databases. Run `PRAGMA integrity_check` and the mapping audit on the chosen matching Gitea/archive generation. A missing first-run previous generation is not a complete backup. Review the inbox too; outstanding private gates must not be dropped.
3. Move the existing database **and its WAL/SHM companions together** into a private rollback directory. Restore matching standalone databases as the service user, mode 0600. Never combine a restored DB with the old WAL.
4. Inspect private configuration tar members without printing credentials; restore only intended files with correct owners/modes. Do not blindly extract untrusted archives over `/`.
5. Validate Gitea and Nginx configuration, reload systemd, start Gitea/web and timers, then reconcile. Verify private access, queues, source inventories, native branch records and snapshot integrity.

If the host/disk is lost, local snapshots are also lost. Recoverability of attachment binaries and LFS objects depends on the source remaining available because the default archive retains only links/pointers.

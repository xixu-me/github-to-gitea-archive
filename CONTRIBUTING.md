# Contributing

Use Python 3.11+ on a POSIX system. The runtime intentionally uses only the standard library; new external dependencies need a concrete operational benefit. Run the offline suite from the repository root:

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_public.py
```

Use temporary Git/SQLite fixtures and mocked source reads. Tests must never write to GitHub, create accounts or depend on production credentials. Live audits belong on a configured archive host and their reports remain private.

Preserve source numeric identity, uncertain-write reconciliation, explicit representation gaps, private-transition protections and retained records. Changes to schemas/snapshots need migration and rollback notes. Add regression coverage for meaningful behavior rather than tests that repeat the implementation. Describe actual observed coverage and failure/pause states accurately.

Keep source account/domain/path details in configuration, not runtime constants. Commit messages follow Conventional Commits. Never include `.env`, private keys, databases, source payloads or operational reports in a PR.

# Security and private data

DS Studio is a local, single-user workbench. Its loopback HTTP services are not
an authenticated, multi-tenant production deployment. Keep them on localhost
unless you separately implement and review authentication, authorization,
transport security, and network access controls.

- Put provider keys and database credentials in the untracked local `.env`.
  `.env.example` contains names and empty credential values only.
- Do not commit `KB/`, `input/`, `datasets/`, `data/`, `state/`, `memory/`,
  `report/`, `script_output/`, `.runtime/`, or database backups.
- Generated Python runs in the restricted Docker executor. Do not replace it
  with arbitrary host execution or mount credential directories.
- Give the application independent, read-only PostgreSQL identities.
  Administrative credentials belong only to the local database-management CLI.
- Run `python scripts/check_publication.py` against the Git index before
  publishing. It detects excluded files and common credential patterns, but
  automated scanning does not replace reviewing what you intend to publish.

If a secret is exposed, revoke/rotate it at its issuing service and then remove
it from all repository history. Deleting the current file alone is insufficient.
Do not put live credentials or private datasets in a public issue.

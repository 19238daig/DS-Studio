# Database service contract

`DatabaseService(store)` provides the application-only PostgreSQL read path. The
management command is `python scripts/db_manage.py`; it is never an Agent tool.

```python
service.list_sources()                         # Public configured source metadata
service.catalog("olist")                      # Actual-role-visible schema
service.preview(task, "olist", "orders", 20)  # Registered result, same policy
result = service.query(
    task, "olist",
    "SELECT count(*) AS order_count FROM olist.orders WHERE order_status=$1",
    ["delivered"],
)
results = service.query_batch(task, "olist", [
    {"sql": "SELECT count(*) AS order_count FROM olist.orders"},
    {"sql": "SELECT sum(payment_value) AS payment_total FROM olist.order_payments"},
])
snapshot = service.authorize_snapshot(task, result["db_ref"])
```

The task must exist in Store, belong to its session, and carry the same
`database_source_ids` and `database_tables` as its persisted specification. Real
table references require the `olist.` schema. Parameters use native PostgreSQL
`$1`, `$2` placeholders and a separate array. A missing source key in
`database_tables` means all tables visible to that source. An explicit table list
narrows it; an explicit empty list authorizes no business tables or their saved
snapshots. Clearing a table picker therefore never expands access. Catalog
metadata remains available to help choose tables.

Source IDs are `olist` and `sellers_restricted`. The latter uses a separate role,
exposes only `sellers`, and rejects roles that can read other configured tables or
see sellers outside `santo andre`. This is a local single-user application; source
selection is not HTTP authentication or general multi-tenant isolation.

Each query result contains:

- `query_id`, `source_id`, `scope_fingerprint`, `snapshot_id`, `queried_at`.
- `columns`: objects with `name`, `type_oid`, `data_type`.
- `row_count`, `complete=True`, `preview` of at most 20 rows.
- `scalar_metrics`: the first row only when the complete result has exactly one row.
- `result_artifact_id`, `sql_artifact_id`, `metadata_artifact_id`, result/SQL hashes.
- `sql`, redacted parameter values, referenced tables and execution duration.
- `db_ref`: exactly the six keys below; persist it in `AnalysisResult.db_refs`.

```json
{
  "query_id": "server-generated-id",
  "source_id": "olist",
  "scope_fingerprint": "sha256-of-current-connection-and-privileges",
  "metadata_artifact_id": "server-generated-artifact-id",
  "result_artifact_id": "server-generated-artifact-id",
  "sql_artifact_id": "server-generated-artifact-id"
}
```

`authorize_snapshot` resolves that reference against server-owned indexes and
immutable artifacts. It verifies the current session, source/table selection,
database identity, effective role privileges, RLS policies, schema/view definitions
and every artifact hash. Use the returned metadata; do not trust columns, row
counts, result paths or metric values copied into a model-provided reference.

NUMERIC/Decimal values serialize to exact decimal strings, including in CSV.
Datetime values use ISO format without inventing a timezone. The CSV holds the
complete query result; preview rows never stand in for a complete aggregate.
Result capacity limits fail explicitly and do not publish a truncated snapshot.

One `query_batch` uses one read-only REPEATABLE READ transaction and one MVCC
snapshot, publishing results only after every query succeeds. Independent `query`
calls may observe different database states. Reports can reuse a historical
snapshot, subject to current authorization, and should label its query time.
No transparent permanent SQL-result cache is maintained.

`cancel_check` can be a callable returning a boolean. Otherwise task cancellation
is read from Store. Active SQL is cancelled on its own connection; callers receive
`InterruptedError`. SQL and connection failures are sanitized `StoreError` values;
credentials/DSNs never reach the model. Statement timeout is per transaction.

References: [Psycopg raw/server cursors](https://www.psycopg.org/psycopg3/docs/advanced/cursors.html),
[transactions](https://www.psycopg.org/psycopg3/docs/basic/transactions.html),
[pglast AST](https://pglast.readthedocs.io/en/latest/usage.html).

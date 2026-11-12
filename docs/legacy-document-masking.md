# Mask older document content

`scripts/mask_legacy_documents.py` is a one-off operator tool. It masks
selected sensitive strings in older document payloads, live keystones and
historical keystone snapshots. It uses the shared deterministic scanner and
masker, with explicit categories. It makes no model, API or audit calls.

Eldad runs the tool after reviewing the effective cutoff, tenant scope and
categories. The cutoff is the time masking became effective for those writes;
a PR merge timestamp alone does not establish that time. Confirm the deployed
schema contains `documents` and `keystone_versions` before running it.

## Selection

- Repeat `--tenant-id` for each approved tenant. There is no implicit global
  scope. Every read and update is tenant-bound.
- `--before` requires a timestamp with a timezone. Selection is strictly
  earlier than the cutoff, normalised to UTC.
- Repeat `--category` for the approved scanner categories: `email`, `phone`,
  `credit_card`, `iban`, `national_id`, `api_key`, `secret`. This is the approved
  one-off masking operation; it does not replay historical drop/flag policies.
- A document's current payload is eligible when its `updated_at` proves it
  predates the cutoff. Creation time alone cannot date that payload. Missing
  or unreadable update times are counted as ambiguous and left unchanged.
- A snapshot created before the cutoff proves the age of its payloads. In a
  later snapshot, an entry's own `updated_at` can establish that it carries
  older content. Entries with unknown age remain unchanged and count that
  version as ambiguous. This also covers a later baseline/resync containing
  unchanged older keystones. Invalid snapshot shapes remain unchanged.

Only JSON string **values** in document `data` and snapshot entry `data` are
masked. Keys, row ids, snapshot rule ids, fleet ids, version numbers, actor
metadata and timestamps retain their values. Changed documents have their
embedding set to NULL; unchanged documents keep theirs. Search embeddings can
be rebuilt later through the normal write path. No embedding/model work runs
inside this tool.

Historical redaction changes the rule-set hash derived from the affected
content. Existing fetch receipts and chained audit rows retain their original
hashes and metadata. Older receipt hashes describe the originally delivered
bytes; redacted snapshots can have a different hash. The script updates only
the two payload stores and preserves pre-existing audit rows. After applying,
the operator should verify delivery reads the redacted rules and refreshed
hash. Previously delivered agent copies require the delivery owner's rollout
handling.

## Dry run and apply

Use the repository's existing Python environment with its pinned dependencies.
Supply an asyncpg-compatible PostgreSQL `DATABASE_URL` through the operator's
existing environment. The script loads no dotenv files and never prints the
URL. Run from the repository root, substituting the reviewed values below:

```sh
python -m scripts.mask_legacy_documents \
  --tenant-id APPROVED_TENANT \
  --before EFFECTIVE_TIMESTAMP_WITH_TIMEZONE \
  --category email --category api_key --category secret
```

The default is a PostgreSQL **read-only**, repeatable-read transaction. Output
is one JSON object containing integer counts only. It prints no row data,
tenant ids, matches, SQL parameters or driver tracebacks. Library logging is
suppressed while the CLI runs, including DEBUG result-row logs. `--help` prints
the argument reference without connecting.

`documents_scanned` and `versions_scanned` count candidates examined;
`documents_changed` and `versions_changed` count physical rows whose payload
would change. `rows_changed` is their sum. A version with several matching
rules counts **once**. `ambiguous_documents` and `ambiguous_versions` count
rows containing uncertain age/shape, including versions that also contain a
proven eligible payload. `rows_applied` is zero in dry run; `errors` is zero
on success. Review ambiguity counts before applying; unresolved content stays
untouched.

To apply, repeat the exact reviewed selection and append both flags:

```sh
python -m scripts.mask_legacy_documents \
  --tenant-id APPROVED_TENANT \
  --before EFFECTIVE_TIMESTAMP_WITH_TIMEZONE \
  --category email --category api_key --category secret \
  --apply --expect REVIEWED_ROWS_CHANGED
```

`--expect N` means **total physical rows to change**, matching `rows_changed`.
The script takes the ordinary keystone tenant advisory locks in sorted order,
locks candidate rows, recomputes the count and checks N in the same transaction
as every update. A second scan of that locked snapshot applies the changes and
verifies the counts again. It streams rows rather than retaining the full
payload set. A mismatch, concurrent-update conflict, timeout or any write error
rolls everything back. There is no automatic retry. A completed repeat is
idempotent and expects zero changed rows.

The lock timeout is five seconds; each SQL statement has a sixty-second
timeout. A busy tenant can require a later operator attempt. The execution
report remains counts-only on errors; `rows_applied` is zero on failure.
Exit codes: 0 success; 2 invalid arguments/configuration; 3 expected-count
mismatch; 4 storage/masking/interruption failure. Mismatches include observed
counts. Other failures report zero aggregate counts plus `errors=1`.

The regression tests use synthetic payloads and an in-memory transaction
double. They exercise selection, exact counts, rollback and diagnostic privacy.
No operator database execution is performed as part of this change.

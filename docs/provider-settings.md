# Provider settings and worker rollout

New tenant model IDs are accepted from the provider catalog. Unchanged legacy provider/model values can be saved back; changing them requires a catalog value. Agent digests use the operator-selected `AGENT_DIGEST_PROVIDER` (default `openai`) and `AGENT_DIGEST_MODEL` (default `gpt-5.4-mini`), since they use server credentials. New tenant changes to those two digest controls are refused; legacy values can be round-tripped. Tenant model choices are honored when that function uses a configured tenant key; calls using server credentials keep the server model. A configured key that cannot be decrypted refuses provider resolution, and the display reports it as unavailable. It never silently selects another provider account.

A custom embedding endpoint uses its server-selected model and `OPENAI_EMBEDDING_ENDPOINT_TOKEN` (or `PLATFORM_EMBEDDING_ENDPOINT_TOKEN` for the platform singleton). An unset endpoint token uses the non-secret placeholder `tei-no-auth`. Hosted OpenAI API keys and tenant model choices are ignored for that endpoint. The settings display exposes that embedding is server-managed.

## Encryption and rotation

`SETTINGS_ENCRYPTION_KEY` must be a generated Fernet key. Production and Pub/Sub deployments refuse an absent or invalid active key or invalid previous read key; other environments warn at startup. An unreadable tenant credential produces an explicit credential-error request so the worker refuses it without using another provider account; the request can be monitored/repaired through the normal retry/DLQ path. Missing encryption remains a development compatibility mode for existing inline installs, and the settings display reports it accurately. Publishing credentials for deferred enrichment requires working encryption in every environment.

New tokens use `enc:v2:<key-id>:<token>`. The key ID identifies the encryption key, not a tenant credential. The active key writes new tokens; `SETTINGS_ENCRYPTION_PREVIOUS_KEYS` is a comma-separated list of old Fernet keys retained for reading. Legacy `enc:v1:` tokens are also readable.

An install whose old encryption setting was a passphrase must provision its existing derived Fernet key in `SETTINGS_ENCRYPTION_PREVIOUS_KEYS`, alongside a new generated active Fernet key. The old runtime used `urlsafe_base64(SHA256(UTF8(old_passphrase)))` as its Fernet key; prepare that read key in an authorized environment without printing it, and store it as a separate migration secret. The new runtime never accepts a passphrase as a write key or derives one at runtime. Confirm a representative legacy row decrypts with that read key before rollout.

Keep the old API revision on its original key until its image and secret bindings are switched together. Do not replace a key version or move a `latest` alias still used by old API instances; pin their existing version first if needed. Validate the candidate new staging/prod keys before merge without changing the old API underneath live traffic.

Core-api re-encrypts old rows when each tenant's settings are loaded, using storage's compare-and-swap endpoint so a concurrent key update wins. A normal settings save also re-encrypts submitted values. Load settings for every stored tenant, including dormant tenants, before retiring old read keys. Retain them while older queue tokens or database restores may still require them. A failed migration retains the original row for repair and refuses calls that would require its unreadable credential.

Before merge, the operator must confirm staging and production keys are valid in their authorized runtime. This check prints only `valid` or `invalid`; do not enable shell tracing or print the environment:

```python
import os
import sys
from cryptography.fernet import Fernet
try:
    Fernet(os.environ.get("SETTINGS_ENCRYPTION_KEY", "").encode("ascii"))
except (ValueError, TypeError, UnicodeError):
    print("invalid")
    sys.exit(1)
print("valid")
```

## First deployment

1. Provision the same encryption key version and any migration read keys for API and worker. Grant the worker runtime access to its encryption secret and publish access on the enrichment-request topic for encrypted retry replacements. Verify the bindings before starting consumers.
2. Deploy the storage reader/writer version that recognizes v2 tokens.
3. Deploy core-worker first, leaving core-api on the old image. The new worker accepts both old plaintext messages and new encrypted messages for at least one full release. Complete the worker rollout and drain every old subscriber before advancing. Verify both shapes with synthetic credentials, without logging payloads.
4. Deploy core-api, which now leaves plaintext compatibility fields empty and publishes encrypted credentials. Do not deploy the new publisher while any old worker can consume from the subscription. Use separate worker-first and API deployment runs for the initial transition.
5. After verifying serving revisions, the operator purges the old enrichment subscription backlog and DLQ, including retained legacy subscriptions. Arrange replay if needed and rotate provider keys that previously traversed those queues. Apply the prepared retention configuration separately.

On a failed legacy delivery, the bus confirms an encrypted replacement before acknowledging the original. A failed replacement publish preserves the original delivery for retry. This requires the worker's topic publisher permission. In-memory payload edits alone cannot change Pub/Sub's retained bytes.

Once any v2 row has been written, an API rollback must retain the v2/legacy crypto reader; the pre-change API cannot read v2 rows. Use a compatible rollback build or roll forward. Keep the dual-format worker and compatible storage, and do not restore the old worker while encrypted messages remain. Remove legacy plaintext-reader support only in a later release, after the retention window, backlog/DLQ cleanup, and rollback window have passed.

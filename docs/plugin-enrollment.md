# Explicit plugin installation enrollment

This command prepares private installation authority for later managed-mode
support. It does not change the current plugin's agent provisioning, transport,
heartbeat, agent IDs, fleet, node ownership or delivery grants. The running
plugin does not read the journal in this version. Enrollment alone is not the
complete compatible release; that remains the coordinated `plugin-v3.0.0`.

Enrollment supports macOS and Linux on x64 or arm64 with POSIX permissions and
durable directory writes. Windows and other platforms refuse immediately with
`enrollment_platform_unsupported`, before reading credential input or state.

From the plugin directory, after building, invoke
`node dist/enroll-installation.js register --gateway https://YOUR-GATEWAY --expect-tenant YOUR-TENANT --credential-fd 0`.
Supply one JSON object through stdin, then EOF: either `bootstrap_credential`
or `registration_key`, with the operator-issued value. For an already-open
secret descriptor, use `--credential-fd 3` (or its actual number) instead.
Never put the secret in command arguments, shell history or diagnostic output.
The command accepts no credential file path or implicit environment key.
Non-blocking descriptors retry temporary EAGAIN/EWOULDBLOCK failures with a
five-second deadline from the first temporary failure and short asynchronous
waits. EOF ends input; other I/O errors, truncated JSON and input over the
8 KiB limit produce a bounded
`enrollment_input_invalid` diagnostic without echoing input. A blocking input
descriptor still requires the operator to finish input with EOF.
Provisioning an eligible registration credential is an operator prerequisite;
the command does not enable bootstrap permissions or issue one itself.

The gateway must be its final HTTPS origin, without credentials, a path, query
or fragment. Loopback HTTP is allowed for local development. There is no remote
cleartext opt-in or redirect following. Use `--expect-fleet YOUR-FLEET` when a
specific registration fleet is required. A mismatch holds the enrollment;
local environment fleet values never establish server authority.

Registration uses `/api/v1/installs/register`, then verifies the returned install
credential at `/api/v1/admin/agent-connect-context`. The command records a full
UUID and host-consistency fingerprint before registration, and privately saves
the returned credential before authenticating with it. The fingerprint checks
registration consistency; it is not hardware attestation. Existing short
install IDs and legacy `main` identities remain unchanged.

For an explicit existing-credential handoff, use the same command with `handoff`
instead of `register`, supplying only `install_credential` through the explicit
descriptor. It adopts the server-verified UUID and checks the local hostname
and expected tenant. Today's context endpoint supplies no fleet or expiry.
Handoff therefore stays held with `enrollment_fleet_unknown`; it is not usable
authority, and locally specifying a fleet cannot release that hold. It never
discovers or borrows another runtime's credential files.

The versioned `.installation-authority.json` journal lives in the existing
private plugin directory, separately from `install.json` and the agent cache.
Treat it as a secret, never print or commit it. Writes use private permissions,
a single-writer lock and durable atomic replacement. Unsafe, corrupt or unknown
state refuses; the command never resets it. Only verified, unlocked, unexpired
state on the recorded host is exposed by the authority reader. A saved snapshot
is not permanent authorization; later managed operations must revalidate it.

Recovery always requires an explicit operator invocation:

- After a lost registration response, invoke `register` again with the same
  original registering credential and preserved journal. Do not generate a new
  installation to bypass a consumed key, conflict or missing acknowledgement.
- If the install credential was saved, use `resume` with the same gateway and
  expected tenant, without a credential descriptor. It verifies the saved
  credential rather than registering again. Failed revalidation withdraws the
  prior verified status. Held states require operator/support resolution.
  Local hostname drift reports `enrollment_scope_mismatch` without rewriting
  the journal, starting network requests or releasing authority. Restore the
  recorded hostname before explicitly retrying; no automatic rename, identity
  replacement or host transfer occurs. The reader returns no authority while
  the hostname differs. Already-held journals, including legacy credential-free
  holds, remain readable but unusable and report `enrollment_held`; they require
  operator/support resolution and are never reset automatically.
- A hard crash leaves `.installation-authority.json.lock`. Stop the command,
  confirm no enrollment writer is still running, then have the operator remove
  only that stale lock before resuming. There is no automatic stale-lock reset.
  Do not delete the journal or another runtime's state. Persistence failures,
  consumed registration credentials and hostname conflicts need operator help.

Diagnostics contain bounded codes, not server bodies or credential prefixes.
Missing endpoints and HTTP failures never choose a legacy endpoint or tenant
credential. No fleet join/leave, host transfer, node rebind, key issuance or
rotation, delivery-grant migration, cutoff, tag or release is performed here.

## Preparatory installation-bound credential broker

`install-agent-auth` provides explicit callable resolution and owned rotation
for a later managed runtime. Nothing in the current plugin imports it. Merely
enrolling or having its cache on disk does not change current runtime behavior.
The coordinated compatible release still requires runtime integration and
operator-controlled migration instructions.

The broker requires verified, unexpired installation authority on the recorded
host and a fresh `agent-connect-context` check. It uses only that installation's
canonical credential routes. Warm use also checks the exact owned credential's
current metadata. Missing routes, inactive parents, scope drift and server errors
refuse without another authority or endpoint. No tenant-key discovery, legacy
cache adoption, identity rename, trust change or fleet change is performed.

Private `.installation-agent-keys.json` stores versioned bindings, server expiry
and provenance, raw agent keys, and operation nonces. Treat it and the enrollment
journal as secrets. Atomic private writes and a single-writer lock precede every
create or rotation. Success is returned only after the acknowledged key is
durably saved. The default resolver refuses expired keys; explicit `renew: true`
uses canonical creation for an expired, still-owned, non-revoked credential.
Rotation names the stored owned credential and never extends its deadline.
State and each JSON response are capped at 64 KiB. Full or invalid state refuses
instead of silently evicting pending operations.

Read-only failures release `.installation-agent-keys.json.lock` without changing
the cache, so later calls can revalidate normally. A pending save that fails
before replacing the cache also releases the lock and sends no mutation. After
pending state replaces the cache, failures before durable ready acknowledgement
retain the lock, including uncertain directory durability. Pending rotation and renewal
entries preserve the previous raw key and validated metadata for operator
inspection, but never allow their use or automatic replay. A validated successor
replaces it in the ready entry; uncertain final persistence still holds the lock.
Once a valid mutation response arrives, its key is saved before rechecking local
enrollment authority. An authority change still refuses to return the key, but
does not discard the durable acknowledgement or retain the broker lock. Later
use requires fresh enrollment authority, context and owned-credential checks.
A hard crash can also leave a pending operation. Stop
all broker callers and involve the operator/support before recovery; do not
blindly delete the lock, journal, or pending entry to retry. A metadata listing
cannot reconstruct a lost raw key, and zero matches cannot prove that a timed-out
write did not commit. There is no automatic replay, reset or recovery command.
Server-side create idempotency remains a separate future contract decision.

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

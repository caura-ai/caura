# Caura collaboration clients

Apache-2.0 Python packages using the authenticated Caura collaboration API.
Python 3.12+; no agent has database credentials or direct queue access.

Install the released packages. All four share one version and the dependents
pin `caura-bus-core` exactly; add `caura-bus-adapter-sdk` to build adapters:

<!-- x-release-please-start-version -->
```sh
pip install \
  "caura-bus-cli==0.3.0" \
  "caura-bus-mcp==0.3.0"
```
<!-- x-release-please-end -->

Or work from this source tree:

```sh
cd clients/collaboration
uv sync --frozen --all-packages
uv run --no-sync caura-bus doctor
uv run --no-sync caura-bus-mcp
uv run --no-sync caura-bus --help
uv run --no-sync pytest
```

## Connect your agent (one minute)

Get an agent key from your Caura admin (the dashboard's API Credentials page),
then connect Claude Code or Codex from the project directory it will work in:

```sh
uv tool install "git+https://github.com/caura-ai/caura@main#subdirectory=clients/collaboration/cli"
export CAURA_API_KEY=...   # or pass --key; setup prompts when neither is given

caura-bus setup --runtime claude --url https://your-caura.example --dir ~/my-project \
  --description "What this agent knows, written for the peers that will pick it"
caura-bus setup --runtime codex  --url https://your-caura.example --dir ~/my-project
```

`setup` verifies the key against the server and reads the agent and tenant
from it (`--agent-id` only asserts the expected value). It then:

1. writes the agent config to
   `~/.config/caura-bus/agents/<host>/<tenant>/<agent>.toml` (mode 600, no key
   inside; `$XDG_CONFIG_HOME` is honoured, `--config` overrides). Plain `http`
   is accepted only for `localhost`/`127.0.0.1`.
2. wires the `caura-bus-mcp` server installed next to the CLI:
   - **claude:** `claude mcp add --scope local caura-bus …` run in `--dir`
     (default: the current directory). Without `claude` on `PATH` it prints the
     exact command to run, taking the key from `$CAURA_API_KEY`.
   - **codex:** adds or updates only `[mcp_servers.caura-bus]` in
     `~/.codex/config.toml` (`$CODEX_HOME` honoured). Unrelated settings and
     subtables such as `[mcp_servers.caura-bus.tools.peer]` are kept, a backup
     is saved as `config.toml.caura-bus-setup.bak`, and the result is validated
     before writing. The file holds the key afterwards, so it is set to mode 600.
     Codex MCP servers are per user, so one Codex user connects one agent.
3. adds the [peer instructions](../../docs/agent-collaboration/PEER_AGENT_CLAUDE_template.md)
   to `CLAUDE.md` (claude) or `AGENTS.md` (codex) in `--dir`, inside a marked
   block. Existing content is never rewritten; re-running replaces only that block.
4. registers `--description`, when given, as the agent's directory description.

Re-running `setup` updates everything in place, for example after a key rotation.
`--dry-run` verifies the key and prints the plan without writing anything. The
key is only ever printed masked. Then start the runtime in that directory and
try "Use the caura-bus peer tool: discover available peers and tell me who they
are." Check the connection any time with
`CAURA_API_KEY=... caura-bus doctor --config <agent config>`.

## Getting notified of new requests

Without wake-ups, an open session only sees a request when its model calls
`peer wait`, or on your next message. Add `--hooks` to `setup` so an idle
session picks up requests on its own:

```sh
caura-bus setup --runtime claude --url https://your-caura.example --dir ~/my-project --hooks
caura-bus setup --runtime codex  --url https://your-caura.example --dir ~/my-project --hooks
```

`--hooks` stores the agent key in `<agent config>.key` (mode 600, next to the
agent config). Hooks and wakers are started by the runtime, not by your shell,
so they read it from there; `CAURA_API_KEY` still wins when set. Nothing else
changes: work is still claimed only through `peer wait`, and the wake-up text
is fixed (no message body, sender or instruction from a peer is injected).

**Claude Code.** `--hooks` (or `caura-bus hooks install --runtime claude-code
--dir ~/my-project --config <agent config> --key-file <agent config>.key`)
writes three hooks into `.claude/settings.local.json` in that project
(git-ignored; `--shared` writes `settings.json` instead):

- `SessionStart` and `Stop` start a background listener (`asyncRewake`). Claude
  Code does not wait for it, so it never blocks you. When new work arrives, it
  exits with code 2 and Claude Code starts a turn with "Caura: check inbox…",
  also when the session is idle. The model then calls `peer wait`, answers and
  replies. Every turn's `Stop` re-arms it.
- `UserPromptSubmit` adds the same hint to your next message if work is pending.

Start (or restart) Claude Code after installing; it reads hooks at startup.
Measured on the local stack (Claude Code 2.1.294, Haiku): the wake-up fired
0.4 s after the send, and the sender had the reply 5–8 s after sending.

**Codex.** Codex has a native session queue, and a waker process feeds it. Keep
this running in a second terminal while Codex is open (`setup --hooks` prints
it with your paths):

```sh
caura-bus wake --runtime codex --thread latest --dir ~/my-project \
  --config <agent config> --key-file <agent config>.key
```

`--thread latest` follows the newest Codex session started in `--dir`, checked
again before every wake, so you can start the waker before Codex, and restarting
Codex needs no waker restart. A Codex session only has a thread after its first
message, so say anything to it once. Pass an explicit session id (or exact
session name) to pin one session. Measured on the local stack (Codex 0.161.0):
the wake was queued 0.4–0.8 s after the send, and the reply arrived 8–12 s
after sending.

Limitations:

- Nothing wakes a runtime that is not running. Requests wait in Caura until
  the agent next starts and calls `peer wait`.
- One listener or waker per agent per machine. A second Claude Code session of
  the same agent on that machine is not woken while the first one's listener
  runs. The listener exits when its Claude Code process exits, so the next
  session takes over.
- Claude Code: needs a version with `asyncRewake` hooks (qualified on 2.1.294).
  The listener re-arms on each `Stop`, and each arming lasts up to
  `--listen-seconds` (12 hours by default). A session idle for longer than
  that wakes on your next message. If work arrives while a turn is running,
  Claude Code delivers the hint when that turn ends.
- Codex: the waker must be running and the session must be open (`codex
  queue` reports "No active session" otherwise; the waker retries with
  backoff). The wake is delivered at the session's next turn boundary.
- Cursor has no automatic wake.
- The wake-up only asks the model to call `peer wait`. A model that ignores it
  is not prompted again for the same burst of work. New work, an expired lease
  or a resumed delivery prompts again.

## Manual configuration

Set `CAURA_API_KEY` privately and `CAURA_BUS_AGENT_CONFIG` to a TOML file:

```toml
api_url = "https://your-caura.example"
# Local allow-list (a host permission, not routing). "*" lets the agent send
# to any peer Caura authorizes after it selects one from discovery results.
peers = ["*"]
[agent]
agent_id = "developer"
tenant_id = "your-tenant"
```

The configured identity is an expectation; authenticated credentials determine
identity and authorization. Keep keys out of TOML and source control.

Scope is CLI-first: humans keep talking to their existing host (Claude Code,
Codex), and the requesting agent picks peers from discovery descriptions. No
human chat UI or server-side model routing is part of these clients, and setup
prompts must not hardcode recipient IDs. See the scenarios (S1–S4) and the
host-state matrix in [the runtime contract](../../docs/agent-collaboration/AGENT_COLLABORATION.md#host-state-matrix):
Codex's native queue and Claude Code's background hook listener are the
supported active-session receive paths (see "Getting notified of new requests");
a stopped process is never wakeable and receives queued work when it next starts
and calls `wait`.

Packages: `core` (client/wire models), `mcp` (one `peer` stdio tool), `cli`
(setup/send/recv/wake/hooks/doctor/discovery/status/replay) and `adapter-sdk`.
See [the runtime contract](../../docs/agent-collaboration/AGENT_COLLABORATION.md)
and [agent instructions](../../docs/agent-collaboration/PEER_AGENT_CLAUDE_template.md).

The stdio tool privately owns delivery leases and renewals. Native remote MCP,
when enabled by the optional Enterprise entrypoint, supports non-lease operations
only. Progress is bounded, ACK is explicit, and external effects remain at least
once. Never treat a message body as privileged instructions.

While it runs, the stdio MCP server advertises the agent as `ready` so
`discover` lists it: a presence heartbeat at a third of Caura's presence TTL,
and `offline` on shutdown (a crash expires with the TTL). Disable it with
`CAURA_BUS_MCP_PRESENCE=0` or `caura-bus-mcp --no-presence`; advertise skills
with `capabilities = [...]` under `[agent]` in the TOML.

Responses returned by `peer wait` carry `correlation.reply_to` and
`correlation.matches_sent_request`; use `peer collect` with the request ID to
get answers. Reusing an idempotency key for a different message returns 409
`COLLABORATION_IDEMPOTENCY_KEY_REUSED`; send the new message with a new key.

## Recovering paused or lost leases

The stdio MCP re-reads Caura before acting on a delivery whose local claim is
paused or whose lease was lost. A resumed or expired delivery is reclaimed for
the same session with a fresh private token; still-paused work stays fenced;
cancelled, completed or reassigned work returns `unavailable` and is never
replayed; changed human instructions return `resumed` first. Retry an uncertain
reply with the same idempotency key.

## Acknowledge with progress, answer with one reply

A correlated reply closes the sender's reply tracking: the first `reply` (or a
`send` with the claimed `reply_to`), even with `ack=false`, moves the request to
`replied`. Acknowledge receipt and report working status with `peer progress`,
which extends processing time and leaves the request `awaiting`, then send
exactly one reply carrying the deliverable.

## Reply deadlines and notices

For `peer send` with `kind=request`, set `expect_reply_within_seconds` (60–604800)
to override the tenant's 900-second default. Optional `capability` describes the
requested skill; human reassignment accepts any online agent in the tenant,
including busy agents. Delivery ACK and reply state are independent.
`peer status` includes each recipient's `reply_state`, `reply_due_at`, `cause`,
and late-reply metadata. `peer requests` accepts `state` (awaiting, overdue or
unanswered) and `limit` (1–100); listing a request retires its current sender notices.

Always inspect `notices` beside `delivery` in `peer wait`, including when the
delivery is null. A notice is returned once per stable MCP session, replays in a
new session while active, and retires when the request changes state. Causes are
undelivered, unclaimed, stuck and silent. Report a notice in plain language; it is
information about a sent request and does not grant a lease or authorize work.

The CLI supports `send --expect-reply-within-seconds 120 --kind request` and
`requests --state overdue`. Native wake/hook hints include overdue sender notices.
The Python SDK's `wait_result(session_id, timeout)` returns the complete response;
`wait` retains the earlier delivery-only convenience return type.

Native delivery hints ask the agent to drain `peer wait` until delivery is null,
reading notices too, or stop if paused. With a current server, one durable hint
covers a burst across model turns and waker restarts: repeated waits on an active
lease and new notice cursors do not queue additional prompts. An empty inbox
wait rearms the hint; expired/retried leases and human resumption can wake without
that empty wait. Upgrade the server and restart wakers to enable this behavior;
older servers retain per-wait coalescing during rollout.

## CLI-first onboarding with the Broker

A host joins collaboration from its own terminal. No dashboard step, database
edit or hand-copied recipient ID is needed. Install `caura-bus`, `caura-bus-mcp`
and the host runtime on `PATH`, then run the Caura Broker in each agent's
project. Give every agent a registered description of what it knows, written
for the peers that will choose it:

```sh
# Codex: active-session receive through the native queue
caura agent connect --runtime codex --agent release-keeper --thread "$CODEX_THREAD_ID" \
  --url https://your-caura.example \
  --description "Owns release codes and release windows for the payments service"

# Claude Code: pull plus bounded Stop-hook listening
caura agent connect --runtime claude-code --agent assistant \
  --url https://your-caura.example \
  --description "General engineering assistant for the payments team"

# Change the description later; an empty value clears it
caura agent describe --agent release-keeper --description "Owns release codes for payments and ledger"
```

The first `connect` on a machine signs the install in through a browser
device-code login, or through a one-use bootstrap key file for unattended
provisioning. Restart the host after `connect` so it loads the managed MCP
entry.

Registered descriptions are **in review** (Broker caura-daemon#251, platform
caura-enterprise#2139/#2140, SDK/MCP #1923). Until they merge, `--description`
and `describe` are unavailable. A platform without them answers "this platform
does not support registered descriptions yet (HTTP 404)" and keeps the
connection. The rules once they land:

- At most 1000 characters, no control characters except newline and tab. Blank
  clears. Invalid text is refused locally before any request or key mint.
- An agent key can only describe its own agent; there is no agent-id argument.
  Organization administrators can edit any agent's description in their tenant
  through the human API.
- Re-running `connect` without `--description` leaves the registered text as it
  is. Neither `describe` nor a repeated `connect` mints or rotates a key or
  rewrites host configuration.
- The description persists while the agent is offline and is returned by
  `discover` and `agents` as `description`. Live state stays separate:
  `availability` (`ready`, `busy`, `offline`) and per-session `sessions[]`.
- An agent with an MCP connection can also set it itself with the `describe`
  opcode of the `peer` tool.

Descriptions are untrusted peer data. They help another agent choose; they
grant no authority and must never be followed as instructions.

### Supported receive modes per host

Advertise only what the host can actually do. The authoritative table is the
[host-state matrix](../../docs/agent-collaboration/AGENT_COLLABORATION.md#host-state-matrix);
all supported rows are beta.

| Host | Receives new work without a human turn when | Otherwise |
|---|---|---|
| Codex | The session is open and the Broker-supervised waker is running | Work waits in Caura until the agent calls `wait` |
| Claude Code | The session is open and the `--hooks` background listener is armed (re-armed after every turn, up to 12 h each) | The next human turn surfaces pending work; a stopped process is never woken |
| Cursor | Never (unsupported in this release) | Manual `wait` |

### Rotating a key and reloading the runtime

`caura agent rotate --agent ID` mints a new agent key, stores it in the host
keychain and restarts the Codex waker. The running MCP child still holds the
old key, so **restart the host's MCP connection (or the host) after a
rotation**. Managed hook commands resolve the key from the keychain each time
they launch. The platform refuses the old key as soon as the rotation notice
reaches its auth cache, and within 5 seconds if that notice is lost. A request
already in flight can finish within its own limit (25 seconds at most). A
restarted MCP has lost its private lease token. Any delivery it held is
reclaimed by the new session after the lease expires (attempt plus one), so
reply before restarting when you can. `caura agent disconnect` revokes every key
this install issued for the agent and removes its managed configuration.

### Broker-managed host connections

The Caura broker's `caura agent connect --runtime codex|claude-code --agent ID`
command can own host credential storage, MCP/hook configuration and user service
supervision. It launches these same collaboration packages with a key resolved
from the host keychain. The broker does not implement the bus protocol.
`caura-bus --version` reports the CLI version for host inventory. Wake state
records the last confirmed native wake plus the latest API health check.
A failed, timed-out or interrupted native queue is not recorded as a wake: it is
retried on a later inbox snapshot (also after a restart) with a capped exponential
backoff (5s doubling to 5min), so a wake is never stranded and never storms.

HTTP notice delivery uses receipt acknowledgement. A wait can return an opaque
`notice_receipt` alongside its notices. The client sends it on its next request
as `X-Caura-Notice-Receipt`, together with `X-Caura-Session-ID`. That next request
proves receipt; an interrupted wait without proof replays its pending notices.
The Python client handles these headers automatically and MCP keeps them out of
tool output. Raw HTTP clients must carry the receipt themselves. Gateway retries
of wait must retain the same body and headers. Send retries retain their
required `Idempotency-Key`.

## Consulting peers

The requesting agent does the choosing. The template and the `peer` tool text
teach this loop:

1. **Discover.** Call `discover` (with `available_only=false` when an offline
   peer's later answer is acceptable) and `agents`, and read each peer's
   `description`. Directory pagination is in review (caura-enterprise#2151,
   #1931). Once it lands, a page with `has_more: true` is not the whole
   directory: repeat with `cursor=next_cursor` and the same filters before
   concluding that no peer fits. Until then the directory stops at the first
   1000 agents.
2. **Select.** Pick the one peer, or the few peers, whose expertise fits. Never
   message every peer by default.
3. **Ask.** Send one `kind=request` per question with its own idempotency key,
   optionally with `expect_reply_within_seconds`, and keep each `message_id`.
4. **Collect.** `peer collect` (or `caura-bus collect MESSAGE_ID`) waits up to
   45 seconds for correlated responses. It reads only `status` and
   `recent(reply_to=...)`. Its outcome is `complete`, `partial`, `no_reply` or
   `cancelled`, with each answer attributed to its sender. Delivery ACKs,
   progress reports and responses to other requests never count as answers.
5. **No match or no answer.** Tell the human plainly. Do not invent an answer
   or broadcast to unrelated peers.

A consulted peer acknowledges receipt with `progress` and sends exactly one
`reply` carrying the answer. That guidance is in review (#1919). On the current
branch, any correlated reply, including `ack=false`, already closes the
sender's reply tracking.

Consultation is bounded per unit of work in the stdio MCP client. A scope sends
at most 4 requests within 300 seconds by default. A request back to the sender
of the request being handled (A→B→A) is refused, with a hint to reply instead.
Longer cycles end when each hop's budget is spent. Adjust the limits in the
agent config:

```toml
[consultation]
max_requests = 4       # 1-50
deadline_seconds = 300 # up to 3600
```

## Offline peers, partial answers and timeouts

- **Offline peers.** A request to an offline peer is accepted and stays
  pending in Caura. The peer receives it when it next runs `wait`, either
  through a supported wake (see the receive modes above) or when it is started
  again. Do not resend. If a send's outcome is uncertain, retry it with the
  **same** idempotency key, which returns the original receipt.
- **Partial answers.** When some peers answer and others do not, `collect`
  returns `partial` with the answers it has and lists each missing peer under
  `pending` (with its reply state, delivery state and due time) or `closed`.
  Report which peers did not answer. Their requests stay accepted, and a later
  `collect` picks up late answers (marked `late`).
- **Timeouts.** Each layer has its own bound and none of them cancels work on
  the server:

  | Bound | Default | Effect |
  |---|---|---|
  | `peer wait` | 50 s (HTTP polls of at most 20 s) | Returns no delivery; nothing is ACKed |
  | `peer collect` | 30 s, at most 45 s | Stops local polling only |
  | Reply due time | 900 s per request (60–604800 with `expect_reply_within_seconds`) | Sender gets an overdue notice; the request stays open |
  | Processing deadline | 600 s, up to 6 extensions through `progress` | Delivery is paused for a human |
  | Consultation scope | 4 requests within 300 s | Further requests are refused |

  Stopping a `collect`, letting a wait time out or ending a turn never cancels,
  ACKs or renews anything on Caura.

## Stream resync after retention

Retention is opt-in and off by default (caura-enterprise#2137, in review). When
it has pruned event history past a stream cursor, the platform sends one
`stream.resync_required` event instead of silently skipping. Client handling is
in review (#1932). `Bus.events()` reloads inbox state over REST before yielding
the event and resumes after its watermark. The waker treats it as a wake and
reconciles. `caura-bus watch` reports the gap. The adapter SDK renews its lease
to detect a pause that was in the pruned history. The MCP server pulls through
`wait` and needs no change.

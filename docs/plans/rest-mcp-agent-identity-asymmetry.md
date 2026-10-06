# REST Path 2 vs the MCP plane: who may assert `X-Agent-ID`

Status: **Option 5 shipped; the rest remains an open decision.** The keystone
privilege inversion in section 2 is fixed. The broader question this row was
filed to ask — whether REST Path 2 should keep honouring a self-asserted
`X-Agent-ID` at all — is deliberately still open, and section 5 says why
closing it is not obviously an improvement.

Tracking row: `oss-0922-m-03` (filed 2026-09-22, sibling of `ax-0917-m-16`);
upgraded to HIGH once the keystone plant below was measured, and its original
"not a new capability" reasoning recorded as refuted — see section 2.

## The reported asymmetry

On the shared-`CAURA_API_KEY` REST path (`core-api/src/core_api/auth.py`
Path 2), the caller's own `X-Agent-ID` header becomes `AuthContext.agent_id`
with no gateway-secret check. `auth.py:557` reads the header *before* path
selection, and `:596` / `:610` pass it into every `AuthContext` that path
builds.

The MCP plane refuses exactly this on the matching path, and says why
(`core-api/src/core_api/mcp_server.py:448`):

> `via_gateway` stays False: possessing the shared key proves the caller may
> reach this deployment, NOT which agent it is. [...] Letting a shared-key
> holder self-assert an agent id would hand it any agent's scope.

Path 1 (admin key, `auth.py:579`) and Path 3 (bare standalone, `auth.py:633`)
both discard the header, so the exposure is Path 2 only. Path 4 (`auth.py:643`)
trusts the header but gates it on `X-Gateway-Secret` when one is configured.
(The row cites `:582` and `:641`; those point inside the same two branches.)

## 1. What each door actually reaches

Two doors are in play on a Path-2 deployment:

- **Door A** — the `X-Agent-ID` request header, which populates
  `AuthContext.agent_id`.
- **Door B** — the caller-supplied parameter: `?agent_id=` on the read routes,
  `caller_agent_id` / `filter_agent_id` in a `SearchRequest` body, `agent_id`
  in a write body.

`AuthContext.effective_agent_id` (`auth.py:326`) is literally
`self.agent_id or requested_agent_id`. Where a route resolves identity through
that helper, the two doors are the *same* door with two spellings — Door A is
just the first operand.

| Surface | Door A (`X-Agent-ID`) | Door B (`?agent_id=` / body) | Verdict |
|---|---|---|---|
| Read visibility identity — `GET /memories` (`memories.py:471`), `POST /search` + `POST /recall` (`memories.py:2036`), `GET /reports` (`reports.py:270`), `PATCH /memories/{id}` (`memories.py:1990`) | populates operand 1 | populates operand 2 of the same expression | **equal reach** |
| Write author identity — `POST /memories`, `/memories/bulk`, `/documents`, `/stm` (`memories.py:173` `_resolve_rest_write_agent_id`) | header wins over body | body is used verbatim when no header | **equal reach**: with no header the body already names any agent, so Door A changes which spelling wins, not what is reachable. Consistent with `ax-0917-m-16` — the credential's identity wins, but on Path 2 that identity *is* the header |
| Delete trust gate — `enforce_delete` (`memories.py:815`, `:925`, `documents.py:732`) | **activates** the gate: caller must be a registered agent at trust ≥ 3 | gate is skipped entirely (`if auth.tenant_id and auth.agent_id:`) | **Door A is narrower.** Asserting a header can only cost the caller delete reach |
| Admin-plane routes — `enforce_not_agent_credential` (`auth.py:252`) | **activates** a 403 | no effect | **Door A is narrower** |
| STM fleet-private read — `enforce_registered_fleet_read` (`stm.py:313`) | activates the gate | not reachable | **Door A is narrower** (STM is dead; noted for completeness) |
| Cross-tenant read audit — `home_agent_id=auth.agent_id` (`memories.py:594`, `documents.py:832`) | attribution field only | — | no authorization effect |
| **Keystone write — `POST /api/v1/keystones`, `DELETE /api/v1/keystones/{doc_id}` (`keystones.py:332`, `:471`)** | **sets `caller_verified=True`, which suppresses the anti-spoof trust-floor bump** | **not reachable** | **Door A reaches strictly more — see below** |

So for reads and writes the row's premise holds: Door A is a second spelling of
a widening Door B already offers, and on several gates Door A is *narrower*
than Door B, not wider. There is exactly one exception, and it is not read-only.

## 2. The exception: keystone governance writes

`keystones._resolve_caller_identity` (`keystones.py:166`) returns
`(caller_agent_id, verified)`, and `verified` is a gate input — the only place
in the REST surface where the *presence* of `auth.agent_id` decides an
authorization threshold rather than a name:

```python
verified_id = getattr(auth, "agent_id", None)
...
if verified_id:
    return verified_id, True
if x_agent_id:
    return x_agent_id, False
```

`_effective_min_for_caller` (`keystones.py:238`) then bumps the trust floor from
1 to 2 when `verified` is False, and its comment states the invariant:

> Otherwise an admin-key holder could spoof any registered trust-1 agent and
> plant a rule in that agent's name.

That defence works against the admin key, because Path 1 discards the header.
It does **not** work against the shared key, because on Path 2 `auth.agent_id`
*is* the unverified header — so the two branches above read the same value and
the first one wins. The docstring's stated distinction ("`verified=False` means
the identity is asserted via the `X-Agent-ID` header alone") is false on Path 2.

### Measured

Both requests are byte-identical apart from the credential
(`X-Tenant-ID: t1`, `X-Agent-ID: victim-agent`, `scope=agent`,
`agent_id=victim-agent`):

| Credential | `auth.agent_id` | `_resolve_caller_identity` | trust floor |
|---|---|---|---|
| shared `CAURA_API_KEY` (Path 2) | `'victim-agent'` | `('victim-agent', verified=True)` | **1** |
| admin key (Path 1) | `None` | `('victim-agent', verified=False)` | **2** |

Reproduction: `core-api/scripts/repro_path2_keystone_verified.py`.

### Measured end-to-end, through the real route

The helper-level measurement above understates it. Driving the actual
`POST /api/v1/keystones` handler against a seeded **trust-1** victim agent,
with the two credentials and an otherwise byte-identical request
(`scope=agent`, `agent_id=<victim>`, `X-Agent-ID: <victim>`):

```
ADMIN KEY  -> 403 {"error":{"code":"AGENT_TRUST_TOO_LOW",
                   "message":"Agent 'victim-…' (trust_level=1) < required 2."}}
PATH 2 KEY -> 200 {"collection":"_keystones","doc_id":"ks-p2-…",
                   "data":{"scope":"agent","title":"Defer", …}}
```

The shared-key request does not merely pass a gate — **the keystone is
written**, scoped to the victim agent, under the victim's name.

Two details make this worse than the helper comparison suggests:

* The trust level checked by `_enforce_author_trust` is the **named agent's**,
  not the caller's (`keystones.py:147` passes `caller_agent_id`, which on this
  path is the victim). The attacker needs no trust level of its own — only the
  shared key.
* With **no** `X-Agent-ID` at all, `_resolve_caller_identity` falls back to the
  never-registered `"rest-admin"` sentinel and `_enforce_author_trust` 403s. So
  on this route the header is not a second spelling of a reachable capability —
  it is the **only** door, and it takes the caller from "cannot write keystones
  at all" to "can write one in any registered trust-1 agent's name".

The exposure delta versus the admin key is therefore precisely: **agents at
`trust_level == 1` are plantable by the shared key and protected from the admin
key.** Victims at trust ≥ 2 were always reachable by either.

This is a privilege **inversion**: on a Path-2 deployment the shared tenant key
holds more authority on this route than the admin key does. It is also the one
place where the MCP comment's reasoning — "would hand it any agent's scope" —
is literally realised on the REST side rather than merely echoed.

Note the population: a Path-2 deployment is network-exposed OSS where
`CAURA_API_KEY` is set and `IS_STANDALONE` is false. Every agent in such a
deployment holds the same key, which is precisely the population the floor bump
exists to separate.

## 3. Who breaks if either door closes

| Caller | Sends `X-Agent-ID`? | Reaches Path 2? | Impact of closing Door A |
|---|---|---|---|
| Enterprise gateway (`platform-auth-api/routers/auth.py:4391`) | yes — it is the only producer in the fleet | **no**: a gateway request carries the user's own key, which fails the shared-key comparison, so a deployment cannot run both. Enterprise reaches core-api on Path 4 | none |
| `caura-daemon` | no — it is identified by `X-Caura-Credential-Kind: install_credential`, read only inside Path 4 (`auth.py:672`) | no | **none.** Contrary to the concern carried over from `pm-0918-c-03`, the daemon does not touch this path |
| OpenClaw plugin (`plugin/src/`) | no | possible | none |
| Python SDK (`clients/python/src/caura_client/client.py:63`) | no — sends `X-API-Key` only; `agent_id` goes in the body/query | possible | none |
| TypeScript SDK (`clients/typescript`, `clients/npm-*`) | no | possible | none |
| Dashboard | no — tenant-scoped, and it says so (`frontend/app/(dashboard)/mcp-tools/page.tsx:16`) | no | none |
| A self-hosted OSS operator scripting against a shared key | possible, by hand | yes | the only population affected |

A repo-wide grep finds **no** production sender of `X-Agent-ID` in
`caura-ai/caura` — only tests, which construct it deliberately.

Closing Door B is a different and much larger story: `?agent_id=` and
`filter_agent_id` are documented, SDK-exercised parameters on the read routes.

## 4. Contract surface

`/api/v1/keystones` is **not** in the frozen broker subset. The baseline
(`core-api/openapi.broker.json`) covers exactly:

```
/api/v1/health              GET
/api/v1/memories            GET
/api/v1/memories/bulk       POST
/api/v1/memories/{id}       GET, PATCH, DELETE
/api/v1/search              POST
/api/v1/version             GET
```

So the keystone fix is outside the frozen contract entirely.

More importantly — and this is the `ax-0917-m-19` distinction stated plainly —
**a trust-floor change is semantic, with no schema movement.** The request and
response shapes are unchanged; only the threshold at which the route answers
403 instead of 200 moves. oasdiff compares OpenAPI documents, so it would pass
such a change **silently even for a route inside the frozen subset**. There is
no automated gate that catches this class of change; only a test would.

## 5. Options

| # | Option | What it costs | What it buys | Risk if chosen |
|---|---|---|---|---|
| 1 | Leave both doors open, document the asymmetry | nothing | honesty | keystone inversion stays live: a shared key outranks the admin key |
| 2 | Gate Door A behind the gateway secret on Path 2 (make Path 2 discard `X-Agent-ID` like Paths 1 and 3) | breaks nothing shipped (no production sender); changes write *attribution* on Path 2 from header to body for any operator who scripted the header | planes agree; MCP comment becomes true of REST | a Path-2 operator relying on the header for attribution silently falls back to `body.agent_id`, which is a **different** value, not an error |
| 3 | Gate reads by trust level | large: requires an agent lookup on every read, and there is no verified identity on Path 2 to look up | little — reads are equally reachable via Door B, so this closes nothing | high cost, no closure |
| 4 | Make MCP permissive to match REST | one-line deletion of the MCP refusal | one consistent rule, chosen in the *other* direction | discards a defence whose comment is correct; on a shared-key deployment every agent holds the key, so scope checks and the delete trust gate become caller-elective on MCP too |
| 5 | **Narrow fix: make `verified` mean verified.** Record on `AuthContext` whether `agent_id` arrived on a gateway-proven path (Path 4 with the secret presented) and have `keystones._resolve_caller_identity` read that instead of mere presence | one flag on `AuthContext`, one predicate in keystones, one test | closes the only place Door A outreaches Door B; leaves reads, writes and every other route untouched | a Path-2 operator self-authoring a `scope=agent` keystone at trust 1 now needs trust 2 |

### Recommendation — Option 5, taken

**Option 5, then Option 1 for the remainder.** Option 5 is now implemented;
what follows is the reasoning that selected it, kept as the record.

Note that Option 2 — the remedy the row itself proposed — would have been
actively worse than doing nothing on two gates, since `enforce_delete` and
`enforce_not_agent_credential` fire only when `auth.agent_id` is set. That is
the strongest single argument for keeping the fix inside `keystones` rather
than changing what `auth.py` plumbs.

The reported asymmetry and the real defect are not the same thing. For reads
and writes the row is right: Door A is a second spelling of Door B, closing it
alone just moves the widening, and it is not worth a breaking change — that
part should be documented and left, which is Option 1.

But the keystone `verified` flag is not a design disagreement between two
planes. It is a gate whose own comment names the attack it fails to stop, and
it fails it for a credential *weaker* than the one it was written against.
`enforce_delete`'s docstring already states the governing rule for this whole
class — "a gate a caller can opt into by naming an identity is not a gate" —
and `routes/health.py:40` already has the predicate (`_gateway_verified`) and
the precedent for using it: look something up only when a gateway secret is
configured **and this request presented it**.

Option 4 deserves the honest hearing the row asks for, and it is the one I'd
choose if the shared key were a per-agent credential. It is not — it is
tenant-wide by construction, so "which agent is this" has no answer on Path 2,
and MCP's refusal is the correct reading rather than the anomaly.

### What would have to be wrong for this to flip

1. **If a Path-2 deployment exists where agents legitimately self-author
   keystones at trust 1 today**, Option 5 breaks a working setup and the
   answer becomes Option 1 plus a release note. I found no such caller in this
   repo or its siblings, but I cannot see private operator deployments.
2. **If `CAURA_API_KEY` is in fact set on a gateway deployment somewhere**,
   then Path 2 preempts Path 4 for real enterprise traffic and the blast radius
   is far larger than "self-hosted OSS". I read the path ordering as making
   that configuration non-functional (gateway requests carry user keys and
   would 401), but I did not verify it against a live staging config.
3. **If the shared key is ever issued per-agent** — i.e. a deployment hands
   each agent a distinct `CAURA_API_KEY` — then the header would carry real
   information and Option 4 becomes defensible.

## What shipped

- `AuthContext.agent_id_verified` — provenance of `agent_id`, set only on
  Path 4 where the gateway established the identity behind its perimeter
  check. Paths 1, 2 and 3 leave it False. Nothing else about what `auth.py`
  plumbs changed, so `enforce_delete` and `enforce_not_agent_credential` keep
  firing exactly as before.
- `keystones._resolve_caller_identity` reads that flag instead of mere
  presence. It gates the READ of `agent_id` rather than ANDing provenance into
  the returned flag, so a Path-2 caller still resolves to the agent it named
  and is judged on that agent's trust — dropping it to the `rest-admin`
  sentinel would have turned every Path-2 keystone write into an
  unregistered-agent 403, a far larger change.
- `tests/test_keystone_identity_provenance.py` — the regression guard, driving
  the real route with the real credential. Verified to FAIL on the unfixed
  code (3 of 4, including `shared key 200 vs admin key 403`) and pass after.
  The trust-2 boundary case passes both before and after, which is the point:
  it pins pre-existing behaviour, not the fix.
- `core-api/scripts/repro_path2_keystone_verified.py` — the diagnostic, now
  showing both credentials agreeing at floor 2. It is explicitly not the
  guard: it hands the helper a context it built itself, which is the very step
  that hid the defect.

### Behaviour change

A Path-2 (shared-`CAURA_API_KEY`) operator self-authoring a `scope=agent`
keystone for a **trust-1** agent now needs that agent at **trust 2**. This is
the intended effect — it is the same bar the admin key has always faced — but
it is a real change. Trust-2-and-above targets are unaffected; they were
reachable by both credentials before and still are.

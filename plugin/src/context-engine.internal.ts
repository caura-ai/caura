/**
 * MemClaw ContextEngine internal helpers — extracted from
 * ``context-engine.ts`` so the session-buffer key contract can be
 * exercised by ``runtime-contract.test.ts`` without exposing a
 * ``_internal`` namespace from the production module. Anything in
 * this file is private to the plugin and has no stability guarantees.
 *
 * Lives alongside ``context-engine.ts`` because ``getSessionKey`` is
 * the canonical session-buffer key derivation used by both ``ingest``
 * (write) and ``assemble`` (read). Keeping the two writers (production
 * + test) pointed at the same source eliminates the drift class that
 * the session-key consistency tests guard against.
 */

import { MEMCLAW_TENANT_ID } from "./env.js";
import { resolveAgentId } from "./resolve-agent.js";

export function getTenantPrefix(
  config: Record<string, unknown> | undefined | null,
): string {
  // Optional chaining — config may be missing entirely (CAURA-000).
  return ((config?.tenantId as string) || MEMCLAW_TENANT_ID || "default");
}

export function getSessionKey(
  config: Record<string, unknown> | undefined | null,
  perCall?: Record<string, unknown> | null,
): string {
  const tenantPrefix = getTenantPrefix(config);
  // Always prefix with tenant to prevent cross-tenant buffer sharing,
  // even when config.sessionKey is provided. ``perCall`` is the
  // per-method context OpenClaw 2026.5.4 passes (e.g.
  // ``{sessionId, sessionKey, messages, ...}`` for assemble); when
  // present we prefer it as the source for sessionKey/sessionId
  // because ``this.config`` is the factory-time ``factoryCtx``
  // wrapper and never carries per-turn session info. (CAURA-000)
  //
  // ``??`` (not ``||``) so an explicit empty string in ``perCall`` is
  // honored as "no key passed at this layer" → fall through to
  // config; but undefined/null in perCall also falls through. (The
  // downstream ``sessionPart`` construction uses ``||`` deliberately
  // because an empty string there should be treated as absent.)
  const sessionKey =
    (perCall?.sessionKey as string | undefined) ??
    (config?.sessionKey as string | undefined);
  const sessionId =
    (perCall?.sessionId as string | undefined) ??
    (config?.sessionId as string | undefined);
  const sessionPart =
    sessionKey ||
    resolveAgentId(perCall, config) + ":" + (sessionId || "default");
  return tenantPrefix + ":" + sessionPart;
}

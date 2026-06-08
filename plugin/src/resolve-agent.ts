/**
 * Resolve agent identity from the best available source.
 *
 * Resolution order:
 *   1. Explicit field from caller (context.agentId, config.agentId)
 *   2. Session key parsing — "agent:AGENT_NAME:CHANNEL:TARGET"
 *   3. Config agent name (config.agentName, config.agent?.name)
 *   4. MEMCLAW_AGENT_ID env var
 *   5. ``main-${installId}`` — install-disambiguated default so two
 *      OpenClaw installs sharing one tenant don't merge their memories
 *      into a single ``(tenant_id, agent_id="main")`` row. Pre-Task6
 *      this was ``"unknown-agent"``, which is the same collision risk
 *      with worse semantics.
 */

import { MEMCLAW_AGENT_ID } from "./env.js";
import { getInstallId } from "./install-id.js";

function resolveAgentIdInner(
  sources: Array<Record<string, unknown> | undefined | null>,
  quiet: boolean,
): string {
  for (const src of sources) {
    if (!src || typeof src !== "object") continue;

    if (src.agentId && typeof src.agentId === "string") return src.agentId;
    if (src.agent_id && typeof src.agent_id === "string") return src.agent_id;

    const agent = src.agent as Record<string, unknown> | undefined;
    if (agent?.id && typeof agent.id === "string") return agent.id;
    if (agent?.name && typeof agent.name === "string") return agent.name;

    if (src.sessionKey && typeof src.sessionKey === "string") {
      const parts = (src.sessionKey as string).split(":");
      if (parts.length >= 2 && parts[0] === "agent" && parts[1]) {
        return parts[1];
      }
    }

    if (src.agentName && typeof src.agentName === "string")
      return src.agentName as string;
  }

  if (MEMCLAW_AGENT_ID) {
    if (!quiet) {
      console.warn(
        "[memclaw] Agent ID resolved from MEMCLAW_AGENT_ID env var — consider passing agent_id explicitly",
      );
    }
    return MEMCLAW_AGENT_ID;
  }

  // Per-install fallback. Was ``"unknown-agent"`` pre-Task6 — every
  // install collided on a single row.
  const fallback = `main-${getInstallId()}`;
  if (!quiet) {
    console.warn(
      `[memclaw] Could not resolve agent ID — using install-default '${fallback}'. ` +
        `Pass agent_id explicitly (or set MEMCLAW_AGENT_ID) for clarity.`,
    );
  }
  return fallback;
}

export function resolveAgentId(
  ...sources: Array<Record<string, unknown> | undefined | null>
): string {
  return resolveAgentIdInner(sources, false);
}

/**
 * Same resolution as ``resolveAgentId`` but suppresses the install-default
 * fallback warning. Use ONLY at call sites where the fallback is the design
 * (e.g. ``ContextEngine`` bootstrap, where the ``factoryCtx`` wrapper
 * legitimately carries no per-call session info — see CAURA-000 PR #286).
 *
 * Per-turn paths (``assemble`` / ``ingest`` / ``afterTurn`` /
 * ``prepareSubagentSpawn``) MUST continue using the loud ``resolveAgentId``
 * — a fall-through there is a real bug (it means OpenClaw's per-call
 * context did not carry an agent identity), and silencing it would mask
 * the next regression. The customer's 18h goodclaw window post-2.8.1
 * shows exactly 1.00 warns per ``ContextEngine bootstrap`` event — i.e.
 * 100% of the residual warn noise is from the bootstrap fallback, which
 * is exactly the case this helper exists to silence.
 */
export function resolveAgentIdQuiet(
  ...sources: Array<Record<string, unknown> | undefined | null>
): string {
  return resolveAgentIdInner(sources, true);
}

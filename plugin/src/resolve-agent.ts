/**
 * Resolve agent identity from the best available source.
 *
 * Resolution order:
 *   1. Explicit field from caller (context.agentId, config.agentId)
 *   2. Session key parsing — "agent:AGENT_NAME:CHANNEL:TARGET". With no
 *      ``agents.list``, ``main`` is the install's default agent (5).
 *   3. Config agent name (config.agentName, config.agent?.name)
 *   4. CAURA_AGENT_ID env var
 *   5. The install's default agent id, the one its heartbeat registers
 *      (``getDefaultAgentId``): ``main-${installId}`` for a new install,
 *      so two OpenClaw installs sharing one tenant don't merge their
 *      memories into a single ``(tenant_id, agent_id="main")`` row;
 *      ``main`` for an install from before install.json recorded it,
 *      whose memories and recall already use it (M-107). Pre-Task6
 *      this was ``"unknown-agent"``, which is the same collision risk
 *      with worse semantics.
 */

import { CAURA_AGENT_ID } from "./env.js";
import { listedAgents, readOpenClawConfig } from "./config.js";
import { getDefaultAgentId } from "./install-id.js";

/**
 * The agent a session key names. With no ``agents.list``, OpenClaw's one
 * agent is ``main``, which the heartbeat registers under the install's
 * default agent id, so its turns write under that id too (M-107). A listed
 * agent, ``main`` included, keeps its name on both paths.
 */
function sessionAgentId(name: string): string {
  if (name !== "main" || listedAgents(readOpenClawConfig())) return name;
  return getDefaultAgentId();
}

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
        return sessionAgentId(parts[1]);
      }
    }

    if (src.agentName && typeof src.agentName === "string")
      return src.agentName as string;
  }

  if (CAURA_AGENT_ID) {
    if (!quiet) {
      console.warn(
        "[caura] Agent ID resolved from the CAURA_AGENT_ID (legacy: MEMCLAW_AGENT_ID) env var — consider passing agent_id explicitly", // legacy-name-ok: taught as legacy alias
      );
    }
    return CAURA_AGENT_ID;
  }

  // The install's default agent, as the heartbeat registers it. Was
  // ``"unknown-agent"`` pre-Task6 — every install collided on a single row.
  const fallback = getDefaultAgentId();
  if (!quiet) {
    console.warn(
      `[caura] Could not resolve agent ID — using install-default '${fallback}'. ` +
        `Pass agent_id explicitly (or set CAURA_AGENT_ID) for clarity.`,
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

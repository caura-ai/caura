/**
 * User-Agent the plugin sends on every HTTP request to the Caura server.
 *
 * The self-hosted heartbeat (spec "Caura Heartbeat v1", section 7) counts
 * connected SDK families from the ``User-Agent`` prefix; the server matches
 * this plugin on ``openclaw-plugin``. The header carries only the plugin
 * version and the Node major — nothing that isn't already in the heartbeat
 * body — and is sent only to ``CAURA_API_URL``. Every raw ``fetch()`` in the
 * plugin and the shared ``apiCall`` transport go through ``withUserAgent``
 * so new call sites inherit the header instead of re-deriving it.
 */

import { PLUGIN_VERSION } from "./version.js";

export const USER_AGENT_PREFIX = "openclaw-plugin";

/**
 * Build the User-Agent string: ``openclaw-plugin/<version> (node/<major>)``.
 * The ``node`` tag is omitted when no Node version is available (e.g. a
 * non-Node runtime), so the prefix + version is always the stable part.
 */
export function buildUserAgent(nodeVersion: string | undefined): string {
  const base = `${USER_AGENT_PREFIX}/${PLUGIN_VERSION}`;
  if (!nodeVersion) return base;
  const major = nodeVersion.split(".")[0];
  return major ? `${base} (node/${major})` : base;
}

export const USER_AGENT = buildUserAgent(globalThis.process?.versions?.node);

/**
 * Return a headers object that carries ``User-Agent`` plus ``headers``.
 * Caller-supplied headers win, so a deliberate override stays possible.
 */
export function withUserAgent(
  headers: Record<string, string> = {},
): Record<string, string> {
  return { "User-Agent": USER_AGENT, ...headers };
}

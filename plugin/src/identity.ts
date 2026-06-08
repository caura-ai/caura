/**
 * Human-readable display names for OpenClaw agents.
 *
 * Internal identity (``agent_id``) is a stable opaque suffix
 * (``main-${installId}``) used for DB joins / audit FKs / trust
 * lookups. The display name is what operators actually see in admin
 * UIs and dashboards. Hostnames make these recognisable
 * (``johnsmith-laptop-main`` vs ``main-a3f1b2c4``).
 *
 * The display name is recomputed per heartbeat so renaming a machine
 * propagates naturally to the UI; the underlying ``agent_id`` stays
 * stable so historical attribution is preserved.
 *
 * Resolution order:
 *   1. ``MEMCLAW_DISPLAY_NAME_OVERRIDE`` env var — operator's literal
 *      override, used verbatim. Lets users pick a pseudonymous or
 *      branded label, or pin a stable name when their hostname flaps.
 *   2. ``${shortHostname}-${baseName}`` — shortHostname is the first
 *      label of the OS hostname (so ``erni-openclaw.us-central1-c.c.
 *      alpine-theory-469016-c8.internal`` becomes ``erni-openclaw``).
 *      Cloud-VM FQDNs are noisy in admin UIs; the first label is the
 *      part operators actually call the machine.
 *   3. Just ``baseName`` when no hostname is available.
 *
 * No PII filtering — hostnames may include the operator's name (e.g.
 * ``johnsmith-laptop``). Operators who prefer a pseudonymous label
 * use the override env var.
 */

import { hostname } from "os";

/**
 * Normalise a hostname to a friendly suffix:
 *   - Take only the first dot-separated label so cloud-VM FQDNs
 *     (``box.us-central1-c.c.project.internal``) collapse to ``box``.
 *     The full FQDN is "informative" only on a hypothetical multi-
 *     cluster admin view; in practice it's noise that crowds out the
 *     baseName.
 *   - Strip mDNS suffixes (``.local``, ``.lan``) before splitting so
 *     ``mbp.local`` cleanly becomes ``mbp``.
 *   - Lowercase.
 *   - Replace whitespace and ``_`` with ``-`` (URL-friendly).
 *   - Drop characters outside ``[a-z0-9-]``.
 */
export function sanitizeHostname(raw: string): string {
  if (!raw) return "";
  let s = raw.toLowerCase();
  // Strip the most common mDNS / LAN suffixes first so the first-label
  // split below doesn't trim a useful name to ".local"-prefixed garbage.
  s = s.replace(/\.(local|lan)$/g, "");
  // First dot-separated label — drop FQDN tail entirely.
  const firstDot = s.indexOf(".");
  if (firstDot !== -1) {
    s = s.slice(0, firstDot);
  }
  // Replace separators with dashes.
  s = s.replace(/[\s_]+/g, "-");
  // Drop anything outside the URL-friendly set (no dots remain by here).
  s = s.replace(/[^a-z0-9-]+/g, "");
  // Collapse runs of dashes / leading-trailing.
  s = s.replace(/-+/g, "-").replace(/^-+|-+$/g, "");
  return s;
}

/**
 * Return the operator-friendly label for an agent.
 *
 * Resolution: env-var override → ``${shortHostname}-${baseName}`` →
 * ``baseName``. The hostname source and override env can be passed
 * for tests; production callers omit them and the OS hostname /
 * ``process.env.MEMCLAW_DISPLAY_NAME_OVERRIDE`` are used.
 */
export function getDisplayName(
  baseName: string,
  host?: string,
  override?: string | undefined,
): string {
  const ov =
    override !== undefined
      ? override
      : process.env.MEMCLAW_DISPLAY_NAME_OVERRIDE;
  if (ov && ov.trim()) return ov.trim();
  const h = sanitizeHostname(host !== undefined ? host : hostname());
  if (!h) return baseName;
  return `${h}-${baseName}`;
}

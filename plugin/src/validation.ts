/**
 * Validation helpers for Caura plugin security.
 *
 * Covers: UUID format, HTTPS enforcement, path containment,
 * HMAC command signature verification, and prompt length caps.
 */

import { createHmac, timingSafeEqual } from "crypto";
import { isAbsolute, relative, resolve, sep } from "path";
import { realpathSync, existsSync } from "fs";

// --- UUID validation ---

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SAFE_ID_RE = /^[\w-]{1,128}$/;

export function isValidUUID(value: unknown): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

export function isValidSafeId(value: unknown): value is string {
  return typeof value === "string" && SAFE_ID_RE.test(value);
}

export function assertSafePathSegment(
  value: unknown,
  label: string,
): asserts value is string {
  if (!isValidSafeId(value)) {
    throw new Error(
      `${label} must be 1-128 alphanumeric/dash/underscore characters, got: ${String(value).slice(0, 40)}`,
    );
  }
}

// --- HTTPS enforcement ---

/**
 * Whether the API key may travel to ``apiUrl``:
 *
 * - ``send``: https, or plain http to a loopback host (never leaves the box).
 * - ``send-insecure``: plain http to a remote host, explicitly allowed via
 *   ``CAURA_ALLOW_INSECURE_HTTP``.
 * - ``refuse``: plain http to a remote host (or an unparseable URL) — the key
 *   would cross the network in cleartext, so it must not be sent.
 */
export type KeyTransportPolicy = "send" | "send-insecure" | "refuse";

export function isLoopbackHost(hostname: string): boolean {
  const host = hostname.toLowerCase().replace(/^\[(.*)\]$/, "$1");
  return (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host === "::1" ||
    /^127\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(host)
  );
}

export function keyTransportPolicy(apiUrl: string, allowInsecureHttp: boolean): KeyTransportPolicy {
  let url: URL;
  try {
    url = new URL(apiUrl);
  } catch {
    return "refuse";
  }
  if (url.protocol === "https:") return "send";
  if (url.protocol === "http:" && isLoopbackHost(url.hostname)) return "send";
  return url.protocol === "http:" && allowInsecureHttp ? "send-insecure" : "refuse";
}

export function insecureKeyTransportMessage(apiUrl: string): string {
  let host = apiUrl;
  try {
    host = new URL(apiUrl).host;
  } catch {
    // keep the raw value; it is what the operator configured
  }
  return (
    `[caura] Refusing to send CAURA_API_KEY to ${host}: CAURA_API_URL uses plain HTTP to a ` +
    `non-loopback host, so the key would cross the network in cleartext. ` +
    `Fix: point CAURA_API_URL at https://, or set CAURA_ALLOW_INSECURE_HTTP=true ` +
    `in the plugin .env to accept the risk (e.g. a trusted private network).`
  );
}

/** Import-time report: one error when refusing, one warning when opted in. */
export function reportKeyTransportPolicy(
  apiUrl: string,
  apiKey: string,
  policy: KeyTransportPolicy,
): void {
  if (!apiKey) return;
  if (policy === "refuse") {
    console.error(insecureKeyTransportMessage(apiUrl));
  } else if (policy === "send-insecure") {
    console.warn(
      "[caura] WARNING: CAURA_ALLOW_INSECURE_HTTP is set — CAURA_API_KEY will be sent in " +
        "cleartext over plain HTTP to a non-loopback host, so anyone on the path can read it and " +
        "forge what the server sends; deploy, update_plugin and educate commands are refused and " +
        "skills are not synced in this mode. Use https:// where possible.",
    );
  }
}

// --- Path containment ---

interface PathContainmentOps {
  relative(from: string, to: string): string;
  isAbsolute(path: string): boolean;
  sep: string;
}

const HOST_PATH_OPS: PathContainmentOps = { relative, isAbsolute, sep };

/** @internal Exported to inject platform-specific path operations in tests. */
export function isContainedResolvedPath(
  child: string,
  parent: string,
  pathOps: PathContainmentOps = HOST_PATH_OPS,
): boolean {
  const relativePath = pathOps.relative(parent, child);
  return (
    relativePath === "" ||
    (relativePath !== ".." &&
      !relativePath.startsWith(`..${pathOps.sep}`) &&
      !pathOps.isAbsolute(relativePath))
  );
}

export function isContainedPath(child: string, parent: string): boolean {
  try {
    const resolvedChild = existsSync(child) ? realpathSync(child) : resolve(child);
    const resolvedParent = existsSync(parent) ? realpathSync(parent) : resolve(parent);
    return isContainedResolvedPath(resolvedChild, resolvedParent);
  } catch {
    return false;
  }
}

// --- HMAC command signature verification ---

const COMMAND_SIGNATURE_MAX_AGE_MS = 120_000; // 2 minutes

let _unsignedWarned = false;

/**
 * Verify (or accept) a fleet command's HMAC signature.
 *
 * The OSS server doesn't sign commands — signing is reserved for
 * enterprise gateways that proxy commands through a signing layer.
 * Defaulting to "fail closed when CAURA_API_KEY is set" (the prior
 * behavior) silently broke every fleet command (educate / deploy /
 * install_skill / uninstall_skill) on every OSS install with auth on,
 * because the secret used for tenant auth is not a command-signing
 * secret.
 *
 * Three modes, in priority order:
 *
 * 1. **Tampered**: a signature IS present but doesn't verify against
 *    ``secretKey`` → reject. This is always-on; it catches the case
 *    where someone hand-crafts a command with a bogus signature.
 * 2. **Strict** (``requireSigned=true``): missing/invalid signatures
 *    fail closed. Use when running behind an enterprise signing
 *    gateway that always signs commands.
 * 3. **Permissive** (``requireSigned=false``, default): accept
 *    unsigned commands; warn once per process so operators notice;
 *    still verify any signature that happens to be present.
 */
export function verifyCommandSignature(
  cmd: { id: string; command: string; payload?: Record<string, unknown>; timestamp?: string; signature?: string },
  secretKey: string,
  requireSigned: boolean = false,
): { valid: boolean; reason?: string } {
  if (!secretKey) {
    // No key configured — development/keyless mode.
    // Allow unsigned commands through, but reject if a signature IS present
    // (something is trying to look authenticated when we can't verify it).
    if (cmd.signature) {
      return { valid: false, reason: "no_secret_configured_but_signature_present" };
    }
    if (!_unsignedWarned) {
      console.warn(
        `[caura] accepting unsigned commands (no CAURA_API_KEY); set the key to enable verification.`,
      );
      _unsignedWarned = true;
    }
    return { valid: true, reason: "no_secret_configured" };
  }

  if (!cmd.signature) {
    if (requireSigned) {
      return { valid: false, reason: "missing_signature" };
    }
    // Permissive (default): server-side command-signing is opt-in
    // infra; reject only when the operator has explicitly demanded it
    // via CAURA_REQUIRE_SIGNED_COMMANDS=true. Warn once so the gap
    // is visible without flooding logs every 60s heartbeat.
    if (!_unsignedWarned) {
      console.warn(
        `[caura] accepting unsigned command "${cmd.command}" — server is not signing commands. ` +
          `Set CAURA_REQUIRE_SIGNED_COMMANDS=true to fail closed once your gateway signs.`,
      );
      _unsignedWarned = true;
    }
    return { valid: true, reason: "unsigned_accepted_permissive" };
  }

  if (!cmd.timestamp) {
    return { valid: false, reason: "missing_timestamp" };
  }

  // Check timestamp freshness
  const cmdTime = new Date(cmd.timestamp).getTime();
  if (isNaN(cmdTime)) {
    return { valid: false, reason: "invalid_timestamp" };
  }
  const age = Math.abs(Date.now() - cmdTime);
  if (age > COMMAND_SIGNATURE_MAX_AGE_MS) {
    return { valid: false, reason: "expired_timestamp" };
  }

  // Verify HMAC: sign(id + command + timestamp + payload)
  const payloadStr = cmd.payload ? JSON.stringify(cmd.payload) : "";
  const hmacInput = `${cmd.id}:${cmd.command}:${cmd.timestamp}:${payloadStr}`;
  const expected = createHmac("sha256", secretKey)
    .update(hmacInput)
    .digest("hex");

  const sigBuf = Buffer.from(cmd.signature, "hex");
  const expectedBuf = Buffer.from(expected, "hex");
  if (sigBuf.length !== expectedBuf.length || !timingSafeEqual(sigBuf, expectedBuf)) {
    return { valid: false, reason: "invalid_signature" };
  }

  return { valid: true };
}

// --- Prompt length cap ---

export const MAX_EDUCATE_PROMPT_LENGTH = 65_536; // 64KB

export function assertPromptLength(prompt: string): void {
  if (prompt.length > MAX_EDUCATE_PROMPT_LENGTH) {
    throw new Error(
      `Prompt too large (${prompt.length} bytes, max ${MAX_EDUCATE_PROMPT_LENGTH})`,
    );
  }
}

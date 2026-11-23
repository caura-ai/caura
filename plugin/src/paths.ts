import { join } from "path";
import { homedir } from "os";

/** ~/.openclaw */
export function getOpenClawBaseDir(): string {
  return join(homedir(), ".openclaw");
}

/** ~/.openclaw/plugins/memclaw */ // legacy-name-floor: frozen install path
export function getPluginDir(): string {
  return join(getOpenClawBaseDir(), "plugins", "memclaw"); // legacy-name-floor: floor
}

/** ~/.openclaw/openclaw.json */
export function getOpenClawConfigPath(): string {
  return join(getOpenClawBaseDir(), "openclaw.json");
}

/** ~/.openclaw/plugins/memclaw/.env */ // legacy-name-floor: frozen install path
export function getPluginEnvPath(): string {
  return join(getPluginDir(), ".env");
}

/** ~/.openclaw/plugins/memclaw/.agent-keys.json */ // legacy-name-floor: frozen install path
export function getSecretsPath(): string {
  return join(getPluginDir(), ".agent-keys.json");
}

/** ~/.openclaw/plugins/memclaw/install.json — per-install opaque id. */ // legacy-name-floor: frozen install path
export function getInstallStatePath(): string {
  return join(getPluginDir(), "install.json");
}

/** Private enrollment journal, separate from the legacy diagnostic install ID. */
export function getInstallAuthorityPath(): string {
  return join(getPluginDir(), ".installation-authority.json");
}

/** Bound agent keys and pending operations; never adopted by the legacy runtime. */
export function getInstallAgentKeysPath(): string {
  return join(getPluginDir(), ".installation-agent-keys.json");
}

/**
 * A remote deploy cannot change where the node sends its key, the key itself,
 * the tenant it acts for, or the switches that keep it safe.
 */
import { test } from "node:test";
import assert from "node:assert/strict";

const { isRemoteEnvDenied } = await import("./deploy.js");

test("connection, credential and safety keys are refused", () => {
  for (const key of [
    "CAURA_API_URL",
    "CAURA_API_KEY",
    "CAURA_API_PREFIX",
    "CAURA_KEY_TRANSPORT",
    "CAURA_TENANT_ID",
    "CAURA_ALLOW_INSECURE_HTTP",
    "CAURA_REQUIRE_SIGNED_COMMANDS",
    "CAURA_TASK_DB_PATH",
    "MEMCLAW_API_URL", // legacy-name-ok: rule 3 dual-read alias
    "MEMCLAW_API_KEY", // legacy-name-ok: rule 3 dual-read alias
    "caura_api_url",
  ]) {
    assert.equal(isRemoteEnvDenied(key), true, key);
  }
});

test("ordinary tuning keys still pass", () => {
  for (const key of [
    "CAURA_RECALL_POLICY",
    "CAURA_KEYSTONES_ENABLED",
    "CAURA_AUTO_WRITE_TURNS",
    "CAURA_NODE_NAME",
    "CAURA_FLEET_ID",
  ]) {
    assert.equal(isRemoteEnvDenied(key), false, key);
  }
});

test("keys outside the plugin prefix are not this rule's concern", () => {
  // hasPluginEnvPrefix already drops them before they reach .env.
  assert.equal(isRemoteEnvDenied("PATH_API_URL"), false);
});

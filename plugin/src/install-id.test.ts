import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "fs";
import { tmpdir } from "os";
import { join } from "path";

import { getInstallId, _resetInstallIdCacheForTesting } from "./install-id.js";

let _origHome: string | undefined;
let _tmpHome: string | undefined;

function pluginDir(home: string): string {
  return join(home, ".openclaw", "plugins", "memclaw");
}

function installFile(home: string): string {
  return join(pluginDir(home), "install.json");
}

beforeEach(() => {
  _resetInstallIdCacheForTesting();
  _origHome = process.env.HOME;
  _tmpHome = mkdtempSync(join(tmpdir(), "memclaw-installid-"));
  process.env.HOME = _tmpHome;
});

afterEach(() => {
  if (_origHome !== undefined) {
    process.env.HOME = _origHome;
  } else {
    delete process.env.HOME;
  }
  if (_tmpHome) rmSync(_tmpHome, { recursive: true, force: true });
  _resetInstallIdCacheForTesting();
});

describe("getInstallId", () => {
  test("first call generates and persists install.json", () => {
    const id = getInstallId();
    assert.equal(typeof id, "string");
    assert.ok(/^[a-f0-9]{12}$/.test(id), `expected 12 hex chars, got ${id}`);
    assert.ok(existsSync(installFile(_tmpHome!)));

    const persisted = JSON.parse(readFileSync(installFile(_tmpHome!), "utf-8"));
    assert.equal(persisted.install_id, id);
    assert.equal(persisted.schema_version, 1);
    assert.ok(typeof persisted.created_at === "string");
  });

  test("subsequent calls return the same value (cached)", () => {
    const id1 = getInstallId();
    const id2 = getInstallId();
    const id3 = getInstallId();
    assert.equal(id1, id2);
    assert.equal(id2, id3);
  });

  test("re-reading from disk after cache reset returns the same id", () => {
    const id1 = getInstallId();
    _resetInstallIdCacheForTesting();
    const id2 = getInstallId();
    assert.equal(id1, id2, "id must persist across process restarts");
  });

  test("two separate plugin dirs generate distinct install_ids", () => {
    const id1 = getInstallId();
    // Switch HOME to a fresh dir and clear the cache — simulates a
    // second install on the same machine in a different user's home.
    const second = mkdtempSync(join(tmpdir(), "memclaw-installid-2-"));
    try {
      process.env.HOME = second;
      _resetInstallIdCacheForTesting();
      const id2 = getInstallId();
      assert.notEqual(
        id1,
        id2,
        "distinct plugin dirs must generate distinct install_ids",
      );
    } finally {
      rmSync(second, { recursive: true, force: true });
    }
  });

  test("malformed install.json regenerates a fresh id", () => {
    const dir = pluginDir(_tmpHome!);
    mkdirSync(dir, { recursive: true });
    writeFileSync(installFile(_tmpHome!), "{ this is not json");
    const id = getInstallId();
    assert.ok(/^[a-f0-9]{12}$/.test(id));
    // The regenerated file is now valid JSON with the same id.
    const reread = JSON.parse(readFileSync(installFile(_tmpHome!), "utf-8"));
    assert.equal(reread.install_id, id);
  });
});

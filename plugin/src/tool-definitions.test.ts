/**
 * Tests for the MemClaw plugin tool surface.
 *
 * Guards the contract between `plugin/tools.json` (SoT), `MEMCLAW_TOOLS`
 * (registration order), `PARAM_SCHEMAS` (runtime input validation), and
 * `ENDPOINT_DISPATCH` (HTTP routing).
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { MEMCLAW_TOOLS } from "./tools.js";
import { createToolFromSpec } from "./tool-definitions.js";
import { TOOL_SPECS, TOOL_SPECS_BY_NAME, getSpec } from "./tool-specs.js";
import { buildToolsMd } from "./educate.js";
import { memclawPromptSectionText } from "./prompt-section.js";

describe("tool-specs loader", () => {
  test("loads a non-empty ordered spec list from tools.json", () => {
    assert.ok(Array.isArray(TOOL_SPECS));
    assert.ok(TOOL_SPECS.length > 0);
    for (const spec of TOOL_SPECS) {
      assert.equal(typeof spec.name, "string");
      assert.ok(spec.name.startsWith("memclaw_"), `spec name ${spec.name}`);
      assert.equal(typeof spec.description, "string");
      assert.ok(spec.description.length > 0, `${spec.name}: empty description`);
      assert.equal(typeof spec.plugin_exposed, "boolean");
    }
  });

  test("name index matches TOOL_SPECS exactly", () => {
    const fromIndex = new Set(Object.keys(TOOL_SPECS_BY_NAME));
    const fromList = new Set(TOOL_SPECS.map((s) => s.name));
    assert.deepEqual(fromIndex, fromList);
  });

  test("getSpec throws for unknown tool", () => {
    assert.throws(() => getSpec("memclaw_not_a_thing"), /Unknown tool/);
  });

  test("getSpec returns matching entry for known tool", () => {
    const spec = getSpec("memclaw_recall");
    assert.equal(spec.name, "memclaw_recall");
    assert.equal(spec.plugin_exposed, true);
  });
});

describe("MEMCLAW_TOOLS surface", () => {
  test("is the expected list of plugin tools", () => {
    assert.deepEqual([...MEMCLAW_TOOLS], [
      "memclaw_recall",
      "memclaw_write",
      "memclaw_manage",
      "memclaw_doc",
      "memclaw_list",
      "memclaw_entity_get",
      "memclaw_tune",
      "memclaw_insights",
      "memclaw_evolve",
      "memclaw_stats",
      // ``memclaw_keystones`` (read) is plugin-exposed so agents can
      // re-fetch governance rules mid-session; the ContextEngine also
      // injects them automatically at session start. The companion
      // write tool ``memclaw_keystones_set`` is MCP-only (admin path).
      "memclaw_keystones",
    ]);
  });

  test("every listed tool is plugin_exposed in tools.json", () => {
    for (const name of MEMCLAW_TOOLS) {
      const spec = TOOL_SPECS_BY_NAME[name];
      assert.ok(spec, `${name} missing from tools.json`);
      assert.equal(spec.plugin_exposed, true);
    }
  });

  test("every plugin_exposed tool in tools.json is listed in MEMCLAW_TOOLS", () => {
    const exposed = TOOL_SPECS.filter((s) => s.plugin_exposed).map((s) => s.name);
    for (const name of exposed) {
      assert.ok(
        (MEMCLAW_TOOLS as readonly string[]).includes(name),
        `${name} is plugin_exposed in tools.json but absent from MEMCLAW_TOOLS`,
      );
    }
  });

  test("STM and placeholder tools are NOT in MEMCLAW_TOOLS", () => {
    for (const hidden of [
      "memclaw_notes_read",
      "memclaw_bulletin_read",
      "memclaw_promote",
    ]) {
      assert.ok(
        !(MEMCLAW_TOOLS as readonly string[]).includes(hidden),
        `${hidden} should not be plugin-exposed`,
      );
    }
  });
});

describe("createToolFromSpec factory", () => {
  test("produces a valid AgentTool for every listed name", () => {
    for (const name of MEMCLAW_TOOLS) {
      const tool = createToolFromSpec(name);
      assert.equal(tool.name, name);
      assert.ok(tool.label.startsWith("MemClaw "), `${name}: label`);
      assert.ok(tool.description.length > 0, `${name}: description`);
      assert.ok(
        typeof tool.parameters === "object" && tool.parameters !== null,
        `${name}: parameters is object`,
      );
      assert.equal(typeof tool.execute, "function");
    }
  });

  test("throws for a tool name not in tools.json", () => {
    assert.throws(
      () => createToolFromSpec("memclaw_does_not_exist"),
      /Unknown tool/,
    );
  });

  test("op-dispatched tools declare op + required path params", () => {
    const manage = createToolFromSpec("memclaw_manage").parameters as any;
    assert.deepEqual(manage.required, ["op", "memory_id"]);
    assert.deepEqual(manage.properties.op.enum, [
      "read", "update", "transition", "delete",
    ]);

    const doc = createToolFromSpec("memclaw_doc").parameters as any;
    // ``collection`` is required for write/read/query/delete but optional
    // for search (omit → search across all collections) and list_collections,
    // so the schema gates only ``op``; the server enforces collection
    // per-op.
    assert.deepEqual(doc.required, ["op"]);
    assert.deepEqual(doc.properties.op.enum, [
      "write", "read", "query", "delete", "search", "list_collections",
    ]);
  });

  test("memclaw_write requires only agent_id (content/items are mutually exclusive)", () => {
    const write = createToolFromSpec("memclaw_write").parameters as any;
    assert.deepEqual(write.required, ["agent_id"]);
    assert.ok(write.properties.content);
    assert.ok(write.properties.items);
    assert.equal(write.properties.items.maxItems, 100);
  });

  test("memclaw_list has no required params (trust gate handled server-side)", () => {
    const list = createToolFromSpec("memclaw_list").parameters as any;
    assert.deepEqual(list.required, []);
    assert.ok(list.properties.cursor);
    assert.ok(list.properties.include_deleted);
  });

  test("openclaw.plugin.json contracts.tools matches MEMCLAW_TOOLS exactly", () => {
    // OpenClaw runtime requires plugins to declare ``contracts.tools``
    // before it accepts ``api.registerTool`` calls. The list MUST match
    // ``MEMCLAW_TOOLS`` — drift here means OpenClaw silently rejects
    // tool registration at boot ("plugin must declare contracts.tools
    // before registering agent tools" in the gateway log) and every
    // agent loses access to the tool.
    const manifestPath = join(
      import.meta.dirname,
      "..",
      "openclaw.plugin.json",
    );
    const manifest = JSON.parse(readFileSync(manifestPath, "utf-8"));
    const declared = manifest.contracts?.tools as string[] | undefined;
    assert.ok(Array.isArray(declared), "manifest must declare contracts.tools");
    assert.deepEqual(
      [...declared].sort(),
      [...MEMCLAW_TOOLS].sort(),
      "contracts.tools in openclaw.plugin.json must match MEMCLAW_TOOLS",
    );
  });

  test("description falls back to tools.json value when no live override", () => {
    // `getToolDescription` reads from the shared cache in env.ts. On a
    // fresh import (no /tool-descriptions fetch yet), the fallback should
    // be the description baked into tools.json.
    const spec = getSpec("memclaw_recall");
    const tool = createToolFromSpec("memclaw_recall");
    assert.equal(tool.description, spec.description);
  });
});

describe("drift checks across tool surface artefacts", () => {
  // SKILL.md ships with the plugin and is read on demand by every agent;
  // it MUST list every plugin-exposed tool as a tool card. Without this
  // gate, adding a tool (e.g. memclaw_stats in #64) silently leaves
  // SKILL.md a version behind and agents see the wrong tool count.
  test("SKILL.md has a tool card for every tool in MEMCLAW_TOOLS", () => {
    const skillPath = join(
      import.meta.dirname,
      "..",
      "skills",
      "memclaw",
      "SKILL.md",
    );
    const skill = readFileSync(skillPath, "utf-8");
    for (const name of MEMCLAW_TOOLS) {
      const marker = "**`" + name + "(";
      assert.ok(
        skill.includes(marker),
        `SKILL.md is missing tool card for ${name} (expected line starting with ${marker})`,
      );
    }
  });

  // TOOLS.md (per-workspace bootstrap) is built from buildToolsMd() and
  // injected into every turn. It must mention every exposed tool by name.
  test("buildToolsMd output mentions every tool in MEMCLAW_TOOLS", () => {
    const md = buildToolsMd();
    for (const name of MEMCLAW_TOOLS) {
      assert.ok(
        md.includes("`" + name + "`"),
        `buildToolsMd output missing ${name}`,
      );
    }
  });

  // PARAM_SCHEMAS / ENDPOINT_DISPATCH are not exported, but
  // createToolFromSpec throws when either is missing — iterating
  // MEMCLAW_TOOLS via the factory covers the "MEMCLAW_TOOLS ⊆ {schemas,
  // dispatch}" direction (already in the createToolFromSpec test
  // above). The reverse direction — extra entries in either map that
  // are NOT in MEMCLAW_TOOLS — is guarded indirectly: any extra entry
  // would still need a tools.json spec to be reachable, and the
  // existing "every plugin_exposed tool in tools.json is listed in
  // MEMCLAW_TOOLS" test catches that.
  test("MEMCLAW_TOOLS == plugin-exposed tools in tools.json (set equality)", () => {
    const exposed = new Set(
      TOOL_SPECS.filter((s) => s.plugin_exposed).map((s) => s.name),
    );
    const listed = new Set(MEMCLAW_TOOLS);
    assert.deepEqual(listed, exposed);
  });

  // The system-prompt fragment is paid on every turn. It MUST stay slim
  // and MUST NOT duplicate the protocol that already lives in TOOLS.md
  // (per-turn), AGENTS.md (per-turn), and SKILL.md (on-demand).
  // Regression guard against the pre-C3 verbose form.
  describe("prompt-section.ts (per-turn system-prompt fragment)", () => {
    const tools = memclawPromptSectionText(new Set(MEMCLAW_TOOLS));

    test("includes header, identity, and pointer to SKILL.md", () => {
      assert.ok(tools.includes("## MemClaw Memory"), "missing header");
      assert.ok(tools.includes("`agent_id`"), "missing agent_id mention");
      assert.ok(tools.includes("never fabricate") || tools.includes("Never fabricate"),
        "missing identity 'never fabricate' clause");
      assert.ok(
        tools.includes("skills/memclaw/SKILL.md"),
        "must reference skills/memclaw/SKILL.md by path",
      );
    });

    test("lists all currently-available MemClaw tools by name", () => {
      for (const name of MEMCLAW_TOOLS) {
        assert.ok(
          tools.includes(name),
          `prompt-section must list ${name} in available-tools cue`,
        );
      }
    });

    test("does NOT restate the deep protocol (lives in SKILL.md)", () => {
      // Pre-C3 the section emitted full prose for the three rules,
      // write triggers, document-store guidance, delete/transition,
      // and report-outcomes. All of that now lives in SKILL.md.
      assert.ok(
        !/Rule 1 — Search before/.test(tools),
        "pre-C3 'Rule 1 — Search before' prose leaked back into prompt-section",
      );
      assert.ok(
        !/Rule 2 — Write when/.test(tools),
        "pre-C3 'Rule 2 — Write when' prose leaked back into prompt-section",
      );
      assert.ok(
        !/Rule 3 — Update when/.test(tools),
        "pre-C3 'Rule 3 — Update when' prose leaked back into prompt-section",
      );
      assert.ok(
        !/Document Store/.test(tools),
        "Document Store guidance belongs in SKILL.md, not the per-turn fragment",
      );
      assert.ok(
        !/Deleting & transitions/.test(tools),
        "Delete/transition guidance belongs in SKILL.md",
      );
      assert.ok(
        !/Report outcomes/.test(tools),
        "Report-outcomes guidance belongs in SKILL.md",
      );
    });

    test("is slim: under 800 chars (post-C3 budget)", () => {
      // Pre-C3 this fragment was ~3 KB of prose paid on every turn.
      // If this grows back, ask whether the new content really has to
      // be every-turn and not on-demand via SKILL.md.
      assert.ok(
        tools.length < 800,
        `prompt-section is ${tools.length} chars; should be < 800. Move deep content to SKILL.md.`,
      );
    });

    test("emits nothing when no MemClaw tools are available", () => {
      const empty = memclawPromptSectionText(new Set());
      assert.equal(empty, "", "must emit empty string when no tools available");
    });
  });
});

describe("labelFor naming conversion", () => {
  test("memclaw_doc → MemClaw Doc, memclaw_entity_get → MemClaw Entity Get", () => {
    assert.equal(createToolFromSpec("memclaw_doc").label, "MemClaw Doc");
    assert.equal(
      createToolFromSpec("memclaw_entity_get").label,
      "MemClaw Entity Get",
    );
    assert.equal(createToolFromSpec("memclaw_list").label, "MemClaw List");
    assert.equal(
      createToolFromSpec("memclaw_manage").label,
      "MemClaw Manage",
    );
  });
});

/**
 * Tests for the Caura plugin tool surface.
 *
 * Guards the contract between `plugin/tools.json` (SoT), `CAURA_TOOLS`
 * (registration order), `PARAM_SCHEMAS` (runtime input validation), and
 * `ENDPOINT_DISPATCH` (HTTP routing).
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

import { CAURA_TOOLS } from "./tools.js";
import { createToolFromSpec } from "./tool-definitions.js";
import { TOOL_SPECS, TOOL_SPECS_BY_NAME, getSpec } from "./tool-specs.js";
import { buildToolsMd } from "./educate.js";
import { cauraPromptSectionText } from "./prompt-section.js";

describe("tool-specs loader", () => {
  test("loads a non-empty ordered spec list from tools.json", () => {
    assert.ok(Array.isArray(TOOL_SPECS));
    assert.ok(TOOL_SPECS.length > 0);
    for (const spec of TOOL_SPECS) {
      assert.equal(typeof spec.name, "string");
      assert.ok(spec.name.startsWith("caura_"), `spec name ${spec.name}`);
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
    assert.throws(() => getSpec("caura_not_a_thing"), /Unknown tool/);
  });

  test("getSpec returns matching entry for known tool", () => {
    const spec = getSpec("caura_recall");
    assert.equal(spec.name, "caura_recall");
    assert.equal(spec.plugin_exposed, true);
  });
});

describe("CAURA_TOOLS surface", () => {
  test("is the expected list of plugin tools", () => {
    assert.deepEqual([...CAURA_TOOLS], [
      "caura_recall",
      "caura_write",
      "caura_manage",
      "caura_doc",
      "caura_list",
      "caura_entity_get",
      "caura_tune",
      "caura_insights",
      "caura_evolve",
      "caura_stats",
      // ``caura_keystones`` (read) is plugin-exposed so agents can
      // re-fetch governance rules mid-session; the ContextEngine also
      // injects them automatically at session start. The companion
      // write tool ``caura_keystones_set`` is MCP-only (admin path).
      "caura_keystones",
    ]);
  });

  test("every listed tool is plugin_exposed in tools.json", () => {
    for (const name of CAURA_TOOLS) {
      const spec = TOOL_SPECS_BY_NAME[name];
      assert.ok(spec, `${name} missing from tools.json`);
      assert.equal(spec.plugin_exposed, true);
    }
  });

  test("every plugin_exposed tool in tools.json is listed in CAURA_TOOLS", () => {
    const exposed = TOOL_SPECS.filter((s) => s.plugin_exposed).map((s) => s.name);
    for (const name of exposed) {
      assert.ok(
        (CAURA_TOOLS as readonly string[]).includes(name),
        `${name} is plugin_exposed in tools.json but absent from CAURA_TOOLS`,
      );
    }
  });

  test("every listed tool name is canonical (caura_*)", () => {
    // Legacy names are dispatch aliases on the server, never listed on
    // the plugin surface — so the meaningful invariant is that every
    // name here (and in tools.json) carries the canonical prefix.
    for (const name of CAURA_TOOLS) {
      assert.match(name, /^caura_/, `${name}: must carry the caura_ prefix`);
    }
    for (const spec of TOOL_SPECS) {
      assert.match(
        spec.name,
        /^caura_/,
        `${spec.name}: tools.json must list canonical names only`,
      );
    }
  });
});

describe("createToolFromSpec factory", () => {
  test("produces a valid AgentTool for every listed name", () => {
    for (const name of CAURA_TOOLS) {
      const tool = createToolFromSpec(name);
      assert.equal(tool.name, name);
      assert.ok(tool.label.startsWith("Caura "), `${name}: label`);
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
      () => createToolFromSpec("caura_does_not_exist"),
      /Unknown tool/,
    );
  });

  test("op-dispatched tools declare op + required path params", () => {
    const manage = createToolFromSpec("caura_manage").parameters as any;
    assert.deepEqual(manage.required, ["op", "memory_id"]);
    assert.deepEqual(manage.properties.op.enum, [
      "read", "update", "transition", "delete",
    ]);

    const doc = createToolFromSpec("caura_doc").parameters as any;
    // ``collection`` is required for write/read/query/delete but optional
    // for search (omit → search across all collections) and list_collections,
    // so the schema gates only ``op``; the server enforces collection
    // per-op.
    assert.deepEqual(doc.required, ["op"]);
    assert.deepEqual(doc.properties.op.enum, [
      "write", "read", "query", "delete", "search", "list_collections",
    ]);
  });

  test("caura_write requires only agent_id (content/items are mutually exclusive)", () => {
    const write = createToolFromSpec("caura_write").parameters as any;
    assert.deepEqual(write.required, ["agent_id"]);
    assert.ok(write.properties.content);
    assert.ok(write.properties.items);
    assert.equal(write.properties.items.maxItems, 100);
  });

  test("caura_list has no required params (trust gate handled server-side)", () => {
    const list = createToolFromSpec("caura_list").parameters as any;
    assert.deepEqual(list.required, []);
    assert.ok(list.properties.cursor);
    assert.ok(list.properties.include_deleted);
  });

  // ``GET /memories`` and ``GET /memories/stats`` declare ``scope`` as an
  // OPT-IN query param (``Query(default=None)``), and an omitted one is a
  // different request from ``scope='agent'``: it skips the trust ladder and
  // takes its author filter from ``written_by ?? agent_id``. So neither may
  // declare a schema default — a client honouring it would start sending
  // 'agent', which adds the trust-1 gate and turns an agent-less install's
  // tenant-wide team/org read into a 400. Nor may the description call any
  // value "the default", which is what it used to claim.
  //
  // ``caura_insights`` / ``caura_evolve`` are deliberately not covered: their
  // ``scope`` travels in a JSON body whose Pydantic model really does
  // ``Field(default="agent")``, so calling 'agent' the default is true there.
  test("caura_list / caura_stats declare no default for scope", () => {
    for (const name of ["caura_list", "caura_stats"]) {
      const schema = createToolFromSpec(name).parameters as any;
      const scope = schema.properties.scope;
      assert.ok(scope, `${name}: scope property missing`);
      assert.deepEqual(scope.enum, ["agent", "fleet", "all"], `${name}: scope enum`);
      assert.ok(
        !("default" in scope),
        `${name}: scope must not declare a schema default — an omitted scope is not scope='agent'`,
      );
      assert.ok(
        !/\(default/.test(scope.description),
        `${name}: scope description must not call a value "the default": ${scope.description}`,
      );
    }
  });

  test("openclaw.plugin.json contracts.tools matches CAURA_TOOLS exactly", () => {
    // OpenClaw runtime requires plugins to declare ``contracts.tools``
    // before it accepts ``api.registerTool`` calls. The list MUST match
    // ``CAURA_TOOLS`` — drift here means OpenClaw silently rejects
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
      [...CAURA_TOOLS].sort(),
      "contracts.tools in openclaw.plugin.json must match CAURA_TOOLS",
    );
  });

  test("description falls back to tools.json value when no live override", () => {
    // `getToolDescription` reads from the shared cache in env.ts. On a
    // fresh import (no /tool-descriptions fetch yet), the fallback should
    // be the description baked into tools.json.
    const spec = getSpec("caura_recall");
    const tool = createToolFromSpec("caura_recall");
    assert.equal(tool.description, spec.description);
  });
});

describe("drift checks across tool surface artefacts", () => {
  // SKILL.md ships with the plugin and is read on demand by every agent;
  // it MUST name every plugin-exposed tool. Per-tool *signatures* are
  // deferred to the live MCP schemas + injected TOOLS.md, so this gate
  // asserts presence by name (not a signature card). Without it, adding a
  // tool (e.g. caura_stats in #64) silently leaves SKILL.md a version
  // behind and agents see the wrong tool surface.
  test("SKILL.md names every tool in CAURA_TOOLS", () => {
    const skillPath = join(
      import.meta.dirname,
      "..",
      "skills",
      "memclaw",
      "SKILL.md",
    );
    const skill = readFileSync(skillPath, "utf-8");
    for (const name of CAURA_TOOLS) {
      assert.ok(
        skill.includes(name),
        `SKILL.md does not mention ${name}`,
      );
    }
  });

  // Content invariants mirroring tests/test_direct_mcp_skill.py for the
  // canonical variant: facts the plugin SKILL.md must keep stating. If you
  // remove one, justify it; if you add a must-keep fact, pin it here.
  test("SKILL.md keeps the canonical content invariants", () => {
    const skillPath = join(
      import.meta.dirname,
      "..",
      "skills",
      "memclaw",
      "SKILL.md",
    );
    const skill = readFileSync(skillPath, "utf-8");
    const mustContain = [
      // Reflective ingest (Interviewer) — realtime writes stay mandatory,
      // and the note must not conflate it with the plugin auto-write layer
      "Some memories may be written for you",
      "Interviewer is periodic and reflective",
      "different mechanism",
      // Skill Factory governance — staged is not an error, prefixes are provenance
      "Skill Factory",
      "Skills Inbox",
      "forge/<slug>",
    ];
    for (const phrase of mustContain) {
      assert.ok(
        skill.includes(phrase),
        `SKILL.md missing invariant phrase: ${phrase}`,
      );
    }
  });

  // TOOLS.md (per-workspace bootstrap) is built from buildToolsMd() and
  // injected into every turn. It must mention every exposed tool by name.
  test("buildToolsMd output mentions every tool in CAURA_TOOLS", () => {
    const md = buildToolsMd();
    for (const name of CAURA_TOOLS) {
      assert.ok(
        md.includes("`" + name + "`"),
        `buildToolsMd output missing ${name}`,
      );
    }
  });

  // PARAM_SCHEMAS / ENDPOINT_DISPATCH are not exported, but
  // createToolFromSpec throws when either is missing — iterating
  // CAURA_TOOLS via the factory covers the "CAURA_TOOLS ⊆ {schemas,
  // dispatch}" direction (already in the createToolFromSpec test
  // above). The reverse direction — extra entries in either map that
  // are NOT in CAURA_TOOLS — is guarded indirectly: any extra entry
  // would still need a tools.json spec to be reachable, and the
  // existing "every plugin_exposed tool in tools.json is listed in
  // CAURA_TOOLS" test catches that.
  test("CAURA_TOOLS == plugin-exposed tools in tools.json (set equality)", () => {
    const exposed = new Set(
      TOOL_SPECS.filter((s) => s.plugin_exposed).map((s) => s.name),
    );
    const listed = new Set(CAURA_TOOLS);
    assert.deepEqual(listed, exposed);
  });

  // The system-prompt fragment is paid on every turn. It MUST stay slim
  // and MUST NOT duplicate the protocol that already lives in TOOLS.md
  // (per-turn), AGENTS.md (per-turn), and SKILL.md (on-demand).
  // Regression guard against the pre-C3 verbose form.
  describe("prompt-section.ts (per-turn system-prompt fragment)", () => {
    const tools = cauraPromptSectionText(new Set(CAURA_TOOLS));

    test("includes header, identity, and pointer to the memclaw skill (by name)", () => { // legacy-name-floor: OpenClaw skill slug
      assert.ok(tools.includes("## Caura Memory"), "missing header");
      assert.ok(tools.includes("`agent_id`"), "missing agent_id mention");
      assert.ok(tools.includes("never fabricate") || tools.includes("Never fabricate"),
        "missing identity 'never fabricate' clause");
      // CAURA-000: the per-turn fragment must point at the **memclaw** // legacy-name-floor: OpenClaw skill slug
      // skill by NAME, not a filesystem path — a path pointer made cron
      // agents run OpenClaw's `search` tool to find a file that isn't in
      // their workspace (the skill is manifest-discovered at plugin-root),
      // burning ~3 min and failing the turn.
      const flat = tools.replace(/\s+/g, " ");
      assert.ok(
        !flat.includes("skills/memclaw/SKILL.md"),
        "must NOT reference the skill by filesystem path (CAURA-000)",
      );
      assert.ok(
        /\*\*memclaw\*\* skill/i.test(flat),
        "must reference the **memclaw** skill by name", // legacy-name-floor: OpenClaw skill slug
      );
      assert.ok(
        /do NOT search the filesystem/i.test(flat),
        "must tell the agent not to filesystem-search for the skill",
      );
    });

    test("lists all currently-available Caura tools by name", () => {
      for (const name of CAURA_TOOLS) {
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

    test("emits nothing when no Caura tools are available", () => {
      const empty = cauraPromptSectionText(new Set());
      assert.equal(empty, "", "must emit empty string when no tools available");
    });
  });
});

describe("labelFor naming conversion", () => {
  test("caura_doc → Caura Doc, caura_entity_get → Caura Entity Get", () => {
    assert.equal(createToolFromSpec("caura_doc").label, "Caura Doc");
    assert.equal(
      createToolFromSpec("caura_entity_get").label,
      "Caura Entity Get",
    );
    assert.equal(createToolFromSpec("caura_list").label, "Caura List");
    assert.equal(
      createToolFromSpec("caura_manage").label,
      "Caura Manage",
    );
  });
});

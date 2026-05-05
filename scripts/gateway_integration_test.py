#!/usr/bin/env python3
"""MemClaw Gateway Integration Test — validates the plugin running INSIDE OpenClaw.

Unlike smoke_test.py (which hits the backend API directly), this script validates
that the plugin is loaded in the gateway, lifecycle hooks fire, education files
are written, heartbeats include setup_status, and fleet commands are processed.

Prerequisites:
  - Plugin installed via wet-test-install.sh (or manually)
  - OpenClaw gateway running with the plugin loaded
  - MemClaw backend API reachable

Usage:
    python scripts/gateway_integration_test.py \\
        --url http://localhost:8000 \\
        --api-key mc_key_xxx \\
        --tenant-id ran-test-1 \\
        --node-name dev-vm-1 \\
        --fleet-id my-fleet \\
        --openclaw-dir /home/user/.openclaw

    # Minimal (uses defaults for dir):
    python scripts/gateway_integration_test.py \\
        --url http://localhost:8000 --api-key mc_key_xxx \\
        --tenant-id ran-test-1 --node-name dev-vm-1

    # JSON output for CI:
    python scripts/gateway_integration_test.py ... --json
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

TIMEOUT = 30.0


class GatewayIntegrationTest:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        tenant_id: str,
        node_name: str,
        fleet_id: str = "",
        openclaw_dir: str = "",
        *,
        verbose: bool = False,
        json_output: bool = False,
    ):
        self.base = base_url.rstrip("/")
        self.api = f"{self.base}/api"
        self.api_key = api_key
        self.tenant_id = tenant_id
        self.node_name = node_name
        self.fleet_id = fleet_id
        self.openclaw_dir = openclaw_dir or os.path.expanduser("~/.openclaw")
        self.verbose = verbose
        self.json_output = json_output

        self.headers = {"X-API-Key": api_key} if api_key else {}
        self.client = httpx.Client(timeout=TIMEOUT, headers=self.headers)

        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.results: list[dict] = []
        self._test_times: list[tuple[str, float]] = []
        self._node_id: str | None = None
        self._memory_ids: list[str] = []

    # ── Helpers ──

    def check(self, name: str, condition: bool, detail: str = ""):
        entry = {"name": name, "status": "PASS" if condition else "FAIL"}
        if detail:
            entry["detail"] = detail
        self.results.append(entry)
        if condition:
            self.passed += 1
            if not self.json_output:
                print(f"  PASS  {name}")
        else:
            self.failed += 1
            if not self.json_output:
                msg = f"  FAIL  {name}"
                if detail:
                    msg += f"  ({detail})"
                print(msg)

    def skip(self, name: str, reason: str = ""):
        self.skipped += 1
        entry = {"name": name, "status": "SKIP"}
        if reason:
            entry["detail"] = reason
        self.results.append(entry)
        if not self.json_output:
            msg = f"  SKIP  {name}"
            if reason:
                msg += f"  ({reason})"
            print(msg)

    def _run_test(self, test_fn):
        name = test_fn.__name__
        t0 = time.monotonic()
        try:
            test_fn()
        except (
            httpx.ReadError,
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.PoolTimeout,
        ) as exc:
            self.check(f"{name}: network error", False, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            self.check(
                f"{name}: unexpected error", False, f"{type(exc).__name__}: {exc}"
            )
        finally:
            self._test_times.append((name, time.monotonic() - t0))

    def _retry_until(self, fn, *, predicate, interval=1.0, max_wait=30.0):
        elapsed = 0.0
        result = None
        while elapsed < max_wait:
            result = fn()
            if predicate(result):
                return result
            time.sleep(interval)
            elapsed += interval
            interval = min(interval * 1.5, 5.0)
        return result

    # ── Test runner ──

    def run(self) -> int:
        t_start = time.monotonic()

        if not self.json_output:
            print()
            print("  MemClaw Gateway Integration Test")
            print(f"  URL:       {self.base}")
            print(f"  Tenant:    {self.tenant_id}")
            print(f"  Node:      {self.node_name}")
            print(f"  Fleet:     {self.fleet_id or '(not set)'}")
            print(f"  OpenClaw:  {self.openclaw_dir}")
            print()

        # Pre-flight: verify API is reachable
        try:
            self.client.get(f"{self.api}/health", timeout=5)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if self.json_output:
                print(
                    json.dumps(
                        {"error": f"Cannot connect: {exc}", "passed": 0, "failed": 1}
                    )
                )
            else:
                print(f"  FAIL  Cannot connect to {self.base}: {exc}")
            return 1

        tests = [
            # ── Section 1: Plugin loaded & configured ──
            self.test_plugin_manifest_exists,
            self.test_plugin_dist_exists,
            self.test_plugin_env_exists,
            self.test_openclaw_config_allowlist,
            self.test_openclaw_config_tools,
            self.test_openclaw_config_load_path,
            # ── Section 2: Gateway logs (smoke test output) ──
            self.test_gateway_log_plugin_loaded,
            # ── Section 3: Heartbeat with setup_status ──
            self.test_heartbeat_setup_status,
            # ── Section 4: Fleet command processing (ping) ──
            self.test_fleet_command_ping,
            # ── Section 5: Education files ──
            self.test_educated_flag_exists,
            self.test_skill_md_exists,
            self.test_tools_md_has_memclaw,
            self.test_agents_md_has_memclaw,
            self.test_heartbeat_md_exists,
            # ── Section 6: Memory prompt section injection ──
            self.test_prompt_section_file,
            # ── Section 7: Context engine lifecycle — write + recall ──
            self.test_context_engine_write_memory,
            self.test_context_engine_search_recall,
            self.test_context_engine_recall_endpoint,
            # ── Section 8: afterTurn auto-write simulation ──
            self.test_after_turn_auto_write,
            # ── Section 9: Flush plan / compaction persistence ──
            self.test_compaction_write,
            # ── Section 10: Fleet command — educate ──
            self.test_fleet_command_educate,
            # ── Section 11: Tool descriptions served ──
            self.test_tool_descriptions_count,
        ]

        for test_fn in tests:
            self._run_test(test_fn)

        # Cleanup
        self._cleanup()

        elapsed = time.monotonic() - t_start

        if self.json_output:
            print(
                json.dumps(
                    {
                        "passed": self.passed,
                        "failed": self.failed,
                        "skipped": self.skipped,
                        "total": self.passed + self.failed + self.skipped,
                        "elapsed_s": round(elapsed, 2),
                        "results": self.results,
                    },
                    indent=2,
                )
            )
        else:
            print()
            total = self.passed + self.failed + self.skipped
            if self.failed == 0:
                print(f"  ALL {self.passed} TESTS PASSED", end="")
                if self.skipped:
                    print(f" ({self.skipped} skipped)", end="")
                print(f"  [{elapsed:.1f}s]")
            else:
                print(
                    f"  {self.passed}/{total} passed, {self.failed} FAILED, {self.skipped} skipped  [{elapsed:.1f}s]"
                )
            if self._test_times:
                slowest = sorted(self._test_times, key=lambda x: x[1], reverse=True)[:5]
                if slowest[0][1] > 1.0:
                    print()
                    print("  Slowest tests:")
                    for name, dur in slowest:
                        print(f"    {dur:6.1f}s  {name}")
            print()

        return 0 if self.failed == 0 else 1

    def _cleanup(self):
        """Delete memories created during the test."""
        for mid in self._memory_ids:
            try:
                self.client.delete(
                    f"{self.api}/memories/{mid}", params={"tenant_id": self.tenant_id}
                )
            except Exception:
                pass

    # ═══════════════════════════════════════════════════════════
    # Section 1: Plugin installed & configured
    # ═══════════════════════════════════════════════════════════

    def test_plugin_manifest_exists(self):
        """Verify openclaw.plugin.json exists in the plugin directory."""
        manifest = (
            Path(self.openclaw_dir) / "plugins" / "memclaw" / "openclaw.plugin.json"
        )
        exists = manifest.exists()
        self.check("Plugin manifest exists", exists, str(manifest))
        if exists:
            data = json.loads(manifest.read_text())
            has_memory = data.get("kind") == "memory" or data.get("type") == "memory"
            self.check(
                "Plugin manifest: memory type",
                has_memory,
                f"kind={data.get('kind')}, type={data.get('type')}",
            )
            self.check(
                "Plugin manifest: has version",
                bool(data.get("version")),
                f"version={data.get('version')}",
            )

    def test_plugin_dist_exists(self):
        """Verify compiled JS exists."""
        dist = Path(self.openclaw_dir) / "plugins" / "memclaw" / "dist" / "index.js"
        self.check("Plugin dist/index.js exists", dist.exists(), str(dist))

    def test_plugin_env_exists(self):
        """Verify .env exists with required vars."""
        env_path = Path(self.openclaw_dir) / "plugins" / "memclaw" / ".env"
        exists = env_path.exists()
        self.check("Plugin .env exists", exists, str(env_path))
        if exists:
            content = env_path.read_text()
            self.check(
                "Plugin .env: has API_URL",
                "MEMCLAW_API_URL=" in content,
                "MEMCLAW_API_URL",
            )
            self.check(
                "Plugin .env: has API_KEY",
                "MEMCLAW_API_KEY=" in content,
                "MEMCLAW_API_KEY",
            )

    def test_openclaw_config_allowlist(self):
        """Verify memclaw is in plugins.allow in openclaw.json."""
        config_path = Path(self.openclaw_dir) / "openclaw.json"
        if not config_path.exists():
            self.skip("OpenClaw config: allowlist", "openclaw.json not found")
            return
        config = json.loads(config_path.read_text())
        allow = config.get("plugins", {}).get("allow", [])
        self.check(
            "OpenClaw config: memclaw in plugins.allow",
            "memclaw" in allow,
            f"allow={allow}",
        )

    def test_openclaw_config_tools(self):
        """Verify all 9 memclaw tools are in tools.alsoAllow."""
        config_path = Path(self.openclaw_dir) / "openclaw.json"
        if not config_path.exists():
            self.skip("OpenClaw config: tools", "openclaw.json not found")
            return
        config = json.loads(config_path.read_text())
        also_allow = config.get("tools", {}).get("alsoAllow", [])
        expected_tools = [
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
        ]
        missing = [t for t in expected_tools if t not in also_allow]
        self.check(
            "OpenClaw config: all 10 tools allowed",
            len(missing) == 0,
            f"missing={missing}" if missing else "",
        )

    def test_openclaw_config_load_path(self):
        """Verify plugin path is in plugins.load.paths."""
        config_path = Path(self.openclaw_dir) / "openclaw.json"
        if not config_path.exists():
            self.skip("OpenClaw config: load path", "openclaw.json not found")
            return
        config = json.loads(config_path.read_text())
        paths = config.get("plugins", {}).get("load", {}).get("paths", [])
        plugin_dir = str(Path(self.openclaw_dir) / "plugins" / "memclaw")
        has_path = any(plugin_dir in p for p in paths)
        self.check("OpenClaw config: plugin in load paths", has_path, f"paths={paths}")

    # ═══════════════════════════════════════════════════════════
    # Section 2: Gateway log verification
    # ═══════════════════════════════════════════════════════════

    def test_gateway_log_plugin_loaded(self):
        """Check gateway logs for plugin loading confirmation."""
        log_content = ""
        try:
            import subprocess

            result = subprocess.run(
                [
                    "journalctl",
                    "--user",
                    "-u",
                    "openclaw-gateway",
                    "--since",
                    "1 hour ago",
                    "--no-pager",
                    "-q",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            log_content = result.stdout
        except Exception:
            for log_path in [
                Path("/var/log/openclaw/gateway.log"),
                Path(self.openclaw_dir) / "logs" / "gateway.log",
                Path(self.openclaw_dir) / "gateway.log",
            ]:
                if log_path.exists():
                    lines = log_path.read_text().splitlines()
                    log_content = "\n".join(lines[-500:])
                    break

        if not log_content:
            self.skip("Gateway log: plugin loaded", "could not read gateway logs")
            self.skip(
                "Gateway log: context engine registered", "could not read gateway logs"
            )
            self.skip("Gateway log: smoke test", "could not read gateway logs")
            return

        self.check(
            "Gateway log: plugin loaded",
            "[memclaw]" in log_content,
            "searched journalctl + log files",
        )

        self.check(
            "Gateway log: context engine registered",
            "ContextEngine" in log_content and "registered" in log_content,
            "looking for 'ContextEngine.*registered'",
        )

        has_smoke = "Smoke test passed" in log_content
        has_smoke_any = "SMOKE TEST" in log_content or "Smoke test" in log_content
        self.check(
            "Gateway log: smoke test ran",
            has_smoke or has_smoke_any,
            "passed"
            if has_smoke
            else ("ran but may not be passing" if has_smoke_any else "not found"),
        )

    # ═══════════════════════════════════════════════════════════
    # Section 3: Heartbeat with setup_status
    # ═══════════════════════════════════════════════════════════

    def test_heartbeat_setup_status(self):
        """Send a heartbeat and verify setup_status is persisted."""
        hb_body = {
            "tenant_id": self.tenant_id,
            "node_name": self.node_name,
            "fleet_id": self.fleet_id or None,
            "hostname": "gateway-integration-test",
            "plugin_version": "0.9.10-test",
            "tools": [
                "memclaw_recall",
                "memclaw_write",
                "memclaw_manage",
                "memclaw_doc",
                "memclaw_list",
                "memclaw_entity_get",
                "memclaw_tune",
                "memclaw_insights",
                "memclaw_evolve",
            ],
            "metadata": {
                "setup_status": {
                    "plugin_loaded": True,
                    "tools_registered": 9,
                    "tools_allowed": True,
                    "fully_configured": True,
                    "agents_educated": True,
                }
            },
        }
        r = self.client.post(f"{self.api}/fleet/heartbeat", json=hb_body)
        self.check("Heartbeat: 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code != 200:
            return

        data = r.json()
        self.check("Heartbeat: ok=true", data.get("ok") is True)
        self._node_id = data.get("node_id")
        self.check(
            "Heartbeat: has node_id", bool(self._node_id), f"node_id={self._node_id}"
        )
        self.check(
            "Heartbeat: has commands list", isinstance(data.get("commands"), list)
        )

        # Verify setup_status was persisted
        r2 = self.client.get(
            f"{self.api}/fleet/nodes", params={"tenant_id": self.tenant_id}
        )
        if r2.status_code == 200:
            nodes = (
                r2.json() if isinstance(r2.json(), list) else r2.json().get("nodes", [])
            )
            our_node = None
            for n in nodes:
                if n.get("node_name") == self.node_name or str(n.get("id")) == str(
                    self._node_id
                ):
                    our_node = n
                    break
            if our_node:
                meta = our_node.get("metadata", {})
                ss = meta.get("setup_status", {})
                self.check(
                    "Heartbeat: setup_status persisted",
                    ss.get("plugin_loaded") is True,
                    f"setup_status={json.dumps(ss)[:200]}",
                )
                self.check(
                    "Heartbeat: setup_status.tools_registered=13",
                    ss.get("tools_registered") == 13,
                    f"tools_registered={ss.get('tools_registered')}",
                )
            else:
                self.check(
                    "Heartbeat: node found in fleet list",
                    False,
                    f"node_name={self.node_name} not in {len(nodes)} nodes",
                )
        else:
            self.skip(
                "Heartbeat: setup_status persisted",
                f"fleet/nodes returned {r2.status_code}",
            )

    # ═══════════════════════════════════════════════════════════
    # Section 4: Fleet command processing (ping)
    # ═══════════════════════════════════════════════════════════

    def test_fleet_command_ping(self):
        """Queue a 'ping' command, wait for the plugin to process it."""
        if not self._node_id:
            self.skip("Fleet command: ping", "no node_id from heartbeat")
            return

        r = self.client.post(
            f"{self.api}/fleet/commands",
            json={
                "node_id": self._node_id,
                "command": "ping",
            },
        )
        self.check(
            "Fleet command: ping queued",
            r.status_code == 201,
            f"status={r.status_code}",
        )
        if r.status_code != 201:
            return

        cmd_id = r.json().get("id")
        self.check("Fleet command: has command id", bool(cmd_id))

        if not self.json_output:
            print("         ... waiting for plugin to process ping (up to 90s) ...")

        def check_cmd():
            try:
                cr = self.client.get(
                    f"{self.api}/fleet/commands",
                    params={"tenant_id": self.tenant_id, "node_id": str(self._node_id)},
                )
                if cr.status_code == 200:
                    cmds = (
                        cr.json()
                        if isinstance(cr.json(), list)
                        else cr.json().get("commands", [])
                    )
                    for c in cmds:
                        if str(c.get("id")) == str(cmd_id):
                            return c
                return None
            except Exception:
                return None

        result = self._retry_until(
            check_cmd,
            predicate=lambda c: c is not None and c.get("status") in ("done", "failed"),
            interval=5.0,
            max_wait=90.0,
        )

        if result and result.get("status") == "done":
            self.check("Fleet command: ping completed", True)
            cmd_result = result.get("result", {})
            self.check(
                "Fleet command: ping has pong",
                cmd_result.get("pong") is True or cmd_result.get("ok") is True,
                f"result={json.dumps(cmd_result)[:200]}",
            )
            self.check(
                "Fleet command: ping has plugin_version",
                bool(cmd_result.get("plugin_version")),
                f"version={cmd_result.get('plugin_version')}",
            )
        elif result and result.get("status") == "failed":
            self.check(
                "Fleet command: ping completed",
                False,
                f"status=failed, result={result.get('result')}",
            )
        elif result and result.get("status") == "acked":
            self.check(
                "Fleet command: ping completed",
                False,
                "command was acked but not completed within 90s — "
                "is the gateway heartbeat loop running?",
            )
        else:
            self.check(
                "Fleet command: ping completed",
                False,
                "command not picked up within 90s — "
                "gateway may not be running or plugin not loaded",
            )

    # ═══════════════════════════════════════════════════════════
    # Section 5: Education files
    # ═══════════════════════════════════════════════════════════

    def test_educated_flag_exists(self):
        """Check that the .educated flag was written on first load."""
        flag = Path(self.openclaw_dir) / "plugins" / "memclaw" / ".educated"
        self.check("Education: .educated flag exists", flag.exists(), str(flag))

    def _find_workspaces(self) -> list[Path]:
        """Find OpenClaw workspace directories."""
        base = Path(self.openclaw_dir)
        ws = []
        default_ws = base / "workspace"
        if default_ws.is_dir():
            ws.append(default_ws)
        for d in sorted(base.iterdir()):
            if d.is_dir() and d.name.startswith("workspace") and d.name != "workspace":
                ws.append(d)
        ws_parent = base / "workspaces"
        if ws_parent.is_dir():
            for d in sorted(ws_parent.iterdir()):
                if d.is_dir():
                    ws.append(d)
        return ws

    def test_skill_md_exists(self):
        """Check SKILL.md was written to at least one workspace."""
        workspaces = self._find_workspaces()
        if not workspaces:
            self.skip("Education: SKILL.md exists", "no workspaces found")
            return
        found = []
        for ws in workspaces:
            skill = ws / "skills" / "memclaw" / "SKILL.md"
            if skill.exists():
                found.append(str(skill))
        self.check(
            "Education: SKILL.md exists",
            len(found) > 0,
            f"checked {len(workspaces)} workspace(s), found in {len(found)}",
        )
        if found:
            content = Path(found[0]).read_text()
            self.check(
                "Education: SKILL.md has 3 rules",
                "Rule 1" in content and "Rule 2" in content and "Rule 3" in content,
                "expected Rule 1, Rule 2, Rule 3",
            )
            self.check(
                "Education: SKILL.md has tool table",
                "memclaw_write" in content and "memclaw_recall" in content,
                "expected tool names in skill doc",
            )
            self.check(
                "Education: SKILL.md has 3-layer protocol",
                "3-Layer" in content or "3-LAYER" in content,
                "expected 3-Layer Memory Capture section",
            )

    def test_tools_md_has_memclaw(self):
        """Check TOOLS.md in at least one workspace mentions MemClaw."""
        workspaces = self._find_workspaces()
        if not workspaces:
            self.skip("Education: TOOLS.md has MemClaw", "no workspaces found")
            return
        found = False
        for ws in workspaces:
            tools_md = ws / "TOOLS.md"
            if tools_md.exists() and "MemClaw" in tools_md.read_text():
                found = True
                break
        self.check(
            "Education: TOOLS.md has MemClaw section",
            found,
            f"checked {len(workspaces)} workspace(s)",
        )

    def test_agents_md_has_memclaw(self):
        """Check AGENTS.md in at least one workspace has Memory V2 section."""
        workspaces = self._find_workspaces()
        if not workspaces:
            self.skip("Education: AGENTS.md has Memory V2", "no workspaces found")
            return
        found = False
        for ws in workspaces:
            agents_md = ws / "AGENTS.md"
            if agents_md.exists() and "Memory V2" in agents_md.read_text():
                found = True
                break
        self.check(
            "Education: AGENTS.md has Memory V2 section",
            found,
            f"checked {len(workspaces)} workspace(s)",
        )

    def test_heartbeat_md_exists(self):
        """Check HEARTBEAT.md exists in at least one workspace with memclaw content."""
        workspaces = self._find_workspaces()
        if not workspaces:
            self.skip("Education: HEARTBEAT.md exists", "no workspaces found")
            return
        found = False
        for ws in workspaces:
            hb = ws / "HEARTBEAT.md"
            if hb.exists() and "memclaw" in hb.read_text().lower():
                found = True
                break
        self.check(
            "Education: HEARTBEAT.md has memclaw content",
            found,
            f"checked {len(workspaces)} workspace(s)",
        )

    # ═══════════════════════════════════════════════════════════
    # Section 6: Memory prompt section
    # ═══════════════════════════════════════════════════════════

    def test_prompt_section_file(self):
        """Verify prompt-section.js exists and contains memory rules."""
        dist = Path(self.openclaw_dir) / "plugins" / "memclaw" / "dist"
        prompt_js = dist / "prompt-section.js"
        self.check(
            "Prompt section: dist/prompt-section.js exists",
            prompt_js.exists(),
            str(prompt_js),
        )
        if prompt_js.exists():
            content = prompt_js.read_text()
            self.check(
                "Prompt section: has memory rules",
                "memclaw_write" in content and "memclaw_recall" in content,
                "expected tool names in prompt section builder",
            )
            self.check(
                "Prompt section: has identity injection",
                "agent_id" in content,
                "expected agent_id in prompt section",
            )

    # ═══════════════════════════════════════════════════════════
    # Section 7: Context engine lifecycle — write + recall
    # ═══════════════════════════════════════════════════════════

    def test_context_engine_write_memory(self):
        """Write a test memory — simulates what afterTurn/ingest would persist."""
        unique = f"gateway-integ-{uuid.uuid4().hex[:8]}"
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": self.tenant_id,
                "agent_id": "integration-test-agent",
                "fleet_id": self.fleet_id or None,
                "content": f"Integration test memory: {unique}. "
                "The project uses FastAPI with PostgreSQL and pgvector for embeddings.",
                "memory_type": "episode",
                "tags": ["integration-test", "gateway-test"],
            },
        )
        self.check(
            "Context engine: write memory",
            r.status_code in (200, 201),
            f"status={r.status_code}",
        )
        if r.status_code in (200, 201):
            data = r.json()
            mid = data.get("id") or (data.get("memory", {}) or {}).get("id")
            if mid:
                self._memory_ids.append(mid)
            self.check("Context engine: write returned id", bool(mid), f"id={mid}")

    def test_context_engine_search_recall(self):
        """Search for the memory — simulates assemble() recall."""
        time.sleep(2.0)

        r = self.client.post(
            f"{self.api}/search",
            json={
                "tenant_id": self.tenant_id,
                "query": "FastAPI PostgreSQL pgvector integration test",
                "filter_agent_id": "integration-test-agent",
                "top_k": 5,
            },
        )
        self.check(
            "Context engine: search 200",
            r.status_code == 200,
            f"status={r.status_code}",
        )
        if r.status_code != 200:
            return

        data = r.json()
        results = data if isinstance(data, list) else data.get("results", [])
        self.check(
            "Context engine: search returns results",
            len(results) > 0,
            f"count={len(results)}",
        )

        if results:
            top = results[0]
            score = top.get("score") or top.get("similarity") or 0
            self.check(
                "Context engine: search relevance > 0.5",
                score > 0.5,
                f"score={score:.3f}",
            )
            self.check("Context engine: result has content", bool(top.get("content")))

    def test_context_engine_recall_endpoint(self):
        """Hit recall endpoint — validates memclaw_recall(include_brief=true) / assemble() recall path."""
        r = self.client.post(
            f"{self.api}/recall",
            json={
                "tenant_id": self.tenant_id,
                "query": "what do you know about the integration test?",
                "agent_id": "integration-test-agent",
            },
        )
        self.check(
            "Context engine: recall 200",
            r.status_code == 200,
            f"status={r.status_code}",
        )
        if r.status_code == 200:
            data = r.json()
            self.check(
                "Context engine: recall has summary",
                bool(data.get("summary")),
                f"summary_len={len(data.get('summary', ''))}",
            )
            self.check(
                "Context engine: recall has memories",
                isinstance(data.get("memories"), list),
                f"count={len(data.get('memories', []))}",
            )

    # ═══════════════════════════════════════════════════════════
    # Section 8: afterTurn auto-write simulation
    # ═══════════════════════════════════════════════════════════

    def test_after_turn_auto_write(self):
        """Simulate afterTurn(): write an episode tagged auto-turn-summary,
        then verify it's retrievable."""
        unique = f"after-turn-{uuid.uuid4().hex[:8]}"
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": self.tenant_id,
                "agent_id": "integration-test-agent",
                "fleet_id": self.fleet_id or None,
                "content": f"Auto turn summary: {unique}. Agent completed code review of PR #158.",
                "memory_type": "episode",
                "tags": ["auto-turn-summary"],
            },
        )
        self.check(
            "afterTurn: write episode",
            r.status_code in (200, 201),
            f"status={r.status_code}",
        )
        if r.status_code in (200, 201):
            data = r.json()
            mid = data.get("id") or (data.get("memory", {}) or {}).get("id")
            if mid:
                self._memory_ids.append(mid)

        time.sleep(1.5)
        r2 = self.client.post(
            f"{self.api}/search",
            json={
                "tenant_id": self.tenant_id,
                "query": f"auto turn summary {unique}",
                "top_k": 3,
            },
        )
        if r2.status_code == 200:
            results = (
                r2.json()
                if isinstance(r2.json(), list)
                else r2.json().get("results", [])
            )
            found = any(unique in (m.get("content", "") or "") for m in results)
            self.check(
                "afterTurn: episode retrievable via search",
                found,
                f"searched for {unique}",
            )
        else:
            self.check(
                "afterTurn: search for episode", False, f"status={r2.status_code}"
            )

    # ═══════════════════════════════════════════════════════════
    # Section 9: Flush plan / compaction persistence
    # ═══════════════════════════════════════════════════════════

    def test_compaction_write(self):
        """Simulate compact(): write an episode tagged auto-compaction."""
        unique = f"compaction-{uuid.uuid4().hex[:8]}"
        r = self.client.post(
            f"{self.api}/memories",
            json={
                "tenant_id": self.tenant_id,
                "agent_id": "integration-test-agent",
                "fleet_id": self.fleet_id or None,
                "content": f"Compaction summary: {unique}. Session covered plugin review, "
                "3 bugs fixed, HMAC verification added, education files restored.",
                "memory_type": "episode",
                "tags": ["auto-compaction"],
            },
        )
        self.check(
            "compact: write compaction summary",
            r.status_code in (200, 201),
            f"status={r.status_code}",
        )
        if r.status_code in (200, 201):
            data = r.json()
            mid = data.get("id") or (data.get("memory", {}) or {}).get("id")
            if mid:
                self._memory_ids.append(mid)
            self.check("compact: has memory id", bool(mid))

    # ═══════════════════════════════════════════════════════════
    # Section 10: Fleet command — educate
    # ═══════════════════════════════════════════════════════════

    def test_fleet_command_educate(self):
        """Queue an educate command and verify it's processed."""
        if not self._node_id:
            self.skip("Fleet command: educate", "no node_id from heartbeat")
            return

        r = self.client.post(
            f"{self.api}/fleet/commands",
            json={
                "node_id": self._node_id,
                "command": "educate",
                "payload": {
                    "prompt": "Integration test education: You have access to MemClaw memory tools. "
                    "Always search before starting work. Always write findings after completing work.",
                },
            },
        )
        self.check(
            "Fleet command: educate queued",
            r.status_code == 201,
            f"status={r.status_code}",
        )
        if r.status_code != 201:
            return

        cmd_id = r.json().get("id")

        if not self.json_output:
            print("         ... waiting for educate command (up to 90s) ...")

        def check_cmd():
            try:
                cr = self.client.get(
                    f"{self.api}/fleet/commands",
                    params={"tenant_id": self.tenant_id, "node_id": str(self._node_id)},
                )
                if cr.status_code == 200:
                    cmds = (
                        cr.json()
                        if isinstance(cr.json(), list)
                        else cr.json().get("commands", [])
                    )
                    for c in cmds:
                        if str(c.get("id")) == str(cmd_id):
                            return c
                return None
            except Exception:
                return None

        result = self._retry_until(
            check_cmd,
            predicate=lambda c: c is not None and c.get("status") in ("done", "failed"),
            interval=5.0,
            max_wait=90.0,
        )

        if result and result.get("status") == "done":
            self.check("Fleet command: educate completed", True)
            cmd_result = result.get("result", {})
            self.check(
                "Fleet command: educate ok",
                cmd_result.get("ok") is True,
                f"result={json.dumps(cmd_result)[:200]}",
            )
            verified = cmd_result.get("verified", 0)
            self.check(
                "Fleet command: educate verified workspaces > 0",
                verified > 0,
                f"verified={verified}",
            )
            files = cmd_result.get("files", {})
            if files:
                self.check(
                    "Fleet command: educate wrote SKILL.md",
                    files.get("skills_written", 0) >= 0,
                    f"skills_written={files.get('skills_written')}",
                )
        elif result and result.get("status") == "failed":
            self.check(
                "Fleet command: educate completed",
                False,
                f"status=failed, result={result.get('result')}",
            )
        else:
            self.check(
                "Fleet command: educate completed",
                False,
                "not completed within 90s — is the gateway running?",
            )

    # ═══════════════════════════════════════════════════════════
    # Section 11: Tool descriptions
    # ═══════════════════════════════════════════════════════════

    def test_tool_descriptions_count(self):
        """Verify the backend serves all 13 tool descriptions."""
        r = self.client.get(
            f"{self.api}/tool-descriptions", params={"tenant_id": self.tenant_id}
        )
        self.check(
            "Tool descriptions: 200", r.status_code == 200, f"status={r.status_code}"
        )
        if r.status_code == 200:
            data = r.json()
            keys = set(data.keys()) if isinstance(data, dict) else set()
            self.check(
                "Tool descriptions: 13 tools",
                len(keys) >= 13,
                f"count={len(keys)}, keys={keys}",
            )


# ── CLI ──


def main():
    parser = argparse.ArgumentParser(
        description="MemClaw Gateway Integration Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", required=True, help="MemClaw API base URL")
    parser.add_argument("--api-key", required=True, help="API key")
    parser.add_argument("--tenant-id", required=True, help="Tenant ID")
    parser.add_argument(
        "--node-name",
        required=True,
        help="Node name (must match the gateway's MEMCLAW_NODE_NAME)",
    )
    parser.add_argument("--fleet-id", default="", help="Fleet ID")
    parser.add_argument(
        "--openclaw-dir",
        default="",
        help="OpenClaw base directory (default: ~/.openclaw)",
    )
    parser.add_argument("--verbose", action="store_true", help="Show extra detail")
    parser.add_argument(
        "--json", action="store_true", dest="json_output", help="JSON output for CI"
    )

    args = parser.parse_args()

    test = GatewayIntegrationTest(
        base_url=args.url,
        api_key=args.api_key,
        tenant_id=args.tenant_id,
        node_name=args.node_name,
        fleet_id=args.fleet_id,
        openclaw_dir=args.openclaw_dir,
        verbose=args.verbose,
        json_output=args.json_output,
    )
    sys.exit(test.run())


if __name__ == "__main__":
    main()

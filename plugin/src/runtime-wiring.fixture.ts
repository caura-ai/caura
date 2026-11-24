/** Real runtime modules, synthetic service and isolated on-disk state. */
import {BROKER_PRELOAD} from "./install-agent-auth.fixture.js";
export const WIRING_PRELOAD = BROKER_PRELOAD + String.raw`
import cp from 'node:child_process';
import {createHash} from 'node:crypto';
cp.execSync = () => {throw new Error('fixture_process_execution_refused');};
cp.exec = (...args) => {const cb = args.at(-1); if (typeof cb === 'function') cb(new Error('fixture_process_execution_refused'), '', ''); return {on(){}, kill(){}};};
syncBuiltinESMExports();
const delegate = globalThis.fetch, timer = globalThis.setTimeout;
globalThis.setTimeout = (fn, ms, ...args) => {const t = timer(fn, ms, ...args); if (ms >= 5000) t.unref(); return t;};
globalThis.setInterval = () => ({unref(){}});
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  if (url.pathname.includes('/admin/')) return delegate(input, init);
  fs.appendFileSync(process.env.BROKER_TRACE, JSON.stringify({url: url.href, method: init.method || 'GET',
    headers: Object.fromEntries(new Headers(init.headers)), redirect: init.redirect,
    body: init.body ? JSON.parse(String(init.body)) : null}) + '\n');
  if (url.origin !== 'https://broker.example.test') throw new Error('fixture_wrong_origin');
  if (url.pathname.endsWith('/whoami')) return Response.json({tenant_id: 'tenant-a', agent_id: 'agent-a',
    auth_source: 'gateway-header', via_gateway: true, key_kind: 'agent_key', ...plan.manualReply}, {status: plan.whoamiStatus || 200});
  if (plan.publicStatus && ['/tool-descriptions','/plugin-manifest','/plugin-source'].some(p => url.pathname.endsWith(p)))
    return Response.json({detail: 'public_route_refused'}, {status: plan.publicStatus});
  if (url.pathname.endsWith('/tool-descriptions')) return Response.json({});
  if (url.pathname.endsWith('/plugin-manifest') && plan.manifestStatus) return Response.json({}, {status: plan.manifestStatus});
  if (url.pathname.endsWith('/plugin-manifest')) return Response.json({version: '2.29.0',
    src_files: ['index.ts'], root_files: ['openclaw.plugin.json'],
    content_hash: createHash('sha256').update('export {};\n{}').digest('hex')});
  if (url.pathname.endsWith('/plugin-source')) return new Response(url.searchParams.get('file') === 'index.ts' ? 'export {};\n' : '{}');
  if (url.pathname.endsWith('/fleet/heartbeat')) return Response.json({node_id: '55555555-5555-4555-8555-555555555555',
    commands: plan.update ? [{id: '66666666-6666-4666-8666-666666666666', command: 'update_plugin', payload: {}}] : []});
  if (url.pathname.endsWith('/skills/installable')) return Response.json({delivery_version: 1,
    node_id: '55555555-5555-4555-8555-555555555555', complete: true, prune_allowed: false, documents: [], held: []});
  return Response.json(plan.resourceStatus ? {detail: 'agent_key_revoked'} :
    {ok: true, id: 'fixture-memory', items: [], memories: []}, {status: plan.resourceStatus || 200});
};
`;
export const WIRING_RUNNER = String.raw`
const action = process.env.WIRING_ACTION, request = JSON.parse(process.env.WIRING_REQUEST);
let result; const diagnostics = [];
for (const level of ['warn','error']) console[level] = (...values) => diagnostics.push(values.map(String).join(' '));
try {
  const env = await import(process.env.WIRING_DIST + '/env.js');
  env.applyPluginConfig(JSON.parse(process.env.WIRING_CONFIG || '{}'));
  const transport = await import(process.env.WIRING_DIST + '/transport.js');
  if (action === 'api' || action === 'twice' || action === 'public-then-agent') {
    if (action === 'public-then-agent') await env.apiFetch('none', 'GET', request.publicPath);
    const call = () => transport.apiCall(request.method || 'POST', request.path || '/search',
      request.body, undefined, undefined, request.agentId, request.extraHeaders);
    const invoke = () => process.env.CAURA_AUTH_MODE === 'manual' ? transport.withAgentAuthority(request.agentId, call) : call();
    if (action === 'twice') {try {await invoke();} catch {}}
    await invoke();
  } else if (action === 'node') {
    if (!transport.nodeApiCall) throw new Error('explicit_node_call_unavailable');
    await transport.nodeApiCall('POST', '/skills/installations', {tenant_id: 'tenant-a', node_id: '55555555-5555-4555-8555-555555555555', reporting_version: 1, events: []});
  } else if (action === 'tenant') await env.ensureTenantId();
  else if (action === 'descriptions') await env.fetchToolDescriptions();
  else if (action === 'heartbeat' || action === 'update') {
    await (await import(process.env.WIRING_DIST + '/heartbeat.js')).sendHeartbeat();
    for (let i = 0; i < 25; i++) await new Promise(r => setImmediate(r));
  } else if (action === 'tool') {
    const {createToolFromSpec} = await import(process.env.WIRING_DIST + '/tool-definitions.js');
    await createToolFromSpec(request.tool || 'caura_recall', {sessionKey: 'agent:agent-b:direct:test'})
      .execute('fixture-tool', request.params || {query: 'fixture query'}, undefined);
  } else if (action === 'bootstrap') {
    const {CauraContextEngine} = await import(process.env.WIRING_DIST + '/context-engine.js');
    const engine = new CauraContextEngine({agentId: 'agent-a'});
    await Promise.all([engine.bootstrap(), engine.bootstrap()]); await new CauraContextEngine({agentId: 'agent-a'}).bootstrap();
  } else if (action === 'assemble') {
    const assert = (await import('node:assert/strict')).default, health = await import(process.env.WIRING_DIST + '/health.js');
    const {CauraContextEngine} = await import(process.env.WIRING_DIST + '/context-engine.js');
    const engine = new CauraContextEngine({agentId: 'agent-a'}); await engine.bootstrap(); health.markReachable();
    const before = health.getReachability();
    for (let i = 0; i < 2; i++) {
      const out = await engine.assemble({messages: [], prompt: 'Remember the previous implementation decision', sessionKey: 'agent:agent-a:direct:test', tokenBudget: 4000});
      assert.deepEqual(out.messages, []); assert.match(out.systemPromptAddition, /Your identity/);
    }
    assert.equal(await (await import(process.env.WIRING_DIST + '/keystones.js')).fetchKeystonesBlock({agentId:'other',fleetId:'fleet-a'}), '');
    assert.deepEqual(health.getReachability(), before);
  } else if (action === 'register') {
    const mod = await import(process.env.WIRING_DIST + '/index.js');
    mod.default.register({pluginConfig: JSON.parse(process.env.WIRING_CONFIG || '{}'),
      registerTool(){},registerGatewayMethod(){},registerMemoryPromptSection(){},registerMemoryFlushPlan(){},
      registerMemoryRuntime(){},registerContextEngine(){},on(){}});
    for (let i = 0; i < 25; i++) await new Promise(r => setImmediate(r));
  }
  result = {ok: true, diagnostics};
} catch (e) {result = {ok: false, code: e.message, diagnostics};}
console.log('WIRING_RESULT:' + JSON.stringify(result));
`;

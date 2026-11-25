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
if (plan.recoverBoot) {
  let now = Date.now(); Date.now = () => now;
  globalThis.fixtureWaits = [];
  globalThis.setTimeout = (fn, ms, ...args) => ms >= 1000 && ms <= 60_000 ?
    timer(() => {now += ms; globalThis.fixtureWaits.push(ms); fn(...args);}, 0) : timer(fn, ms, ...args);
}
let firstContextFailed = false, contextCalls = 0;
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  if (url.pathname.endsWith('/admin/agent-connect-context')) contextCalls++;
  if (url.pathname.endsWith('/admin/agent-connect-context') && contextCalls > 1 && plan.postInitFailure && !globalThis.fixtureContextHealthy) {
    fs.appendFileSync(process.env.BROKER_TRACE, JSON.stringify({url: url.href, method: 'GET', headers: {}}) + '\n');
    if (plan.postInitFailure === 'network') throw new TypeError('fixture network failure');
    return Response.json({detail: 'fixture unavailable'}, {status: plan.postInitFailure});
  }
  if (url.pathname.endsWith('/admin/agent-connect-context') && plan.startFailure && !firstContextFailed) {
    firstContextFailed = true;
    if (typeof plan.startFailure === 'string') {
      fs.appendFileSync(process.env.BROKER_TRACE, JSON.stringify({url: url.href, method: 'GET', headers: {}}) + '\n');
      throw plan.startFailure === 'timeout' ? new DOMException('fixture timeout', 'TimeoutError') : new TypeError('fixture network failure');
    }
    plan.contextStatus = plan.startFailure;
    try {return await delegate(input, init);} finally {delete plan.contextStatus;}
  }
  if (url.pathname.includes('/admin/')) {
    if (plan.pauseWarm && (plan.pauseWarm === 'context' ? contextCalls > 1 && url.pathname.endsWith('/agent-connect-context') : url.pathname.includes('/api-credentials/'))) {
      await new Promise((resolve, reject) => {
        const t = timer(resolve, 200);
        init.signal?.addEventListener('abort', () => {
          clearTimeout(t); fs.appendFileSync(process.env.BROKER_TRACE, JSON.stringify({url: url.href, aborted: true}) + '\n');
          reject(new DOMException('fixture aborted', 'AbortError'));
        }, {once: true});
      });
    }
    if (url.pathname.endsWith('/33333333-3333-4333-8333-333333333333')) {
      plan.existing = {id: '33333333-3333-4333-8333-333333333333', home_agent_id: 'agent-b'};
      try {return await delegate(input, init);} finally {delete plan.existing;}
    }
    return delegate(input, init);
  }
  if (init.signal?.aborted) throw init.signal.reason;
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
  if (globalThis.fixtureResourceStatus)
    return Response.json({detail: 'agent_key_revoked'}, {status: globalThis.fixtureResourceStatus});
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
  } else if (action === 'tenant-fast-failure') {
    const assert = (await import('node:assert/strict')).default, fs = await import('node:fs');
    let now = Date.now(); Date.now = () => now;
    const waits = [], timer = globalThis.setTimeout;
    globalThis.setTimeout = (fn, ms, ...args) => {
      if (ms === 30_000) {waits.push(() => fn(...args)); return {unref(){}};}
      return timer(fn, ms, ...args);
    };
    const flush = async () => {for (let i = 0; i < 25; i++) await new Promise(r => setImmediate(r));};
    const contexts = () => fs.readFileSync(process.env.BROKER_TRACE, 'utf8').trim().split('\n')
      .map(JSON.parse).filter(r => r.url.endsWith('/admin/agent-connect-context')).length;
    await env.initializeRuntimeAuth();
    if (request.register) {
      const mod = await import(process.env.WIRING_DIST + '/index.js');
      mod.default.register({registerTool(){},registerGatewayMethod(){},registerMemoryPromptSection(){},
        registerMemoryFlushPlan(){},registerMemoryRuntime(){},registerContextEngine(){},on(){}});
      await flush();
      assert.equal(contexts(), 2, 'startup performs one failed resolution after successful initialization');
      assert.equal(waits.length, 1, 'startup alone schedules recovery');
    }
    const before = contexts();
    let settled = 0;
    const hooks = [env.ensureTenantId(), env.ensureTenantId()].map(p => p.then(
      () => assert.fail('outage must refuse tenant resolution'), () => {settled++;}));
    await flush();
    assert.equal(contexts(), request.register ? before : before + 1, 'hooks share resolution and its cooldown');
    assert.equal(settled, 2, 'lifecycle hooks must refuse without waiting for the 30-second startup timer');
    await Promise.all(hooks);
    assert.equal(waits.length, request.register ? 1 : 0, 'hooks never schedule another retry loop');
    await assert.rejects(env.ensureTenantId());
    assert.equal(contexts(), 2, 'another hook within cooldown makes no request');
    globalThis.fixtureContextHealthy = true; now += 30_001;
    assert.equal(await env.ensureTenantId(), 'tenant-a', 'rejected resolution is retried after cooldown');
    assert.equal(contexts(), 3, 'recovery makes one fresh context check');
    if (request.register) {waits.shift()(); await flush();}
  } else if (action === 'signal-coalesce') {
    const assert = (await import('node:assert/strict')).default;
    const broker = await import(process.env.WIRING_DIST + '/install-agent-auth.js');
    await env.initializeRuntimeAuth();
    const a = new AbortController(), b = new AbortController(), fetch = globalThis.fetch;
    let release, contexts = 0, firstState;
    const gate = new Promise(resolve => {release = resolve;});
    globalThis.fetch = async (input, init) => {
      if (String(input).endsWith('/admin/agent-connect-context')) {contexts++; await gate;}
      return fetch(input, init);
    };
    const call = signal => broker.resolveInstallAgentKey({gateway: 'https://broker.example.test', apiPrefix: '/api/v1',
      orgId: 'org-a', tenantId: 'tenant-a', installUuid: '11111111-1111-4111-8111-111111111111',
      agentId: 'agent-a', capabilities: ['read','write'], signal});
    const first = call(a.signal).then(key => {firstState = true; return key;}, e => {firstState = false; return e;});
    const second = call(b.signal);
    const flush = async () => {for (let i = 0; i < 10; i++) await new Promise(r => setImmediate(r));};
    await flush();
    assert.equal(contexts, 1, 'same-agent callers with separate signals share the context check');
    if (request.cancel) {
      a.abort(); await flush();
      assert.equal(firstState, false, 'a cancelled waiter settles without waiting for its sibling');
      assert.equal(b.signal.aborted, false);
    }
    release();
    assert.equal(await second, 'fixture_child_key', 'one cancellation cannot refuse the surviving waiter');
    if (!request.cancel) assert.equal(await first, 'fixture_child_key');
    assert.equal(contexts, 1, 'cold waiters never queue a redundant resolution behind the mutation lock');
  } else if (action === 'reuse-window') {
    const assert = (await import('node:assert/strict')).default, fs = await import('node:fs');
    const baseNow = Date.now(); let elapsed = 0; Date.now = () => baseNow + elapsed;
    const calls = () => fs.readFileSync(process.env.BROKER_TRACE, 'utf8').trim().split('\n').map(JSON.parse);
    const admins = () => calls().filter(r => r.url.includes('/admin/')).length;
    const call = () => transport.apiCall('POST', '/search', {}, undefined, undefined, 'agent-a');
    await call(); const before = admins();
    if (request.event === 'reuse') {
      elapsed = 299_999; await call();
      assert.equal(admins(), before, 'reuse before five minutes makes no admin call');
    } else if (request.event === 'held' || request.event === 'expired-key') {
      if (request.event === 'held') {
        const path = process.env.BROKER_AUTHORITY, j = JSON.parse(fs.readFileSync(path, 'utf8'));
        fs.writeFileSync(path, JSON.stringify({...j, status: 'held'}));
      } else elapsed = 60_001;
      const count = calls().length; await assert.rejects(call());
      assert.equal(calls().length, count, 'held authority or expired key is refused locally inside the window');
    } else {
      if (request.event === 'elapsed') elapsed = 300_001;
      else if (request.event === 'authority') {
        const path = process.env.BROKER_AUTHORITY, j = JSON.parse(fs.readFileSync(path, 'utf8'));
        fs.writeFileSync(path, JSON.stringify({...j, install_credential: 'fixture_replacement_install'}));
      } else {
        globalThis.fixtureResourceStatus = request.event;
        await assert.rejects(call()); globalThis.fixtureResourceStatus = undefined;
      }
      const refresh = admins(); await call();
      assert.equal(admins(), refresh + 2, 'expiry, authority change or refusal requires both live checks');
      const checked = admins(); await call();
      assert.equal(admins(), checked, 'a refreshed window immediately reuses the ready key');
    }
    assert.ok(calls().every(r => r.method !== 'POST' || !r.url.includes('/admin/')));
    assert.ok(calls().filter(r => r.url.endsWith('/search')).every(r => r.headers['x-api-key'] === 'fixture_child_key'));
  } else if (action === 'warm-broker' || action === 'wait-broker' || action === 'abort-warm') {
    const assert = (await import('node:assert/strict')).default, fs = await import('node:fs');
    const broker = await import(process.env.WIRING_DIST + '/install-agent-auth.js');
    const options = {gateway: 'https://broker.example.test', apiPrefix: '/api/v1', orgId: 'org-a', tenantId: 'tenant-a',
      installUuid: '11111111-1111-4111-8111-111111111111', agentId: 'agent-a', capabilities: ['read','write']};
    if (action === 'abort-warm') {
      await env.initializeRuntimeAuth();
      const controller = new AbortController(), abort = setTimeout(() => controller.abort(), 10);
      try {await assert.rejects(transport.apiCall('POST', '/search', {}, undefined, controller.signal, 'agent-a'));}
      finally {clearTimeout(abort);}
      const calls = fs.readFileSync(process.env.BROKER_TRACE, 'utf8').trim().split('\n').map(JSON.parse);
      assert.ok(calls.some(r => r.aborted), 'caller abort must reach the warm admin request');
      assert.ok(calls.every(r => !r.url.endsWith('/search')));
    } else {
      if (action === 'wait-broker') {
        fs.writeFileSync(process.env.BROKER_CACHE + '.lock', 'fixture legitimate writer', {mode: 0o600});
        setTimeout(() => fs.unlinkSync(process.env.BROKER_CACHE + '.lock'), 250);
      }
      const agents = request.concurrent ? ['agent-a','agent-b'] : ['agent-a'];
      const keys = await Promise.all(agents.map(agentId => broker.resolveInstallAgentKey({...options, agentId})));
      assert.ok(keys.every(key => key === 'fixture_child_key'));
    }
  } else if (action === 'recover-start') {
    const assert = (await import('node:assert/strict')).default;
    const fs = await import('node:fs'), baseNow = Date.now(); let elapsed = 0;
    Date.now = () => baseNow + elapsed;
    const contexts = () => fs.readFileSync(process.env.BROKER_TRACE, 'utf8').trim().split('\n')
      .map(JSON.parse).filter(r => r.url.endsWith('/admin/agent-connect-context')).length;
    const first = await Promise.allSettled([env.initializeRuntimeAuth(), env.ensureTenantId(), env.fetchToolDescriptions()]);
    assert.ok(first.every(r => r.status === 'rejected')); assert.equal(contexts(), 1);
    await assert.rejects(env.initializeRuntimeAuth()); assert.equal(contexts(), 1);
    elapsed = 29_999;
    await assert.rejects(env.initializeRuntimeAuth()); assert.equal(contexts(), 1);
    elapsed = 30_001;
    await Promise.all([env.initializeRuntimeAuth(), env.initializeRuntimeAuth()]);
    assert.equal(contexts(), 2, 'one new startup probe after cooldown');
    await transport.nodeApiCall('GET', '/whoami');
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
  } else if (action === 'startup') {
    const assert = (await import('node:assert/strict')).default;
    const health = await import(process.env.WIRING_DIST + '/health.js');
    if (request.reachability === 'reachable') health.markReachable();
    if (request.reachability === 'unreachable') health.markUnreachable('fixture prior outage');
    const before = health.getReachability();
    const {CAURA_TOOLS} = await import(process.env.WIRING_DIST + '/tools.js');
    const {PLUGIN_ID} = await import(process.env.WIRING_DIST + '/config.js');
    const configPath = process.env.HOME + '/.openclaw/openclaw.json';
    const config = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    config.tools = {alsoAllow: CAURA_TOOLS};
    config.plugins = {slots: {contextEngine: PLUGIN_ID}};
    fs.writeFileSync(configPath, JSON.stringify(config));
    const mod = await import(process.env.WIRING_DIST + '/index.js');
    const api = {pluginConfig: JSON.parse(process.env.WIRING_CONFIG || '{}'),
      registerTool(){},registerGatewayMethod(){},registerMemoryPromptSection(){},registerMemoryFlushPlan(){},
      registerMemoryRuntime(){},registerContextEngine(){},on(){}};
    mod.default.register(api); mod.default.register(api);
    for (let i = 0; i < 25; i++) await new Promise(r => setImmediate(r));
    const {CauraContextEngine} = await import(process.env.WIRING_DIST + '/context-engine.js');
    await new CauraContextEngine({agentId: 'agent-a'}).bootstrap();
    await new CauraContextEngine({agentId: 'agent-a'}).bootstrap();
    await env.fetchToolDescriptions().catch(() => {});
    await env.ensureTenantId().catch(() => {});
    for (let i = 0; i < 25; i++) await new Promise(r => setImmediate(r));
    assert.deepEqual(health.getReachability(), before);
  } else if (action === 'register' || action === 'register-recovery') {
    const mod = await import(process.env.WIRING_DIST + '/index.js');
    mod.default.register({pluginConfig: JSON.parse(process.env.WIRING_CONFIG || '{}'),
      registerTool(){},registerGatewayMethod(){},registerMemoryPromptSection(){},registerMemoryFlushPlan(){},
      registerMemoryRuntime(){},registerContextEngine(){},on(){}});
    for (let i = 0; i < 25; i++) await new Promise(r => setImmediate(r));
    if (action === 'register-recovery') {
      const assert = (await import('node:assert/strict')).default, fs = await import('node:fs');
      await new Promise(r => setTimeout(r, 50));
      const calls = fs.readFileSync(process.env.BROKER_TRACE, 'utf8').trim().split('\n').map(JSON.parse);
      assert.ok(calls.some(r => r.url.endsWith('/fleet/heartbeat')), 'heartbeat starts after managed recovery');
      assert.ok(globalThis.fixtureWaits.some(ms => ms >= 30_000), 'managed startup retries respect the cooldown');
    }
  }
  result = {ok: true, diagnostics};
} catch (e) {result = {ok: false, code: e.message, diagnostics};}
console.log('WIRING_RESULT:' + JSON.stringify(result));
`;

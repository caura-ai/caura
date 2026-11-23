/** Synthetic authority boundary; never contacts a deployed service. */
import {BROKER_PRELOAD} from "./install-agent-auth.fixture.js";
export const AUTH_PRELOAD = BROKER_PRELOAD + String.raw`
const brokerFetch = globalThis.fetch;
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(String(input));
  if (url.pathname !== '/api/v1/whoami') return brokerFetch(input, init);
  if (url.origin !== 'https://broker.example.test') throw new Error('fixture_wrong_origin');
  fs.appendFileSync(process.env.BROKER_TRACE, JSON.stringify({url: url.href,
    method: init.method || 'GET', headers: Object.fromEntries(new Headers(init.headers)),
    redirect: init.redirect, bounded: deadlines.get(init.signal) || 0}) + '\n');
  const value = {auth_source: 'gateway-header', via_gateway: true, key_kind: 'agent_key',
    tenant_id: 'tenant-a', agent_id: 'agent-a', ...plan.manualReply};
  for (const key of plan.omitManual || []) delete value[key];
  return Response.json(value, {status: plan.whoamiStatus || 200});
};
`;

/** Missing implementation becomes an assertion failure, never a loader crash. */
export const AUTH_RUNNER = String.raw`
import {existsSync} from 'node:fs';
const config = JSON.parse(process.env.AUTH_CONFIG), request = JSON.parse(process.env.AUTH_REQUEST);
const action = process.env.AUTH_ACTION;
let result;
try {
  if (!existsSync(new URL(process.env.AUTH_MODULE))) throw new Error('runtime_auth_unavailable');
  const mod = await import(process.env.AUTH_MODULE);
  if (action === 'import') {mod.createRuntimeAuth(config); result = {ok: true};}
  else if (action === 'settings') result = {ok: true, settings: mod.RUNTIME_AUTH_SETTINGS};
  else {
    let auth = mod.createRuntimeAuth(config);
    await auth.initialize(); // Explicit startup probe; create/import must remain inert.
    if (action === 'hold' || action === 'reinitialize' || action === 'restart') {
      auth.observeResponse(Number(process.env.AUTH_STATUS));
      if (action === 'restart') {auth = mod.createRuntimeAuth(config); await auth.initialize();}
      if (action === 'reinitialize') await auth.initialize();
    }
    const value = await auth.authorize(request), h = new Headers(value.headers);
    const expected = process.env.AUTH_EXPECTED;
    result = {ok: true, matched: expected === 'none' ? !h.has('x-api-key') :
      h.get('x-api-key') === ({install: 'fixture_install_key', agent: 'fixture_child_key',
        manual: 'fixture_manual_key', standalone: 'fixture_tenant_key'})[expected],
      identityHeaders: [...h.keys()].filter(k => /^(?:x-(?:tenant-id|agent-id|install-uuid|gateway-secret|caura-)|authorization)/i.test(k)),
      harmlessHeader: h.get('x-bulk-attempt-id'), tenantId: value.tenantId};
  }
} catch (e) {result = {ok: false, code: e.message};}
console.log(JSON.stringify(result));
`;

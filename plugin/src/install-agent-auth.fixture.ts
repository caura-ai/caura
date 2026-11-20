/** Isolated, synthetic install/credential boundary; never calls a live server. */
export const BROKER_PRELOAD = String.raw`
import fs from 'node:fs';
import os from 'node:os';
import {syncBuiltinESMExports} from 'node:module';
const plan = JSON.parse(process.env.BROKER_PLAN || '{}');
if (plan.hostname) os.hostname = () => plan.hostname;
const cache = process.env.BROKER_CACHE;
const changeAuthority = () => plan.authorityPatch ? fs.writeFileSync(process.env.BROKER_AUTHORITY,
  JSON.stringify({...JSON.parse(fs.readFileSync(process.env.BROKER_AUTHORITY, 'utf8')), ...plan.authorityPatch})) :
  fs.writeFileSync(process.env.BROKER_AUTHORITY + '.lock', 'fixture enrollment writer', {mode: 0o600});
const read = () => fs.existsSync(cache) ? JSON.parse(fs.readFileSync(cache, 'utf8')) : null;
const rename = fs.renameSync, fsync = fs.fsyncSync;
let readyRenamed = false, pendingRenamed = false;
fs.renameSync = function(src, dest) {
  let entry;
  if (String(dest) === cache) {
    entry = JSON.parse(fs.readFileSync(src, 'utf8')).entries?.['agent-a'];
    if ((plan.failPendingSave && entry?.status === 'pending') ||
        (plan.failReadySave && entry?.status === 'ready')) throw new Error('fixture_storage_error');
  }
  const result = rename.apply(this, arguments);
  if (entry?.status === 'ready') readyRenamed = true;
  if (entry?.status === 'pending') pendingRenamed = true;
  return result;
};
fs.fsyncSync = function(fd) {
  if (((plan.failReadyDirectorySync && readyRenamed) || (plan.failPendingDirectorySync && pendingRenamed)) &&
      fs.fstatSync(fd).isDirectory())
    throw new Error('fixture_storage_error');
  const result = fsync.apply(this, arguments);
  if (plan.authorityAt === 'saved' && readyRenamed && fs.fstatSync(fd).isDirectory()) changeAuthority();
  return result;
};
syncBuiltinESMExports();
const deadlines = new WeakMap(), timeout = AbortSignal.timeout.bind(AbortSignal);
AbortSignal.timeout = ms => {const signal = timeout(ms); deadlines.set(signal, ms); return signal;};
const base = '/api/v1/admin/orgs/org-a/api-credentials';
const row = (patch = {}) => ({
  id: '22222222-2222-4222-8222-222222222222', org_id: 'org-a', kind: 'agent_key',
  home_tenant_id: 'tenant-a', home_agent_id: 'agent-a', source_tenant_ids: [],
  read_all_org_tenants: false, capabilities: ['read', 'write'],
  issuing_install_uuid: '11111111-1111-4111-8111-111111111111', key_prefix: 'fixture_prefix',
  label: 'Fixture credential', expires_at: process.env.BROKER_EXPIRY, revoked_at: null,
  bootstrap_allowed: false, fleet_id: null, single_use_bootstrap: false,
  created_at: '2026-11-20T00:00:00Z', created_by: null, ...patch,
});
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(String(input)), method = init.method || 'GET';
  const body = init.body ? JSON.parse(String(init.body)) : null;
  const before = read();
  fs.appendFileSync(process.env.BROKER_TRACE, JSON.stringify({url: url.href, method, body,
    headers: Object.fromEntries(new Headers(init.headers)), redirect: init.redirect,
    bounded: deadlines.get(init.signal) || 0, before}) + '\n');
  if (url.origin !== 'https://broker.example.test') throw new Error('fixture_wrong_origin');
  const respond = (value, status = 200) => Response.json(value, {status});
  if (url.pathname === '/api/v1/admin/agent-connect-context') {
    if (plan.contextStatus) return respond({detail: 'fixture_server_body'}, plan.contextStatus);
    return respond({org_id: 'org-a', tenant_id: 'tenant-a',
      install_uuid: '11111111-1111-4111-8111-111111111111', hostname: os.hostname(), ...plan.context});
  }
  if (url.pathname === base && method === 'GET') {
    if (plan.authorityAt === 'catalog') changeAuthority();
    return plan.listStatus ? respond({detail: 'fixture_server_body'}, plan.listStatus) : respond({items: plan.catalog || []});
  }
  if (url.pathname.startsWith(base + '/') && method === 'GET') {
    if (plan.getStatus) return respond({detail: 'fixture_server_body'}, plan.getStatus);
    return respond(row(plan.existing));
  }
  if (method !== 'POST' || !(url.pathname === base || url.pathname.endsWith('/rotate')))
    throw new Error('fixture_forbidden_endpoint');
  if (plan.pauseMutation) {
    process.send('mutation-started');
    await new Promise(resolve => process.once('message', resolve));
  }
  if (plan.crashAfterMutation) process.exit(71);
  if (plan.lostResponse) throw new Error('fixture_server_body fixture_install_key fixture_child_key');
  if (plan.mutationStatus) return respond({detail: 'fixture_server_body'}, plan.mutationStatus);
  if (plan.hugeResponse) return new Response(' '.repeat(65537), {headers: {'Content-Length': '1'}});
  if (plan.invalidJson) return new Response('{');
  if (plan.authorityAt === 'response') changeAuthority();
  const rotated = url.pathname.endsWith('/rotate');
  return respond(row({label: body.label || 'Fixture credential',
    ...(rotated ? {id: '33333333-3333-4333-8333-333333333333',
      rotated_from_id: '22222222-2222-4222-8222-222222222222'} :
      before?.entries?.['agent-a']?.operation?.target_id ? {id: '33333333-3333-4333-8333-333333333333'} : {}),
    raw_key: rotated ? 'fixture_rotated_key' : 'fixture_child_key', ...plan.response}), rotated ? 200 : 201);
};
`;

/** The missing capability produces assertion failures, not a module-load error. */
export const BROKER_RUNNER = String.raw`
import {existsSync} from 'node:fs';
const entry = process.env.BROKER_MODULE;
let result;
try {
  if (!existsSync(new URL(entry))) throw new Error('broker_unavailable');
  const broker = await import(entry), options = JSON.parse(process.env.BROKER_OPTIONS);
  const action = process.env.BROKER_ACTION;
  if (action === 'import') result = {ok: true};
  else {
    const invoke = () => action === 'rotate' ? broker.rotateInstallAgentKey(options) :
      broker.resolveInstallAgentKey({...options, ...action === 'renew' ? {renew: true} : {}});
    const keys = action === 'coalesce' ? await Promise.all(Array.from({length: 4}, invoke)) : [await invoke()];
    result = {ok: true, matched: keys.every(key => key ===
      (action === 'rotate' || action === 'reuse-rotated' ? 'fixture_rotated_key' : 'fixture_child_key'))};
  }
} catch (e) { result = {ok: false, code: e.message}; }
console.log(JSON.stringify(result));
`;

/** Synthetic HTTP and crash boundaries for the enrollment subprocess tests. */
export const ENROLLMENT_PRELOAD = String.raw`
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import os, { hostname } from 'node:os';
const plan = JSON.parse(process.env.ENROLLMENT_TEST_PLAN);
if (plan.platform) Object.defineProperty(process, 'platform', {value: plan.platform});
if (plan.hostname) os.hostname = () => plan.hostname;
const readSync = fs.readSync;
let inputReads = 0;
fs.readSync = function(fd, buffer, offset, length, position) {
  if (fd === 0 && (plan.inputActions || plan.inputWaitForever)) {
    const action = plan.inputWaitForever ? 'EAGAIN' : plan.inputActions[inputReads++];
    if (typeof action === 'string' && action !== 'read') throw Object.assign(new Error('synthetic_input_error'), {code: action});
    if (typeof action === 'number') length = Math.min(length, action);
  }
  return readSync.call(this, fd, buffer, offset, length, position);
};
const journal = process.env.ENROLLMENT_TEST_JOURNAL;
const read = () => fs.existsSync(journal) ? JSON.parse(fs.readFileSync(journal, 'utf8')) : null;
const rename = fs.renameSync;
fs.renameSync = function(src, dest) {
  if (plan.failCredentialSave && String(dest) === journal &&
      JSON.parse(fs.readFileSync(src, 'utf8')).install_credential) {
    throw new Error('synthetic_persist_failure');
  }
  return rename.apply(this, arguments);
};
syncBuiltinESMExports();
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(String(input));
  const body = init.body ? JSON.parse(String(init.body)) : null;
  const before = read();
  fs.appendFileSync(process.env.ENROLLMENT_TEST_TRACE, JSON.stringify({
    url: url.href, method: init.method || 'GET',
    headers: Object.fromEntries(new Headers(init.headers)), body,
    redirect: init.redirect, bounded: Boolean(init.signal), before,
  }) + '\n');
  if (url.origin !== 'https://enrollment.example.test') throw new Error('unexpected_origin');
  if (url.pathname === '/api/v1/installs/register') {
    if (plan.lostResponse) throw new Error('synthetic_lost_response');
    if (plan.registerStatus) return new Response('fixture_server_body', {status: plan.registerStatus});
    const result = {install_credential: 'fixture_install_value', tenant_id: 'tenant-a',
      fleet_id: 'fleet-a', issued_at: new Date().toISOString(), expires_at: null,
      ...plan.registration};
    return new Response(plan.invalidJson ? '{' : JSON.stringify(result));
  }
  if (url.pathname === '/api/v1/admin/agent-connect-context') {
    if (plan.crashAtContext) process.exit(71);
    if (plan.contextStatus) return new Response('fixture_server_body', {status: plan.contextStatus});
    if (plan.hugeContext) return new Response(' '.repeat(65537));
    return Response.json({org_id: 'org-a', tenant_id: 'tenant-a',
      install_uuid: before?.install_uuid || '11111111-1111-4111-8111-111111111111',
      hostname: hostname(), ...plan.context});
  }
  throw new Error('unexpected_endpoint');
};
`;

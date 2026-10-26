const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../public/index.html'), 'utf8');
const statsCode = html.slice(html.indexOf('    function escapeHtml('), html.indexOf('    loadStats();'));
const adminCode = html.slice(html.indexOf('    async function adminRequest('), html.indexOf('    async function createApiKey('));

function fixture(fetch) {
  const elements = {};
  const errors = [];
  const context = vm.createContext({
    fetch,
    console: {error: (...args) => errors.push(args)},
    document: {getElementById: id => elements[id] ??= {innerText: '', innerHTML: '', style: {}, value: ''}},
    lucide: {createIcons() {}},
  });
  vm.runInContext(statsCode + adminCode, context);
  return {context, elements, errors};
}

(async () => {
  let checks = 0;
  for (const data of [null, {}, {processed_today: null, protected_files: null,
                                plan_used: null, plan_limit: null, recent_activities: null},
                      {processed_today: NaN, protected_files: Infinity, plan_used: 'invalid',
                       plan_limit: 0, recent_activities: [null]}]) {
    const {context, elements, errors} = fixture(async () => ({ok: true, json: async () => data}));
    await context.loadStats();
    for (const id of ['stat-today', 'stat-protected', 'stat-plan-text']) assert.equal(elements[id].innerText, '0');
    assert.equal(errors.length, 0);
    assert.ok(elements['activity-list'].innerHTML.includes('Nenhuma atividade'));
    checks++;
  }
  const normal = fixture(async () => ({ok: true, json: async () => ({
    processed_today: 12, protected_files: 1234, plan_used: 500, plan_limit: 0, recent_activities: [],
  })}));
  normal.elements['stat-plan-bar'] = {style: {}};
  await normal.context.loadStats();
  assert.equal(normal.elements['stat-protected'].innerText, '1.234');
  assert.equal(normal.elements['stat-plan-bar'].style.width, '0%');
  assert.equal(normal.errors.length, 0);
  checks++;

  for (const fetch of [async () => {throw new Error('offline');}, async () => ({ok: false, status: 429})]) {
    const {context, elements, errors} = fixture(fetch);
    await context.loadStats();
    assert.ok(elements['activity-list'].innerHTML.includes('role="alert"'));
    assert.equal(errors.length, 1);
    assert.equal(elements['stat-today'].innerText, '0');
    checks++;
  }
  let calls = [];
  const admin = fixture(async (url, options) => {
    calls.push({url, options});
    return {ok: true, status: 200};
  });
  await assert.rejects(admin.context.adminRequest('/api/auth/create-key'), /Informe sua chave/);
  assert.equal(calls.length, 0);
  admin.elements['admin-key'].value = 'test-admin-fixture';
  await admin.context.adminRequest('/api/auth/create-key', {method: 'POST'});
  assert.equal(calls[0].options.headers.Authorization, 'Bearer test-admin-fixture');
  assert.equal(calls[0].url.includes('test-admin-fixture'), false);
  checks++;
  const markup = '<img src=x onerror="document.body.dataset.auditXss=1">';
  const compliance = fixture(async () => ({json: async () => ({found_triggers: [], safe_rewrite: markup})}));
  compliance.context.FormData = FormData;
  compliance.elements['copy-input'] = {value: markup};
  vm.runInContext(html.slice(html.indexOf('    async function runComplianceScan('), html.indexOf('    async function ', html.indexOf('    async function runComplianceScan(') + 10)), compliance.context);
  await compliance.context.runComplianceScan();
  assert.ok(compliance.elements['compliance-result'].innerHTML.includes('&lt;img'));
  assert.equal(compliance.elements['compliance-result'].innerHTML.includes(markup), false);
  checks++;
  console.log(`${checks} cenarios de dashboard e autenticacao aprovados.`);
})().catch(error => {console.error(error); process.exitCode = 1;});

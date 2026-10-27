"""Static checks and Node DOM-double contract tests; not browser end-to-end tests."""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

STATIC = Path(__file__).resolve().parents[1] / "static"
HTML = (STATIC / "org.html").read_text(encoding="utf-8")
JS = (STATIC / "org.js").read_text(encoding="utf-8")


def test_org_static_security_and_accessibility():
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(", JS)
    assert not re.search(r"console\.|localStorage\.setItem|sessionStorage|navigator\.clipboard", JS)
    assert "textContent" in JS
    assert "['http:', 'https:'].includes(url.protocol)" in JS
    assert "link.rel = 'noopener noreferrer'" in JS
    assert "redirect: 'error'" in JS
    assert "Content-Security-Policy" in HTML
    assert "unsafe-inline" not in HTML
    assert not re.search(r"\son[a-z]+=", HTML)
    assert 'lang="zh-CN"' in HTML
    assert 'aria-live="polite"' in HTML
    assert 'id="confirm-check" type="checkbox" disabled' in HTML
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    assert len(ids) == len(set(ids))
    for target in re.findall(r'\bfor="([^"]+)"', HTML):
        assert target in ids
    assert 'src="org.js"' in HTML
    assert (STATIC / "css" / "org.css").exists()


HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const config = JSON.parse(process.argv[1]);
const ids = new Map();
const all = [];
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {};
    this.value = ''; this._text = ''; this.disabled = false; this.hidden = false;
    this.checked = false; this.required = false; this.dataset = {};
    this.classList = {toggle() {}};
    all.push(this);
  }
  set id(value) { this._id = value; ids.set(value, this); }
  get id() { return this._id; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(x => x.textContent || '').join(''); }
  set innerHTML(value) { throw new Error('unsafe HTML'); }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  querySelector(selector) {
    if (selector === 'button[type=submit]') return this.button;
    const tag = selector.toUpperCase();
    return this.children.find(x => x.tagName === tag) || null;
  }
  checkValidity() {
    this.validationMessage = '请检查此字段';
    return !(this.required && !this.value) &&
      !(this.minLength && this.value.length < this.minLength) &&
      !(this.maxLength && this.value.length > this.maxLength) &&
      !(this.min != null && Number(this.value) < this.min) &&
      !(this.max != null && Number(this.value) > this.max);
  }
  scrollIntoView() {}
}
const html = fs.readFileSync(config.html, 'utf8');
for (const match of html.matchAll(/<([\w-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
  const el = new Element(match[1]); el.id = match[2];
  el.disabled = /\sdisabled\b/.test(match[0]); el.hidden = /\shidden\b/.test(match[0]);
}
for (const id of ['search-form', 'draft-form', 'submit-form', 'project-config', 'cli-form']) {
  ids.get(id).button = new Element('button');
}
ids.get('draft-form').button = ids.get('save-draft');
ids.get('submit-form').button = ids.get('submit-application');
ids.get('cli-scope').value = 'read';
const body = new Element('body'); body.dataset.prefix = config.prefix;
global.document = {body, createElement: tag => new Element(tag), getElementById: id => ids.get(id)};
global.location = {pathname: config.path, search: '?request_id=cli-request-123456'};
global.window = {addEventListener() {}};
const secret = 'WEB_BEARER_MUST_NEVER_APPEAR_IN_DOM';
global.localStorage = {getItem: key => key === 'alethic_session' ? secret : null};
const calls = [];
const preview = {kind:'project-join', form_revision:'1', fields:{project_id:'p1', motivation:'A useful contribution', weekly_hours:3, notes:''}};
let failed = false;
let draftCount = 0;
let codeCount = 0;
function response(data, status=200) { return {ok: status < 400, status, json:async () => data}; }
global.fetch = async (url, options) => {
  assert.equal(options.headers.Authorization, 'Bearer ' + secret);
  assert.equal(options.redirect, 'error');
  assert.equal(options.credentials, 'omit');
  const path = new URL(url, 'https://example.test').pathname;
  calls.push({path, url, options});
  if (path.endsWith('/me')) return response({role:config.scenario === 'applicant' ? 'applicant' : config.scenario.startsWith('admin') ? 'admin' : 'member', principal_id:'pr1', display_name:'<img onerror=alert(1)>'});
  if (path.endsWith('/capabilities')) return response({scopes:['projects:read', 'knowledge:read']});
  if (path.endsWith('/projects') && options.method === 'GET') return response({items:[{id:'p1', title:'Project One', description:'<script>bad</script>', recruiting:true}], next_cursor:null});
  if (path.endsWith('/schema')) return response({kind:'project-join', revision:'1', fields:[{name:'project_id', type:'string', required:true}, {name:'motivation', type:'string', required:true, min_length:10}, {name:'weekly_hours', type:'integer', required:true, minimum:1, maximum:40}, {name:'notes', type:'string'}]});
  if (path.endsWith('/validate')) return response({valid:true, preview});
  if (path.endsWith('/drafts')) { draftCount++; return response({id:'d1', status:'draft', form_revision:'1', preview}); }
  if (path.endsWith('/confirmations')) return response({confirmation:'PRIVATE_CONFIRMATION', preview:config.scenario === 'changed-proof' ? {changed:true} : preview});
  if (path.endsWith('/applications') && options.method === 'POST') {
    if (config.scenario === 'retry' && !failed) { failed = true; throw new Error('network'); }
    return response({id:'d1', status:'submitted'});
  }
  if (path.endsWith('/applications')) {
    if (config.scenario.startsWith('admin') && url.includes('assigned=true')) return response({items:[{id:'review1', project_id:'p1', status:'submitted'}], next_cursor:null});
    return response({items:[], next_cursor:null});
  }
  if (path.endsWith('/authorization-requests')) { codeCount++; return response({code:'ONE_TIME_CODE', expires_at:new Date(Date.now()+60000).toISOString()}); }
  if (path.endsWith('/grants') && options.method === 'GET') return response({items:[{id:'g1', scopes:['projects:read'], expires_at:'2027-01-01'}], next_cursor:null});
  if (path.endsWith('/grants/g1') && options.method === 'DELETE') return response({}, 204);
  if (path.endsWith('/knowledge/search')) return response({items:[{id:'bad', title:'Bad', snippet:'<svg/onload=alert(1)>', source_url:'javascript:alert(1)'}, {id:'good', title:'Good', source_url:'https://example.com/source'}], next_cursor:null});
  if (path.endsWith('/projects/p1') && options.method === 'PATCH') return response({id:'p1', recruiting:false});
  if (path.endsWith('/review')) return response({id:'review1', status:'approved'});
  if (path.endsWith('/applications/review1')) return response({preview, reviews:[]});
  throw new Error('unexpected endpoint ' + url);
};
async function flush() { for (let i=0;i<12;i++) await new Promise(resolve => setImmediate(resolve)); }
async function event(id, name) {
  const el = ids.get(id);
  for (const listener of el.listeners[name] || []) await listener({preventDefault(){}});
  await flush();
}
function matching(path, method) { return calls.filter(c => c.path.endsWith(path) && (!method || c.options.method === method)); }
function noSecrets() {
  for (const el of all) {
    assert.ok(!el.textContent.includes(secret));
    assert.ok(!el.textContent.includes('PRIVATE_CONFIRMATION'));
    assert.ok(!el.value.includes(secret));
  }
}
(async () => {
  vm.runInThisContext(fs.readFileSync(config.js, 'utf8'));
  await flush();
  if (config.scenario === 'applicant') {
    assert.equal(calls.length, 1); assert.equal(ids.get('admin').hidden, true);
    assert.equal(ids.get('save-draft').disabled, true); noSecrets(); return;
  }
  assert.ok(calls.every(c => c.path.startsWith(config.expectedRoot)));
  assert.equal(ids.get('cli-request').value, 'cli-request-123456');
  if (config.scenario === 'read') { noSecrets(); return; }
  if (config.scenario.startsWith('admin')) {
    assert.equal(ids.get('admin').hidden, false);
    const buttons = ids.get('review-list').children.flatMap(c => c.children).filter(c => c.tagName === 'BUTTON');
    const card = ids.get('review-list').children[0];
    const reason = card.children.find(c => c.tagName === 'TEXTAREA'); reason.value = 'Reviewed project fit';
    const consent = card.children.find(c => c.className === 'check').children[0]; consent.checked = true;
    await buttons.find(b => b.textContent === '批准').listeners.click[0]();
    assert.equal(matching('/review', 'POST').length, 1);
    ids.get('config-project').value = 'p1'; ids.get('config-recruiting').checked = false;
    await event('project-config', 'submit');
    assert.deepEqual(JSON.parse(matching('/projects/p1', 'PATCH')[0].options.body), {recruiting:false});
    noSecrets(); return;
  }
  if (config.scenario === 'search') {
    ids.get('search-query').value = 'public rules'; await event('search-form','submit');
    const links = all.filter(e => e.tagName === 'A' && e.href);
    assert.ok(!links.some(e => e.href.startsWith('javascript:')));
    assert.ok(links.some(e => e.href === 'https://example.com/source' && e.rel === 'noopener noreferrer'));
    noSecrets(); return;
  }
  if (config.scenario === 'cli') {
    ids.get('cli-consent').checked = true; await event('cli-form','submit');
    let body = JSON.parse(matching('/authorization-requests','POST')[0].options.body);
    assert.ok(!body.scopes.includes('applications:create'));
    assert.equal(ids.get('cli-code').textContent, 'ONE_TIME_CODE');
    ids.get('cli-scope').value = 'apply'; ids.get('cli-consent').checked = true;
    await event('cli-form','submit');
    body = JSON.parse(matching('/authorization-requests','POST')[1].options.body);
    assert.ok(body.scopes.includes('applications:create'));
    assert.ok(!body.scopes.some(s => s.includes('review') || s.includes('admin')));
    assert.equal(matching('/token').length, 0);
    const revoke = ids.get('grant-list').children[0].children.find(c => c.tagName === 'BUTTON');
    await revoke.listeners.click[0]();
    assert.equal(matching('/grants/g1','DELETE').length, 1);
    await event('clear-code','click'); assert.equal(ids.get('cli-code').textContent, '');
    noSecrets(); return;
  }
  await event('draft-form','submit');
  assert.equal(draftCount, 0); assert.ok(ids.get('missing-fields').children.length > 0);
  ids.get('field-project_id').value = 'p1'; ids.get('field-motivation').value = 'A useful contribution';
  ids.get('field-weekly_hours').value = '3';
  await event('draft-form','submit');
  assert.equal(draftCount, 1);
  assert.equal(ids.get('submit-application').disabled, true);
  assert.equal(matching('/confirmations').length, 0);
  assert.ok(ids.get('draft-preview').textContent.includes('project-join'));
  ids.get('confirm-check').checked = true; await event('confirm-check','change');
  if (config.scenario === 'edit') {
    await event('field-motivation','input');
    assert.equal(ids.get('submit-application').disabled,true);
    assert.equal(ids.get('confirm-check').checked,false);
    assert.equal(matching('/applications','POST').length,0); noSecrets(); return;
  }
  await event('submit-form','submit');
  if (config.scenario === 'changed-proof') {
    assert.equal(matching('/applications','POST').length,0);
    assert.equal(ids.get('submit-application').disabled,true); noSecrets(); return;
  }
  if (config.scenario === 'retry') {
    await event('submit-form','submit');
    const submits = matching('/applications','POST');
    assert.equal(submits.length, 2);
    assert.equal(submits[0].options.headers['Idempotency-Key'], submits[1].options.headers['Idempotency-Key']);
    assert.equal(submits[0].options.body, submits[1].options.body);
    assert.equal(matching('/confirmations').length,1);
  }
  assert.equal(ids.get('submit-application').disabled, true);
  assert.ok(ids.get('draft-status').textContent.includes('已正式提交'));
  noSecrets();
})().catch(error => { console.error(error); process.exitCode=1; });
"""


@pytest.mark.parametrize("scenario", ["read", "applicant", "submit", "edit", "changed-proof", "retry", "cli", "search", "admin-assigned", "admin-unassigned"])
def test_org_js_contract_with_dom_double(scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the DOM-double JavaScript checks")
    result = subprocess.run(
        [node, "-e", HARNESS, json.dumps({
            "html": str(STATIC / "org.html"), "js": str(STATIC / "org.js"),
            "scenario": scenario, "prefix": "__PREFIX__", "path": "/recruitment/static/org.html",
            "expectedRoot": "/recruitment/api/v1/",
        })], capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("prefix,path,root", [("", "/static/org.html", "/api/v1/"), ("/custom", "/custom/org", "/custom/api/v1/"), ("__PREFIX__", "/org.html", "/api/v1/")])
def test_org_api_prefixes(prefix, path, root):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the DOM-double JavaScript checks")
    result = subprocess.run(
        [node, "-e", HARNESS, json.dumps({"html": str(STATIC / "org.html"), "js": str(STATIC / "org.js"), "scenario": "read", "prefix": prefix, "path": path, "expectedRoot": root})],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr

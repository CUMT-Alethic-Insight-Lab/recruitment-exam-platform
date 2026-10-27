import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import config
from conftest import admin_login


def _run_js(script, harness, data):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise page JavaScript")
    result = subprocess.run(
        [node], input="const SCRIPT = " + json.dumps(script) + ";\nconst DATA = "
        + json.dumps(data) + ";\n" + harness,
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _inline(response):
    assert response.status_code == 200
    return re.search(r"<script>(.*?)</script>", response.text, re.S)[1]


HARNESS = r'''
const vm = require('vm');
const assert = require('assert/strict');
const storage = new Map();
const nodes = new Map();
const timers = [];
function element(id) {
  if (!nodes.has(id)) {
    const classes = new Set();
    nodes.set(id, {_value:'', get value(){return this._value;}, set value(v){this._value=String(v);},
      checked:false, style:{}, children:[],
      classList:{add:c=>classes.add(c), remove:c=>classes.delete(c),
        contains:c=>classes.has(c), toggle:(c,on)=>on?classes.add(c):classes.delete(c)},
      addEventListener(){}, removeAttribute(){}, setAttribute(){}, focus(){},
      appendChild(el){this.children.push(el);}});
  }
  return nodes.get(id);
}
const location = {origin:'https://alethicinsight.com', search:'', href:'',
  replace(dest){this.href=dest;}};
const document = {getElementById:element, addEventListener(){}, querySelectorAll:()=>[],
  createElement:tag=>element('created-'+nodes.size), documentElement:{dataset:{}},
  querySelector:()=>({dataset:{prefix:DATA.prefix, adminPath:DATA.admin}})};
let requests = [];
let me = DATA.me;
let status = 200;
const ctx = vm.createContext({console, URL, URLSearchParams, Date, Map, Set, Uint8Array,
  TextEncoder, crypto:require('crypto').webcrypto,
  window:{addEventListener(){}}, document, location,
  localStorage:{getItem:k=>storage.get(k)||null, setItem:(k,v)=>storage.set(k,String(v)), removeItem:k=>storage.delete(k)},
  setTimeout:(fn,ms)=>timers.push([fn,ms]), setInterval:()=>1, clearInterval(){},
  fetch:async(url, opts={})=>{
    requests.push([url,opts]);
    if(url.endsWith('/latest-open')) return {ok:false};
    const data = url.includes('/salt?') ? {salt:'00'.repeat(16)} : url.endsWith('/login') ? DATA.login : me;
    return {ok:status===200,status,json:async()=>data};
  }});
const settle = () => new Promise(resolve=>setImmediate(resolve));
'''


@pytest.mark.parametrize("prefix,admin", [("", "/admin"), ("/recruitment", "/admin/recruitment")])
def test_rendered_login_script_destinations_and_resume(client, monkeypatch, prefix, admin):
    monkeypatch.setattr(config, "PUBLIC_PREFIX", prefix)
    monkeypatch.setattr(config, "ADMIN_PUBLIC_PATH", admin)
    login = admin_login(client).json()
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + login["token"]}).json()
    _run_js(_inline(client.get("/login")), HARNESS + r'''
(async()=>{
  vm.runInContext(SCRIPT,ctx);
  await settle();
  for (const role of ['admin','member','applicant']) {
    assert.equal(vm.runInContext(`loginDestination('${role}')`,ctx), '/account');
  }
  for (const unsafe of ['https://evil.example', '//evil.example', '/\\evil.example',
      'javascript:alert(1)', '/%2f%2fevil.example', '/login', '/admin/../login', '/\nevil.example']) {
    location.search='?next='+encodeURIComponent(unsafe);
    assert.equal(vm.runInContext("loginDestination('admin')",ctx), '/account');
  }
  location.search='?next='+encodeURIComponent(DATA.admin+'?tab=accounts#members');
  assert.equal(vm.runInContext("loginDestination('admin')",ctx), DATA.admin+'?tab=accounts#members');
  assert.equal(vm.runInContext("loginDestination('member')",ctx), '/account');
  location.search='';
  element('student_id').value=DATA.login.student_id;
  element('password').value='test-password';
  await vm.runInContext('login()',ctx);
  assert.equal(storage.get('alethic_admin_token'), DATA.login.token);
  assert.equal(storage.has('alethic_session'),false);
  assert.equal(ctx.window.__loginDest, '/account');
  assert.equal(element('welcome-sub').textContent,'正在进入个人中心…');
  timers.find(t=>t[1]===1400)[0]();
  assert.equal(location.href,'/account');
  timers.find(t=>t[1]===4500)[0]();
  assert.equal(element('welcome-manual').href,'/account');
  location.href='';
  await vm.runInContext('resumeSession()',ctx);
  assert.equal(location.href,'/account');
  location.search='?next='+encodeURIComponent(DATA.admin);
  await vm.runInContext('resumeSession()',ctx);
  assert.equal(location.href,DATA.admin);
  status=401;
  await vm.runInContext('resumeSession()',ctx);
  assert.equal(storage.has('alethic_admin_token'),false);
})().catch(e=>{console.error(e);process.exitCode=1;});
''', {"prefix": prefix, "admin": admin, "login": login, "me": me})


@pytest.mark.parametrize("stored_sid", [None, "99999999"])
def test_admin_own_profile_api_and_rendered_account_script(client, admin_headers, stored_sid):
    response = client.post("/api/auth/profile", headers=admin_headers,
                           json={"profile": {"bio": "管理员本人资料", "email": "admin@example.com"}})
    assert response.status_code == 200, response.text
    me = client.get("/api/auth/me", headers=admin_headers).json()
    assert me["role"] == "admin"
    assert me["profile"]["bio"] == "管理员本人资料"
    assert client.get("/api/admin/accounts", headers=admin_headers).status_code == 200
    token = admin_headers["Authorization"].removeprefix("Bearer ")
    _run_js(_inline(client.get("/account")), HARNESS + r'''
(async()=>{
  storage.set('alethic_admin_token',DATA.token);
  storage.set('alethic_session','stale-applicant');
  if (DATA.stored_sid) storage.set('alethic_student_id',DATA.stored_sid);
  storage.set('alethic_cid','stale-campaign');
  vm.runInContext(SCRIPT,ctx);
  await settle();
  assert.equal(location.href,'');
  assert.equal(storage.get('alethic_admin_token'),DATA.token);
  assert.equal(storage.get('alethic_student_id'),DATA.me.student_id);
  assert.equal(storage.has('alethic_cid'),false);
  assert.equal(element('member-bio').value,'管理员本人资料');
  assert.equal(element('member-save-btn').disabled,false);
  element('member-bio').value='更新资料';
  await vm.runInContext('saveMemberProfile()',ctx);
  const post=requests.find(([url,o])=>url.endsWith('/profile')&&o.method==='POST');
  assert.equal(post[1].headers.Authorization,'Bearer '+DATA.token);
  assert.equal(JSON.parse(post[1].body).profile.bio,'更新资料');
  assert.equal(storage.get('alethic_admin_token'),DATA.token);
  assert.equal(requests.some(([url])=>url.includes('/api/campaigns/')),false);
  me={...DATA.me,pw_changed:false};
  await vm.runInContext('loadMe()',ctx);
  assert.equal(element('member-save-btn').disabled,true);
  status=401;
  await vm.runInContext('loadMe()',ctx);
  assert.equal(storage.has('alethic_admin_token'),false);
  assert.equal(location.href,'/login');
})().catch(e=>{console.error(e);process.exitCode=1;});
''', {"prefix": config.PUBLIC_PREFIX, "admin": config.effective_admin_path(), "me": me,
      "token": token, "stored_sid": stored_sid})


def test_admin_topbar_keeps_personal_and_management_entries(client, admin_headers):
    me = client.get("/api/auth/me", headers=admin_headers).json()
    script = (Path(__file__).parent.parent / "static/js/topbar.js").read_text(encoding="utf-8")
    _run_js(script, HARNESS + r'''
(async()=>{
  storage.set('alethic_admin_token','admin-token');
  vm.runInContext(SCRIPT,ctx);
  await settle();
  const links=element('topbar-auth').children;
  assert(links.some(el=>el.href==='/account'&&el.textContent==='个人中心'));
  assert(links.some(el=>el.href===DATA.admin&&el.textContent==='管理端'));
  assert.equal(storage.get('alethic_admin_token'),'admin-token');
})().catch(e=>{console.error(e);process.exitCode=1;});
''', {"prefix": config.PUBLIC_PREFIX, "admin": config.effective_admin_path(), "me": me})

import re
from pathlib import Path

import pytest

from test_m28_login_destination import HARNESS, _inline, _run_js


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("path", ["/login", "/account", "/admin", "/apply/example"])
def test_site_account_links_keep_legacy_api_and_resources(client, monkeypatch, path):
    monkeypatch.setattr("app.main.config.PUBLIC_PREFIX", "/recruitment")
    page = client.get(path)
    assert page.status_code == 200
    assert 'href="/login"' in page.text
    assert 'href="/recruitment/static/css/style.css?v=' in page.text
    assert "/recruitment/api/" in page.text
    assert "__PREFIX__" not in page.text
    assert 'href="/recruitment/login"' not in page.text
    assert 'href="/recruitment/account"' not in page.text
    if path == "/login":
        assert "const fallback = '/account';" in page.text
    if path == "/account":
        assert "location.href = '/login'" in page.text
        assert "location.replace('/login')" in page.text


def test_legacy_next_paths_are_canonicalized_with_role_whitelist(client, monkeypatch):
    monkeypatch.setattr("app.main.config.PUBLIC_PREFIX", "/recruitment")
    monkeypatch.setattr("app.main.config.ADMIN_PUBLIC_PATH", "/admin/recruitment")
    _run_js(_inline(client.get("/login")), HARNESS + r'''
    vm.runInContext(SCRIPT,ctx);
    for (const old of ['/account','/recruitment/account']) {
      location.search='?next='+encodeURIComponent(old+'?tab=profile#bio');
      assert.equal(vm.runInContext("loginDestination('member')",ctx),'/account?tab=profile#bio');
    }
    location.search='?next='+encodeURIComponent('/recruitment/admin?tab=accounts');
    assert.equal(vm.runInContext("loginDestination('admin')",ctx),'/admin/recruitment?tab=accounts');
    assert.equal(vm.runInContext("loginDestination('member')",ctx),'/account');
    for (const unsafe of ['/recruitment/account/evil','/%61ccount','/account%2f..%2flogin',
        '/account\\evil','//evil.example/account','https://evil.example/account']) {
      location.search='?next='+encodeURIComponent(unsafe);
      assert.equal(vm.runInContext("loginDestination('admin')",ctx),'/account');
    }
    ''', {"prefix": "/recruitment", "admin": "/admin/recruitment"})


def test_admin_logout_clears_session_and_returns_site_login(client):
    page = client.get("/admin").text
    script = re.search(r"document.getElementById\('logout-btn'\).addEventListener\('click', \(\) => \{(.*?)\n\}\);", page, re.S)[1]
    _run_js(script, HARNESS + r'''
    (async()=>{
      for (const key of ['alethic_admin_token','alethic_session','alethic_student_id','alethic_cid']) storage.set(key,'stale');
      ctx.getToken=()=>storage.get('alethic_admin_token');
      ctx.clearToken=()=>storage.delete('alethic_admin_token');
      vm.runInContext(SCRIPT,ctx);
      await settle();
      assert.equal(location.href,'/login');
      assert.equal(storage.size,0);
      assert(requests.some(([url,o])=>url.endsWith('/api/auth/logout')&&o.method==='POST'));
    })().catch(e=>{console.error(e);process.exitCode=1;});
    ''', {})


def test_shared_topbar_logout_and_guest_use_site_login():
    script = (ROOT / "static/js/topbar.js").read_text(encoding="utf-8")
    harness = HARNESS.replace("addEventListener(){}, removeAttribute(){}", "addEventListener(name,fn){this[name]=fn;}, removeAttribute(){}")
    _run_js(script, harness + r'''
    (async()=>{
      storage.set('alethic_admin_token','token');
      vm.runInContext(SCRIPT,ctx);
      await settle();
      const links=element('topbar-auth').children;
      assert(links.some(el=>el.href==='/account'));
      links.find(el=>el.textContent==='退出').click();
      await settle();
      assert.equal(location.href,'/login');
      assert.equal(storage.size,0);
      element('topbar-auth').children=[];
      vm.runInContext(SCRIPT,ctx);
      assert(element('topbar-auth').children.some(el=>el.href==='/login'));
    })().catch(e=>{console.error(e);process.exitCode=1;});
    ''', {"prefix": "/recruitment", "admin": "/admin/recruitment", "me": {"role": "admin"}})


@pytest.mark.parametrize("name", ["nginx-alethicinsight.vhost.conf", "nginx-recruitment.location.conf"])
def test_nginx_site_accounts_reuse_legacy_proxy_chain(name):
    text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
    for path in ("login", "account"):
        assert f"location = /{path} {{ rewrite ^ /recruitment/{path} last; }}" in text
        assert f"return 301 /{path}$is_args$args;" in text
    assert "location ^~ /recruitment/" in text
    assert "proxy_pass" in text

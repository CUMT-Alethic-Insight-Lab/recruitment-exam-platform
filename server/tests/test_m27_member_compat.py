import json
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import auth, config, db
from conftest import applicant_login
from test_m27_member_profile import _member_headers, _register, _unified_login


def _application(student):
    return {
        "student_id": student, "name": "兼容成员", "gender": "male",
        "class_name": "计科244", "qq": "600600600", "lab_intent": False,
        "domains": ["llm_arch"], "focus_next_year": "project",
        "expectation": "成员兼容复核", "overwrite": True,
    }


@pytest.mark.parametrize("session_kind", ["unified", "legacy_member", "exam_kit"])
def test_member_registration_exam_upload_and_booking(
        client, cid, admin_headers, session_kind):
    student = {"unified": "08264801", "legacy_member": "08264802",
               "exam_kit": "08264803"}[session_kind]
    _register(client, cid, student)
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "member"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    data = _unified_login(client, student)
    assert data["cid"] == cid
    with db.connection() as conn:
        sess = auth.lookup_session(conn, data["token"])
        assert sess["kind"] == "member"
        assert sess["campaign_id"] == cid
        if session_kind == "legacy_member":
            token = auth.issue_session(conn, "member", None, student)
        else:
            token = data["token"]
    if session_kind == "exam_kit":
        token = applicant_login(client, cid, student)["token"]
    headers = {"Authorization": f"Bearer {token}"}
    payload = _application(student)
    for _ in range(2):
        res = client.post(f"/api/campaigns/{cid}/applications",
                          json=payload, headers=headers)
        assert res.status_code == 200, res.text
        assert res.json()["registration_complete"] == 1
    assert client.get(f"/api/campaigns/{cid}/auth/me", headers=headers).status_code == 200
    assert client.get(f"/api/campaigns/{cid}/applications/{student}",
                      headers=headers).status_code == 200
    other = str(int(student) + 20).zfill(8)
    _register(client, cid, other)
    assert client.get(f"/api/campaigns/{cid}/applications/{other}",
                      headers=headers).status_code == 403
    other_payload = _application(other)
    res = client.post(f"/api/campaigns/{cid}/applications", json=other_payload)
    assert res.status_code == 200, res.text
    assert client.post(f"/api/campaigns/{cid}/applications", json=other_payload,
                       headers=headers).status_code == 403
    assert client.get("/api/admin/accounts", headers=headers).status_code == 401
    assert client.get("/api/campaigns/not-this-campaign/auth/me",
                      headers=headers).status_code in (403, 404)

    res = client.post("/api/questions", headers=admin_headers, json={
        "domain": "llm_arch", "track": "track1", "title": "M27兼容题",
        "body": "成员兼容测试题面",
    })
    assert res.status_code == 201, res.text
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1", "question_ids": [res.json()["id"]]},
                      headers=headers)
    assert res.status_code == 200, res.text
    exam = res.json()
    res = client.post(f"/api/campaigns/{cid}/sessions",
                      data={"session_id": exam["session_id"]},
                      files={"cleaned": ("cleaned.jsonl", b'{"role":"user","content":"hi"}\n')},
                      headers={"X-Session-Token": exam["session_token"]})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "uploaded"
    res = client.get(f"/api/campaigns/{cid}/applications/{student}", headers=headers)
    assert res.json()["status"] == "tested"
    res = client.patch(f"/api/campaigns/{cid}/applications/{student}/status",
                       json={"status": "shortlisted"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    day = (date.today() + timedelta(days=90)).isoformat()
    res = client.post(f"/api/campaigns/{cid}/interview-slots/generate", json={
        "date": day, "windows": [{"start": "10:00", "end": "12:00"}],
        "slot_minutes": 45,
    }, headers=admin_headers)
    assert res.status_code == 200, res.text
    res = client.get(f"/api/campaigns/{cid}/interview-slots/available", headers=headers)
    assert res.status_code == 200, res.text
    slot = next(s for s in res.json()["slots"] if not s["taken"])
    res = client.post(f"/api/campaigns/{cid}/interview-slots/{slot['id']}/book",
                      json={"method": "online"}, headers=headers)
    assert res.status_code == 200, res.text
    assert client.post(f"/api/campaigns/{cid}/interview-slots/mine/cancel",
                       headers=headers).status_code == 200
    with db.connection() as conn:
        assert conn.execute("SELECT role FROM account WHERE student_id = ?",
                            (student,)).fetchone()["role"] == "member"


def test_member_without_application_has_no_campaign_and_no_privileges(client, cid):
    headers = _member_headers(client, "08264810")
    me = client.get("/api/auth/me", headers=headers).json()
    assert "cid" not in me
    for path in (f"/api/campaigns/{cid}/auth/me",
                 f"/api/campaigns/{cid}/exam/questions",
                 f"/api/campaigns/{cid}/interview-slots/available"):
        res = client.get(path, headers=headers)
        assert res.status_code == 404, res.text
    assert client.get("/api/admin/accounts", headers=headers).status_code == 401


def test_admin_to_member_self_and_stale_session_protection(client, cid, admin_headers):
    res = client.post(f"/api/admin/accounts/{config.ADMIN_USERNAME}/role",
                      json={"role": "member"}, headers=admin_headers)
    assert res.status_code == 400, res.text
    assert "当前登录" in res.json()["detail"]
    student = "08264811"
    _register(client, cid, student)
    with db.connection() as conn:
        admins = [r["student_id"] for r in conn.execute(
            "SELECT student_id FROM account WHERE role = 'admin'")]
        conn.execute("UPDATE account SET role = 'applicant' WHERE role = 'admin'")
        conn.execute("UPDATE account SET role = 'admin' WHERE student_id = ?", (student,))
    try:
        # 模拟未经 set_role 的历史角色变化：旧 admin 会话也不能继续操作。
        res = client.post(f"/api/admin/accounts/{student}/role",
                          json={"role": "member"}, headers=admin_headers)
        assert res.status_code == 401, res.text
    finally:
        with db.connection() as conn:
            conn.execute("UPDATE account SET role = 'applicant' WHERE student_id = ?", (student,))
            conn.executemany("UPDATE account SET role = 'admin' WHERE student_id = ?",
                             [(sid,) for sid in admins])


def test_role_change_revokes_sessions_and_rechecks_current_role(client, cid, admin_headers):
    student = "08264814"
    _register(client, cid, student)
    with db.connection() as conn:
        conn.execute("UPDATE account SET pw_changed = 1 WHERE student_id = ?", (student,))
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "admin"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    admin_data = _unified_login(client, student)
    old_admin = {"Authorization": f"Bearer {admin_data['token']}"}
    assert client.get("/api/admin/accounts", headers=old_admin).status_code == 200

    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "member"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    assert client.get("/api/admin/accounts", headers=old_admin).status_code == 401
    assert _unified_login(client, student)["role"] == "member"

    # 兼容修复前已存在的旧会话：即使未被删除，也不能绕过当前角色核验。
    with db.connection() as conn:
        stale_token = auth.issue_session(conn, "admin", cid, student)
    stale = {"Authorization": f"Bearer {stale_token}"}
    assert client.get("/api/admin/accounts", headers=stale).status_code == 401


def test_migration_preserves_existing_roles_and_profile(client, cid, admin_headers):
    student = "08264812"
    _register(client, cid, student)
    with db.connection() as conn:
        conn.execute("UPDATE account SET role = 'member', profile = ? WHERE student_id = ?",
                     (json.dumps({"bio": "保留", "show_on_site": False}), student))
        before = [dict(r) for r in conn.execute("SELECT * FROM account ORDER BY student_id")]
        db._migrate_account(conn)
        after = [dict(r) for r in conn.execute("SELECT * FROM account ORDER BY student_id")]
    assert before == after


def test_member_profile_requires_explicit_publication_consent(client):
    headers = _member_headers(client, "08264813")
    for patch, expected in (({"bio": "私有"}, False), ({"show_on_site": True}, True),
                            ({"bio": "更新"}, True), ({"show_on_site": False}, False)):
        res = client.post("/api/auth/profile", json={"profile": patch}, headers=headers)
        assert res.status_code == 200, res.text
        assert res.json()["profile"]["show_on_site"] is expected


def test_account_script_member_context_clear_fields_and_consent():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to exercise the account page script")
    page = Path(__file__).parent.parent / "static" / "account.html"
    script = page.read_text(encoding="utf-8").split("<script>", 1)[1].split("</script>", 1)[0]
    script = script.split("if (!token) {", 1)[0]
    harness = r'''
const vm = require('vm');
const assert = require('assert/strict');
const nodes = new Map();
const storage = new Map([['alethic_session', 'token'], ['alethic_student_id', '08264820'], ['alethic_cid', 'stale']]);
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {value: '', checked: false, style: {},
    classList: {toggle() {}, add() {}, remove() {}, contains() {return false;}},
    addEventListener() {}, removeAttribute() {}, focus() {}});
  return nodes.get(id);
}
let response = {role:'member', student_id:'08264820', cid:'2026', pw_changed:true, profile:{email:'old@example.com', enroll_year:2024, grad_year:2028}};
const topics = ['resources', 'projects', 'other'].map(value => ({value, checked:true, disabled:false}));
let saved;
const ctx = vm.createContext({console, URL, Date, Map, Set, Uint8Array,
  localStorage: {getItem:k=>storage.get(k), setItem:(k,v)=>storage.set(k,v), removeItem:k=>storage.delete(k)},
  location: {replace() {}},
  document: {getElementById:element, addEventListener() {}, querySelectorAll:selector=>selector.includes('meeting_topics') ? topics : []},
  fetch: async (url, opts) => {
    if (opts && opts.method === 'POST') {saved = JSON.parse(opts.body); return {ok:true, json:async()=>({ok:true})};}
    return {ok:true, status:200, json:async()=>response};
  }, setInterval:()=>1, clearInterval() {}});
vm.runInContext(SCRIPT, ctx);
(async () => {
  await vm.runInContext('loadMe()', ctx);
  assert.equal(storage.get('alethic_cid'), '2026');
  assert.equal(element('member-show-on-site').checked, false);
  assert.ok(topics.every(t => !t.checked));
  assert.equal(element('member-meeting-other').value, '');
  element('member-email').value = '';
  element('member-enroll-year').value = '';
  element('member-grad-year').value = '';
  await vm.runInContext('saveMemberProfile()', ctx);
  assert.equal(saved.profile.email, '');
  assert.equal(saved.profile.enroll_year, null);
  assert.equal(saved.profile.grad_year, null);
  assert.equal(saved.profile.show_on_site, false);
  assert.deepEqual(saved.profile.meeting_topics, []);
  assert.equal(saved.profile.meeting_other, '');
  topics[0].checked = true;
  topics[1].checked = true;
  element('member-meeting-other').value = '也想听听经验';
  await vm.runInContext('saveMemberProfile()', ctx);
  assert.deepEqual(saved.profile.meeting_topics, ['resources', 'projects']);
  assert.equal(saved.profile.meeting_other, '也想听听经验');
  response = {...response, profile:{...saved.profile, enroll_year:null, show_on_site:true}};
  topics.forEach(t => {t.checked = false;});
  element('member-meeting-other').value = '';
  await vm.runInContext('loadMe()', ctx);
  assert.deepEqual(topics.map(t => t.checked), [true, true, false]);
  assert.equal(element('member-meeting-other').value, '也想听听经验');
  assert.equal(element('member-enroll-year').value, '');
  assert.equal(element('member-show-on-site').checked, true);
  topics.forEach(t => {t.checked = false;});
  await vm.runInContext('saveMemberProfile()', ctx);
  assert.deepEqual(saved.profile.meeting_topics, []);
  assert.equal(saved.profile.meeting_other, '也想听听经验');
  element('member-meeting-other').value = '';
  await vm.runInContext('saveMemberProfile()', ctx);
  assert.equal(saved.profile.meeting_other, '');
  vm.runInContext('setForcedPwMode(true)', ctx);
  assert.ok(topics.every(t => t.disabled));
  assert.equal(element('member-meeting-other').disabled, true);
  vm.runInContext('setForcedPwMode(false)', ctx);
  assert.ok(topics.every(t => !t.disabled));
  assert.equal(element('member-meeting-other').disabled, false);
  delete response.cid;
  await vm.runInContext('loadMe()', ctx);
  assert.equal(storage.has('alethic_cid'), false);
  assert.equal(vm.runInContext('cid', ctx), '');
})().catch(e=>{console.error(e); process.exitCode=1;});
'''
    result = subprocess.run([node], input="const SCRIPT = " + json.dumps(script) + ";\n" + harness,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr

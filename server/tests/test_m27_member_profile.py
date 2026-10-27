"""M27 成员资料（account.profile）与 member 角色：
老库迁移出 profile 列、me 返回 profile 与 member 的 name、
PATCH profile 严格校验/部分合并/改密门禁、admin 设 role=member、
list_accounts 携带 profile_filled。"""

import sqlite3
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import auth, db
from conftest import change_password

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NOW = "2026-01-01T00:00:00+00:00"


def _register(client: TestClient, cid: str, student_id: str, name="M27考生"):
    res = client.post(f"/api/campaigns/{cid}/applications/register",
                      json={"student_id": student_id, "name": name, "gender": "male"})
    assert res.status_code == 200, res.text


def _unified_login(client: TestClient, student_id: str, password: str | None = None):
    password = password if password is not None else student_id
    res = client.get("/api/auth/salt", params={"student_id": student_id})
    assert res.status_code == 200, res.text
    client_hash = auth.compute_client_hash(password, res.json()["salt"])
    res = client.post("/api/auth/login",
                      json={"student_id": student_id, "client_hash": client_hash})
    assert res.status_code == 200, res.text
    return res.json()


def _make_applicant(client: TestClient, cid: str, student_id: str, name="M27考生"):
    _register(client, cid, student_id, name)
    return _unified_login(client, student_id)


def _member_headers(client: TestClient, student_id: str) -> dict:
    """直插一个 pw_changed=1 的 member 账号并登录，返回 Bearer headers。"""
    salt = auth.new_salt()
    client_hash = auth.compute_client_hash(student_id, salt)
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO account (student_id, role, nickname, pw_salt, pw_hash,"
            " pw_changed, created_at, updated_at)"
            " VALUES (?, 'member', ?, ?, ?, 1, ?, ?)",
            (student_id, "老成员", salt, auth.store_hash(client_hash), NOW, NOW),
        )
    data = _unified_login(client, student_id)
    assert data["role"] == "member"
    return {"Authorization": f"Bearer {data['token']}"}


# ---------- 老库迁移 ----------


def test_migration_adds_profile_column(tmp_path):
    """老库 account 表缺 profile 列：_migrate 补列，存量行回填 '{}'。"""
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    # 模拟老库：account 表还是旧结构（无 profile 列），且已有一行存量账号
    conn.executescript("""
        DROP TABLE account;
        CREATE TABLE account (
            student_id  TEXT PRIMARY KEY,
            role        TEXT NOT NULL DEFAULT 'applicant',
            nickname    TEXT NOT NULL DEFAULT '',
            avatar_ext  TEXT,
            pw_salt     TEXT,
            pw_hash     TEXT,
            pw_changed  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
    """)
    conn.execute(
        "INSERT INTO account (student_id, role, nickname, pw_salt, pw_hash,"
        " pw_changed, created_at, updated_at)"
        " VALUES ('08264701', 'applicant', '老库考生', NULL, NULL, 0, ?, ?)",
        (NOW, NOW),
    )
    assert "profile" not in {r["name"] for r in conn.execute("PRAGMA table_info(account)")}

    db._migrate(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(account)")}
    assert "profile" in cols
    row = conn.execute(
        "SELECT profile FROM account WHERE student_id = '08264701'").fetchone()
    assert row["profile"] == "{}"
    # 迁移后新插入（不带 profile 列）也默认 '{}'
    conn.execute(
        "INSERT INTO account (student_id, role, nickname, pw_salt, pw_hash,"
        " pw_changed, created_at, updated_at)"
        " VALUES ('08264702', 'applicant', '新考生', NULL, NULL, 0, ?, ?)",
        (NOW, NOW),
    )
    row = conn.execute(
        "SELECT profile FROM account WHERE student_id = '08264702'").fetchone()
    assert row["profile"] == "{}"
    conn.close()


# ---------- me / 登录 payload ----------


def test_me_returns_profile_and_member_name(client, cid):
    # member 老成员：无报名行 → name=''，不带 cid/registration_complete，带 profile
    headers = _member_headers(client, "08264710")
    res = client.get("/api/auth/me", headers=headers)
    assert res.status_code == 200, res.text
    me = res.json()
    assert me["role"] == "member"
    assert me["name"] == ""
    assert "cid" not in me and "registration_complete" not in me
    assert me["profile"] == {}

    # 坏 JSON 容错：me 不炸，profile 回退 {}
    with db.connection() as conn:
        conn.execute("UPDATE account SET profile = 'not-json' WHERE student_id = ?",
                     ("08264710",))
    res = client.get("/api/auth/me", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["profile"] == {}
    # 非 dict 的合法 JSON（数组）同样回退 {}
    with db.connection() as conn:
        conn.execute("UPDATE account SET profile = '[1,2]' WHERE student_id = ?",
                     ("08264710",))
    res = client.get("/api/auth/me", headers=headers)
    assert res.json()["profile"] == {}

    # applicant：照旧带 name/cid/registration_complete，profile 初始 {}
    data = _make_applicant(client, cid, "08264711", "报名考生")
    assert data["profile"] == {}
    res = client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {data['token']}"})
    me = res.json()
    assert me["name"] == "报名考生"
    assert me["cid"] == cid
    assert me["registration_complete"] is False
    assert me["profile"] == {}


def test_member_with_application_row_keeps_registration_payload(
        client, cid, admin_headers):
    """member 角色但查得到报名行：name/cid/registration_complete 照常返回。"""
    student = "08264712"
    _register(client, cid, student, "转成员考生")
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "member"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    data = _unified_login(client, student)
    assert data["role"] == "member"
    assert data["name"] == "转成员考生"
    assert data["cid"] == cid
    assert data["registration_complete"] is False

    res = client.get("/api/auth/me",
                     headers={"Authorization": f"Bearer {data['token']}"})
    me = res.json()
    assert me["role"] == "member"
    assert me["name"] == "转成员考生"
    assert me["cid"] == cid
    assert me["registration_complete"] is False

    # member 有报名记录时保留本人考生流程，但不获得管理权限。
    member_headers = {"Authorization": f"Bearer {data['token']}"}
    assert client.get(f"/api/campaigns/{cid}/auth/me",
                      headers=member_headers).status_code == 200
    assert client.get("/api/admin/accounts", headers=member_headers).status_code == 401


# ---------- PATCH /api/auth/profile：校验分支 ----------


def _patch(client, headers, body):
    return client.post("/api/auth/profile", json=body, headers=headers)


def test_profile_patch_validation_branches(client, cid):
    data = _make_applicant(client, cid, "08264720", "资料考生")
    res = change_password(client, "08264720", "08264720", "Prof#12345", data["token"])
    assert res.status_code == 200, res.text
    headers = {"Authorization": f"Bearer {data['token']}"}

    # nickname 与 profile 都缺省 → 400
    assert _patch(client, headers, {}).status_code == 400
    assert _patch(client, headers, {"nickname": None, "profile": None}).status_code == 400

    # 未知键 → 400
    assert _patch(client, headers, {"profile": {"hobby": "爬山"}}).status_code == 400

    # 坏邮箱
    for bad in ("not-an-email", "a@b", "a@b.c", "a b@x.cn", "x" * 81 + "@x.cn"):
        assert _patch(client, headers,
                      {"profile": {"email": bad}}).status_code == 400, bad
    assert _patch(client, headers,
                  {"profile": {"school_email": "同学@@x.cn"}}).status_code == 400

    # 坏手机号
    for bad in ("12345", "22345678901", "1380013800", "138001380012", "手机号"):
        assert _patch(client, headers,
                      {"profile": {"phone": bad}}).status_code == 400, bad

    # 坏政治面貌
    assert _patch(client, headers,
                  {"profile": {"political_status": "少先队员"}}).status_code == 400

    # 坏年月（birth_date）
    for bad in ("2004-13", "2004-00", "2004-1", "04-01", "2004/01", "2004年01"):
        assert _patch(client, headers,
                      {"profile": {"birth_date": bad}}).status_code == 400, bad

    # 坏 URL（github / homepage）
    for bad in ("github.com/octocat", "https://gitlab.com/octocat",
                "https://github.com/", "ftp://github.com/octocat"):
        assert _patch(client, headers,
                      {"profile": {"github": bad}}).status_code == 400, bad
    for bad in ("example.com", "ftp://example.com", "x" * 201):
        assert _patch(client, headers,
                      {"profile": {"homepage": bad}}).status_code == 400, bad

    # 年份越界 / 类型错误
    for bad in (1999, 2041, "2023", True, 2023.0):
        assert _patch(client, headers,
                      {"profile": {"enroll_year": bad}}).status_code == 400, bad
    assert _patch(client, headers,
                  {"profile": {"grad_year": 2050}}).status_code == 400

    # 字段类型错误
    assert _patch(client, headers, {"profile": {"bio": 123}}).status_code == 400
    assert _patch(client, headers,
                  {"profile": {"show_on_site": "yes"}}).status_code == 400
    assert _patch(client, headers,
                  {"profile": {"birth_date": 200407}}).status_code == 400
    # 超长文本
    assert _patch(client, headers, {"profile": {"bio": "字" * 101}}).status_code == 400
    assert _patch(client, headers,
                  {"profile": {"college": "院" * 41}}).status_code == 400
    assert _patch(client, headers,
                  {"profile": {"class_name": "班" * 41}}).status_code == 400


def test_profile_patch_valid_fill_merge_and_clear(client, cid):
    data = _make_applicant(client, cid, "08264721", "合并考生")
    res = change_password(client, "08264721", "08264721", "Merge#12345", data["token"])
    assert res.status_code == 200, res.text
    headers = {"Authorization": f"Bearer {data['token']}"}

    # 一次填全（不带 show_on_site → 默认 false，公开须本人授权）
    full = {
        "birth_date": "2004-07",
        "political_status": "共青团员",
        "college": "计算机学院",
        "class_name": "计科2401",
        "enroll_year": 2024,
        "grad_year": 2028,
        "phone": "13800138000",
        "email": "stu@example.com",
        "school_email": "stu@school.edu.cn",
        "github": "https://github.com/octocat",
        "homepage": "https://octocat.dev",
        "bio": "大家好，我是合并考生。",
    }
    res = _patch(client, headers, {"profile": full})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["profile"]["show_on_site"] is False
    for k, v in full.items():
        assert body["profile"][k] == v, k

    # 库里中文不转义（ensure_ascii=False）
    with db.connection() as conn:
        raw = conn.execute("SELECT profile FROM account WHERE student_id = ?",
                           ("08264721",)).fetchone()["profile"]
    assert "合并考生" in raw

    # 部分更新：只改 bio，其余字段保留
    res = _patch(client, headers, {"profile": {"bio": "只改一句话"}})
    assert res.status_code == 200, res.text
    merged = res.json()["profile"]
    assert merged["bio"] == "只改一句话"
    assert merged["birth_date"] == "2004-07"
    assert merged["phone"] == "13800138000"
    assert merged["show_on_site"] is False

    # 清除语义：''/null 置空，其余保留
    res = _patch(client, headers, {"profile": {"birth_date": "", "grad_year": None}})
    assert res.status_code == 200, res.text
    merged = res.json()["profile"]
    assert merged["birth_date"] == ""
    assert merged["grad_year"] is None
    assert merged["phone"] == "13800138000"

    # show_on_site 可显式关掉，且部分更新不回弹
    res = _patch(client, headers, {"profile": {"show_on_site": False}})
    assert res.status_code == 200, res.text
    assert res.json()["profile"]["show_on_site"] is False
    res = _patch(client, headers, {"profile": {"bio": "再改一次"}})
    assert res.json()["profile"]["show_on_site"] is False

    # nickname + profile 同提：两项都生效，响应带 nickname 与完整 profile
    res = _patch(client, headers, {"nickname": "新昵称", "profile": {"bio": "昵称同改"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["nickname"] == "新昵称"
    assert body["profile"]["bio"] == "昵称同改"

    # me 回显落库后的完整 profile
    res = client.get("/api/auth/me", headers=headers)
    me = res.json()
    assert me["nickname"] == "新昵称"
    assert me["profile"]["bio"] == "昵称同改"
    assert me["profile"]["phone"] == "13800138000"

    # 校验失败不落库：非法 patch 后库内 profile 保持上次成功状态
    res = _patch(client, headers, {"profile": {"email": "bad-email"}})
    assert res.status_code == 400
    res = client.get("/api/auth/me", headers=headers)
    assert res.json()["profile"]["bio"] == "昵称同改"


def test_profile_patch_gated_for_initial_password(client, cid):
    """pw_changed=0（初始密码）会话：profile 更新同样被强制改密门禁拦住。"""
    data = _make_applicant(client, cid, "08264722", "门禁考生")
    headers = {"Authorization": f"Bearer {data['token']}"}
    res = _patch(client, headers, {"nickname": "想改名"})
    assert res.status_code == 403
    assert res.json()["detail"] == "请先修改初始密码"
    res = _patch(client, headers, {"profile": {"bio": "想填资料"}})
    assert res.status_code == 403


# ---------- admin 设 role=member ----------


def test_admin_sets_role_member(client, cid, admin_headers):
    student = "08264730"
    _make_applicant(client, cid, student, "成员考生")

    # 非法角色仍 400
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "mentor"}, headers=admin_headers)
    assert res.status_code == 400

    # 设为 member → 200，DB 落库，登录后 role=member
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "member"}, headers=admin_headers)
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True}
    with db.connection() as conn:
        role = conn.execute("SELECT role FROM account WHERE student_id = ?",
                            (student,)).fetchone()["role"]
    assert role == "member"
    data = _unified_login(client, student)
    assert data["role"] == "member"
    assert data["name"] == "成员考生"  # 报名行还在，payload 照常携带

    # 审计照常记录角色流转
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT detail FROM audit_log WHERE target = ? AND action = 'set_role'"
            " ORDER BY id DESC LIMIT 1", (student,)).fetchall()
    assert rows[0]["detail"] == "applicant -> member"

    # 角色筛选 role=member 能查到
    res = client.get("/api/admin/accounts", params={"role": "member"},
                     headers=admin_headers)
    ids = [i["student_id"] for i in res.json()["items"]]
    assert student in ids
    assert all(i["role"] == "member" for i in res.json()["items"])

    # 还原现场：降回 applicant
    res = client.post(f"/api/admin/accounts/{student}/role",
                      json={"role": "applicant"}, headers=admin_headers)
    assert res.status_code == 200, res.text


# ---------- list_accounts.profile_filled ----------


def test_list_accounts_profile_filled(client, cid, admin_headers):
    filled, partial, empty = "08264740", "08264741", "08264742"
    for sid in (filled, partial, empty):
        _make_applicant(client, cid, sid, "名单考生")

    def _filled_of(sid):
        res = client.get("/api/admin/accounts", headers=admin_headers)
        assert res.status_code == 200, res.text
        items = {i["student_id"]: i for i in res.json()["items"]}
        return items[sid]["profile_filled"]

    # 初始均为未填
    assert _filled_of(filled) is False
    assert _filled_of(partial) is False
    assert _filled_of(empty) is False

    # 单字段草稿绝不代表完成填报。
    data = _unified_login(client, partial)
    res = change_password(client, partial, partial, "List#12345", data["token"])
    assert res.status_code == 200, res.text
    p_headers = {"Authorization": f"Bearer {data['token']}"}
    res = _patch(client, p_headers, {"profile": {"birth_date": "2005-03"}})
    assert res.status_code == 200, res.text
    assert _filled_of(partial) is False

    # 只填 show_on_site（不在判定键内）不算已填
    res = _patch(client, p_headers, {"profile": {"birth_date": "", "show_on_site": True}})
    assert res.status_code == 200, res.text
    assert _filled_of(partial) is False

    for key, value in (("political_status", "群众"), ("phone", "13900139000"),
                       ("email", "a@b.co"), ("github", "https://github.com/a")):
        res = _patch(client, p_headers, {"profile": {key: value}})
        assert res.status_code == 200, res.text
        assert _filled_of(partial) is False, key
        res = _patch(client, p_headers, {"profile": {key: ""}})
        assert res.status_code == 200, res.text
        assert _filled_of(partial) is False, key

    res = _patch(client, p_headers, {"profile": {"email": "real@example.com"}})
    assert res.status_code == 200, res.text
    assert _filled_of(partial) is False


def test_meeting_topics_save_reload_clear_and_validation(client, admin_headers):
    student = "08264750"
    headers = _member_headers(client, student)
    assert client.get("/api/auth/me", headers=headers).json()["profile"] == {}
    topics = ["name_values", "resources", "projects", "membership", "next_recruitment",
              "getting_started", "time_commitment", "start_project", "credit", "feedback", "other"]
    answer = {"meeting_topics": topics, "meeting_other": "想" * 1000}
    res = _patch(client, headers, {"profile": answer})
    assert res.status_code == 200, res.text
    for key, value in answer.items():
        assert res.json()["profile"][key] == value
    assert client.get("/api/auth/me", headers=headers).json()["profile"] == res.json()["profile"]
    assert _unified_login(client, student)["profile"] == res.json()["profile"]
    items = client.get("/api/admin/accounts", headers=admin_headers).json()["items"]
    assert next(item for item in items if item["student_id"] == student)["profile_filled"] is False

    for key, values in (
        ("meeting_topics", [None, "resources", {}, True, 1, [1], [None], [[]], [{}], [True], ["unknown"]]),
        ("meeting_other", [None, 1, True, [], {}, "字" * 1001]),
    ):
        for value in values:
            res = _patch(client, headers, {"profile": {key: value}})
            assert res.status_code == 400, (key, value, res.text)
    stored = client.get("/api/auth/me", headers=headers).json()["profile"]
    assert all(stored[key] == value for key, value in answer.items())

    for answer in ({"meeting_topics": [], "meeting_other": "独立填写，不勾选其他"},
                   {"meeting_topics": [], "meeting_other": ""}):
        res = _patch(client, headers, {"profile": answer})
        assert res.status_code == 200, res.text
        stored = client.get("/api/auth/me", headers=headers).json()["profile"]
        assert all(stored[key] == value for key, value in answer.items())


def test_meeting_topics_initial_password_gate(client, cid):
    data = _make_applicant(client, cid, "08264751")
    headers = {"Authorization": f"Bearer {data['token']}"}
    res = _patch(client, headers, {"profile": {"meeting_topics": ["resources"], "meeting_other": "想听"}})
    assert res.status_code == 403
    assert client.get("/api/auth/me", headers=headers).json()["profile"] == {}


def test_meeting_question_complete_optional_and_last():
    from html.parser import HTMLParser

    class FormParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.controls = []

        def handle_starttag(self, tag, attrs):
            if tag in ("input", "textarea", "select", "button"):
                self.controls.append((tag, dict(attrs)))

    page = (Path(__file__).parent.parent / "static" / "account.html").read_text(encoding="utf-8")
    member = page.split('id="member-card"', 1)[1].split('id="project-card"', 1)[0]
    section = member.split('<section id="member-meeting-section"', 1)[1].split('</section>', 1)[0]
    assert "非必填，不公开" in section
    labels = [
        "这次周日晚的见面会，你想听到哪些内容？（可多选）",
        "社团名称、核心理念和做事方法",
        "社团能提供哪些资源、指导和交流机会",
        "有哪些项目、竞赛或研究可以参与，怎么加入",
        "社团成员和实验室成员分别有哪些安排与要求",
        "下学期的筛选和招新安排",
        "基础不多、方向没定，现阶段可以从哪里开始",
        "课程学习与社团任务怎么兼顾，通常需要投入多少时间",
        "有自己的想法，怎么发起项目、找队友或申请支持",
        "项目成果怎么署名，成员的贡献怎么认定",
        "对社团的安排有建议或异议，可以怎么提出和讨论",
        "其他：自由文本",
    ]
    positions = [section.index(label) for label in labels]
    assert positions == sorted(positions)
    parser = FormParser()
    parser.feed(section)
    checkboxes = [attrs for tag, attrs in parser.controls if tag == "input"]
    assert [attrs["value"] for attrs in checkboxes] == [
        "name_values", "resources", "projects", "membership", "next_recruitment",
        "getting_started", "time_commitment", "start_project", "credit", "feedback", "other"]
    assert all(attrs["type"] == "checkbox" and attrs["name"] == "meeting_topics" for attrs in checkboxes)
    assert all("required" not in attrs for _, attrs in parser.controls)
    assert parser.controls[-1] == ("textarea", {"id": "member-meeting-other", "maxlength": "1000", "rows": "3"})
    after_section = member.split('</section>', 1)[1]
    assert after_section.index('id="member-save-btn"') >= 0
    parser = FormParser()
    parser.feed(after_section)
    assert parser.controls == [("button", {"class": "btn secondary", "id": "member-save-btn"}),
                               ("button", {"class": "btn", "id": "member-complete-btn"})]
    assert member.index('id="member-show-on-site"') < member.index('id="member-meeting-section"')

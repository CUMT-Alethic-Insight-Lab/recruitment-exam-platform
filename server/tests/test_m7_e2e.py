"""M7 全链路集成验证（可重复运行）：

管理员登录 → 建批次(open) → latest-open → 考生问卷注册 → 考生登录
（模拟前端 PBKDF2 客户端散列）→ enroll → 模拟清洗后上传 → 管理员评审列表
→ 打分 → 状态流转/收口 → 页面路由 200 且无 __PREFIX__ 残留。
"""

import hashlib

CLEANED_CONTENT = (
    '{"event": "kimi_code_session", "student": "m7-e2e", "turns": [1, 2, 3]}\n'
)

# 与前端一致的 PBKDF2 参数（见 app/auth.py: PBKDF2_ITERATIONS=100_000, DKLEN=32）
_PBKDF2_ITERATIONS = 100_000
_PBKDF2_DKLEN = 32


def client_hash(password: str, salt_hex: str) -> str:
    """模拟浏览器端：PBKDF2-HMAC-SHA256(密码, salt, 100000, 32) 的 hex。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex),
        _PBKDF2_ITERATIONS, _PBKDF2_DKLEN,
    ).hex()


def _admin_login(client):
    """管理员登录（学号 00000000；强制改密门禁下由 conftest 先把初始密码收敛为
    测试密码，此处直接以测试密码登录），返回 Bearer headers。"""
    from conftest import ADMIN_TEST_PASSWORD, ensure_admin_password_changed
    ensure_admin_password_changed(client)
    res = client.get("/api/auth/salt", params={"student_id": "00000000"})
    assert res.status_code == 200, res.text
    salt = res.json()["salt"]
    assert len(salt) == 32
    res = client.post("/api/auth/login", json={
        "student_id": "00000000",
        "client_hash": client_hash(ADMIN_TEST_PASSWORD, salt),
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["student_id"] == "00000000"
    assert data["role"] == "admin"
    assert data["pw_changed"] is True  # 初始密码已由 conftest 强制改密流程收敛
    assert data["token"]
    return {"Authorization": f"Bearer {data['token']}"}


def _applicant_login(client, cid, student_id):
    """考生登录：先取 salt，再算 PBKDF2 client_hash，换 Bearer token。"""
    res = client.get("/api/auth/salt",
                     params={"cid": cid, "student_id": student_id})
    assert res.status_code == 200, res.text
    salt = res.json()["salt"]
    res = client.post(f"/api/campaigns/{cid}/auth/login", json={
        "student_id": student_id,
        "client_hash": client_hash(student_id, salt),  # 初始密码 = 学号
    })
    assert res.status_code == 200, res.text
    return res.json()


CID = "m7e2e"
STUDENT_ID = "08268801"


def test_m7_full_chain(client):
    # ---------- 1. 管理员登录 ----------
    admin_headers = _admin_login(client)

    # ---------- 2. 建批次（创建即 open） ----------
    res = client.post("/api/campaigns", headers=admin_headers,
                      json={"id": CID, "name": "M7 全链路批次", "year": 2026})
    assert res.status_code == 201, res.text
    campaign = res.json()
    assert campaign["id"] == CID
    assert campaign["status"] == "open"
    assert campaign["name"] == "M7 全链路批次"
    assert campaign["expires_at"]

    # ---------- 3. latest-open 公开接口返回它 ----------
    res = client.get("/api/campaigns/latest-open")
    assert res.status_code == 200, res.text
    assert res.json()["id"] == CID
    assert res.json()["title"] == "M7 全链路批次"

    # ---------- 4. 考生提交报名问卷（初始密码=学号） ----------
    res = client.post(f"/api/campaigns/{CID}/applications", json={
        "student_id": STUDENT_ID,
        "name": "链路考生",
        "gender": "female",
        "class_name": "计科244",
        "qq": "60088001",
        "lab_intent": True,
        "domains": ["llm_arch", "agent"],
        "focus_next_year": "project",
        "expectation": "M7 全链路集成验证",
    })
    assert res.status_code == 200, res.text
    app_out = res.json()
    assert app_out["student_id"] == STUDENT_ID
    assert app_out["campaign_id"] == CID
    assert app_out["status"] == "applied"
    assert app_out["domains"] == ["llm_arch", "agent"]

    # ---------- 5. 考生登录（完整模拟 PBKDF2 客户端散列） ----------
    data = _applicant_login(client, CID, STUDENT_ID)
    assert data["student_id"] == STUDENT_ID
    assert data["name"] == "链路考生"
    assert data["pw_changed"] is False
    applicant_headers = {"Authorization": f"Bearer {data['token']}"}

    # 错误密码（不同 salt 的 PBKDF2 结果）应 401
    res = client.post(f"/api/campaigns/{CID}/auth/login", json={
        "student_id": STUDENT_ID,
        "client_hash": client_hash("wrong-password", "ab" * 16),
    })
    assert res.status_code == 401

    # ---------- 6. enroll（Bearer 会话，body 只有 track） ----------
    # 未带 token → 401
    res = client.post(f"/api/campaigns/{CID}/exam/enroll", json={"track": "track2"})
    assert res.status_code == 401

    # 管理员先录一道 track2 题目，确保开考能取到题
    res = client.post("/api/questions", headers=admin_headers, json={
        "domain": "llm_arch", "track": "track2",
        "title": "M7 E2E 题", "body": "请现场部署一个最小推理服务", "tags": ["e2e"],
    })
    assert res.status_code == 201, res.text

    res = client.post(f"/api/campaigns/{CID}/exam/enroll",
                      json={"track": "track2"}, headers=applicant_headers)
    assert res.status_code == 200, res.text
    exam = res.json()
    assert exam["session_id"] and exam["session_token"] and exam["machine_id"]
    assert exam["track"] == "track2"
    assert exam["domain_tags"] == ["llm_arch", "agent"]
    assert len(exam["questions"]) >= 1
    assert exam["questions"][0]["domain"] == "llm_arch"
    assert exam["rerolls_left"] >= 0
    sid, session_token = exam["session_id"], exam["session_token"]

    # ---------- 7. 模拟清洗后上传（.jsonl cleaned 件 + 会话凭证） ----------
    res = client.post(
        f"/api/campaigns/{CID}/sessions",
        data={"session_id": sid, "repo_url": "https://github.com/m7e2e/work"},
        files={"cleaned": ("cleaned.jsonl", CLEANED_CONTENT.encode("utf-8"),
                           "application/octet-stream")},
        headers={"X-Session-Token": session_token},
    )
    assert res.status_code == 200, res.text
    uploaded = res.json()
    assert uploaded["id"] == sid
    assert uploaded["status"] == "uploaded"
    assert uploaded["uploaded_at"]
    assert uploaded["repo_url"] == "https://github.com/m7e2e/work"

    # 重复交卷 → 409
    res = client.post(
        f"/api/campaigns/{CID}/sessions",
        data={"session_id": sid},
        files={"cleaned": ("cleaned.jsonl", CLEANED_CONTENT.encode("utf-8"),
                           "application/octet-stream")},
        headers={"X-Session-Token": session_token},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "already_uploaded"

    # 上传后报名状态流转为 tested
    res = client.get(f"/api/campaigns/{CID}/applications/{STUDENT_ID}", headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["status"] == "tested"

    # ---------- 8. 管理员评审列表能看到该会话 ----------
    res = client.get(f"/api/campaigns/{CID}/review/sessions", headers=admin_headers)
    assert res.status_code == 200, res.text
    review_rows = [r for r in res.json() if r["session_id"] == sid]
    assert len(review_rows) == 1
    row = review_rows[0]
    assert row["student_id"] == STUDENT_ID
    assert row["student_name"] == "链路考生"
    assert row["track"] == "track2"
    assert row["uploaded_at"]
    assert row["increment_score"] is None  # 尚未打分

    # 管理员可下载 cleaned 件，内容一致
    res = client.get(f"/api/campaigns/{CID}/sessions/{sid}/files/cleaned",
                     headers=admin_headers)
    assert res.status_code == 200
    assert res.content == CLEANED_CONTENT.encode("utf-8")

    # ---------- 9. 管理员打分 ----------
    res = client.post(f"/api/campaigns/{CID}/sessions/{sid}/score",
                      headers=admin_headers,
                      json={"increment_score": 88.5, "human_rate": 92.0,
                            "comment": "完成度高", "scorer": "00000000"})
    assert res.status_code == 200, res.text
    score = res.json()
    assert score["session_id"] == sid
    assert score["increment_score"] == 88.5
    assert score["human_rate"] == 92.0
    assert score["scorer"] == "00000000"
    assert score["created_at"] <= score["updated_at"]

    # 评审列表回显分数
    res = client.get(f"/api/campaigns/{CID}/review/sessions", headers=admin_headers)
    row = next(r for r in res.json() if r["session_id"] == sid)
    assert row["increment_score"] == 88.5
    assert row["human_rate"] == 92.0
    assert row["comment"] == "完成度高"

    # ---------- 10. 状态流转 + 收口 ----------
    res = client.patch(f"/api/campaigns/{CID}/applications/{STUDENT_ID}/status",
                       headers=admin_headers, json={"status": "admitted"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "admitted"

    # 录取公示含该考生
    res = client.get(f"/api/campaigns/{CID}/export/notice", headers=admin_headers)
    assert res.status_code == 200
    assert "链路考生（计科244）" in res.content.decode("utf-8")

    # 收口批次
    res = client.patch(f"/api/campaigns/{CID}", headers=admin_headers,
                       json={"status": "closed"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "closed"

    # 收口后 latest-open 不再返回该批次
    res = client.get("/api/campaigns/latest-open")
    assert res.json().get("id") != CID

    # 收口后无法再开考
    res = client.post(f"/api/campaigns/{CID}/exam/enroll",
                      json={"track": "track1"}, headers=applicant_headers)
    assert res.status_code == 403

    # ---------- 11. 页面路由 200 且无 __PREFIX__ 残留 ----------
    for path in ("/", "/login", "/account", f"/apply/{CID}", "/admin"):
        res = client.get(path)
        assert res.status_code == 200, f"{path} 应返回页面"
        assert "__PREFIX__" not in res.text, f"{path} 残留 __PREFIX__ 占位符"

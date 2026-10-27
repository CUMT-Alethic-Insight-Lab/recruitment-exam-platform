from app import db
from test_m27_member_profile import _member_headers

FULL = {"birth_date": "2005-01", "political_status": "共青团员",
        "college": "计算机科学与技术学院/人工智能学院", "class_name": "计算机科学与技术2023-一班",
        "enroll_year": 2023, "grad_year": 2030, "phone": "13800138000", "email": "test@example.com",
        "meeting_topics": [], "meeting_other": ""}


def test_completion_lifecycle_and_identity(client, admin_headers):
    headers = _member_headers(client, "00009991")
    base = "/api/auth/profile"
    result = client.post(base, headers=headers, json={"profile": {"phone": FULL["phone"]}})
    assert result.status_code == 200
    assert result.json()["profile_completed"] is False
    result = client.post(base + "/complete", headers=headers, json={"profile": FULL})
    assert "identity" in result.json()["detail"]["fields"]
    with db.connection() as conn:
        conn.execute("INSERT INTO membership (student_id,name,source,created_at) VALUES ('00009991','可信姓名','test','now')")
    for patch, key in (({"class_name": "计科 2401 班"}, "class_name"), ({"college": "计算机学院"}, "college"),
                       ({"birth_date": "2025-01"}, "birth_date"), ({"grad_year": 2020}, "grad_year"),
                       ({"email": "", "school_email": ""}, "email")):
        result = client.post(base + "/complete", headers=headers, json={"profile": {**FULL, **patch}})
        assert result.status_code == 400
        assert key in result.json()["detail"]["fields"]
    result = client.post(base, headers=headers, json={"profile": FULL})
    assert result.json()["profile_completed"] is False
    result = client.post(base + "/complete", headers=headers, json={})
    assert result.status_code == 200, result.text
    assert result.json()["profile_completed"] is True
    me = client.get('/api/auth/me', headers=headers).json()
    assert me["profile_completed"] is True
    assert me["member_identity"]["name"] == '可信姓名'
    assert me["profile"]["meeting_topics"] == []
    items = client.get('/api/admin/accounts', headers=admin_headers).json()["items"]
    assert next(i for i in items if i['student_id'] == '00009991')['profile_filled'] is True
    result = client.post(base, headers=headers, json={"profile": {"email": ""}})
    assert result.json()["profile_completed"] is False
    assert result.json()["profile"]["birth_date"] == FULL["birth_date"]
    assert client.post(base, headers=headers, json={"profile": {"profile_completed": True}}).status_code == 400


def test_admin_own_completion(client, admin_headers):
    me = client.get('/api/auth/me', headers=admin_headers).json()
    with db.connection() as conn:
        conn.execute("INSERT INTO membership (student_id,name,source,created_at) VALUES (?, '管理员本人', 'test', 'now')", (me['student_id'],))
    result = client.post('/api/auth/profile/complete', headers=admin_headers, json={"profile": FULL})
    assert result.status_code == 200, result.text
    assert result.json()["profile_completed"] is True
    result = client.post('/api/auth/profile/complete', headers=admin_headers,
                         json={"profile": {"class_name": "法学2023-二班", "college": "人文与艺术学院"}})
    assert result.status_code == 200, result.text
    result = client.post('/api/auth/profile', headers=admin_headers, json={"nickname": "别人名字"})
    assert result.status_code == 200
    assert client.get('/api/auth/me', headers=admin_headers).json()['name'] == '管理员本人'

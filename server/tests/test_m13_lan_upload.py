"""upload_mode=lan 的轻量交卷标记（POST /{cid}/sessions/{sid}/lan-uploaded）测试。

文件存考场局域网接收机不传云端，云端只记状态；与正式上传同口径校验凭证。
"""

import sys
from pathlib import Path

from conftest import applicant_headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _app_payload(student_id):
    return {
        "student_id": student_id,
        "name": f"局域网交卷{student_id[-2:]}",
        "gender": "male",
        "class_name": "计科26-1",
        "qq": f"10000{student_id[-3:]}",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "M13 局域网交卷",
    }


def _enroll_draft(client, cid, student_id, track="track1"):
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student_id))
    assert res.status_code == 200
    headers = applicant_headers(client, cid, student_id)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": track}, headers=headers)
    assert res.status_code == 200
    return res.json()


def _mark(client, cid, sid, token, repo_url=None):
    body = {"repo_url": repo_url} if repo_url else {}
    return client.post(f"/api/campaigns/{cid}/sessions/{sid}/lan-uploaded",
                       json=body, headers={"X-Session-Token": token})


def test_lan_mark_success(client, cid, admin_headers):
    data = _enroll_draft(client, cid, "08269501")
    sid, token = data["session_id"], data["session_token"]
    res = _mark(client, cid, sid, token, repo_url="https://github.com/test/lan")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["status"] == "uploaded"
    assert out["uploaded_at"]
    assert out["repo_url"] == "https://github.com/test/lan"
    assert out["cleaned_file_path"] is None
    assert out["attachment_file_path"] is None
    # 报名状态联动 tested
    res = client.get(f"/api/campaigns/{cid}/applications",
                     params={"q": "08269501"}, headers=admin_headers)
    assert res.status_code == 200
    item = next(x for x in res.json() if x["student_id"] == "08269501")
    assert item["status"] == "tested"
    # 评审列表带 stored_lan 标记
    res = client.get(f"/api/campaigns/{cid}/review/sessions", headers=admin_headers)
    assert res.status_code == 200
    item = next(x for x in res.json() if x["session_id"] == sid)
    assert item["stored_lan"] is True


def test_lan_mark_idempotent_conflict(client, cid):
    data = _enroll_draft(client, cid, "08269502")
    sid, token = data["session_id"], data["session_token"]
    assert _mark(client, cid, sid, token).status_code == 200
    res = _mark(client, cid, sid, token)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "already_uploaded"


def test_lan_mark_wrong_token_401(client, cid):
    data = _enroll_draft(client, cid, "08269503")
    sid = data["session_id"]
    res = _mark(client, cid, sid, "wrong-token")
    assert res.status_code == 401


def test_lan_mark_missing_token_401(client, cid):
    data = _enroll_draft(client, cid, "08269504")
    sid = data["session_id"]
    res = client.post(f"/api/campaigns/{cid}/sessions/{sid}/lan-uploaded", json={})
    assert res.status_code == 401


def test_lan_mark_bad_repo_url_400(client, cid):
    data = _enroll_draft(client, cid, "08269505")
    sid, token = data["session_id"], data["session_token"]
    res = _mark(client, cid, sid, token, repo_url="ftp://nope")
    assert res.status_code == 400


def test_lan_mark_after_cloud_upload_conflict(client, cid):
    """云端已收到正式交卷的会话不能再打局域网标记。"""
    data = _enroll_draft(client, cid, "08269506")
    sid, token = data["session_id"], data["session_token"]
    files = {"cleaned": ("cleaned.jsonl", b'{"e":1}\n', "application/octet-stream")}
    res = client.post(f"/api/campaigns/{cid}/sessions", data={"session_id": sid},
                      files=files, headers={"X-Session-Token": token})
    assert res.status_code == 200
    res = _mark(client, cid, sid, token)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "already_uploaded"

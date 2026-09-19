import sys
from pathlib import Path

from app import config
from conftest import applicant_headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLEANED_CONTENT = '{"event": "kimi_code_session", "turns": [1, 2, 3]}\n'


def _app_payload(student_id):
    return {
        "student_id": student_id,
        "name": f"测试交卷{student_id[-2:]}",
        "gender": "male",
        "class_name": "计科244",
        "qq": "600600600",
        "lab_intent": True,
        "domains": ["llm_arch", "ai_safety"],
        "focus_next_year": "project",
        "expectation": "M3 交卷测试",
    }


def _enroll_draft(client, cid, student_id, track="track1"):
    res = client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student_id))
    assert res.status_code == 200
    headers = applicant_headers(client, cid, student_id)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": track}, headers=headers)
    assert res.status_code == 200
    return res.json()


def _upload(client, cid, sid, token=None, cleaned=True, repo_url=""):
    files = {}
    if cleaned:
        files["cleaned"] = ("cleaned.jsonl", CLEANED_CONTENT.encode("utf-8"),
                            "application/octet-stream")
    data = {"session_id": sid}
    if repo_url:
        data["repo_url"] = repo_url
    headers = {"X-Session-Token": token} if token else {}
    return client.post(f"/api/campaigns/{cid}/sessions", data=data,
                       files=files, headers=headers)


def test_upload_success(client, cid):
    data = _enroll_draft(client, cid, "08267001")
    sid, token = data["session_id"], data["session_token"]
    res = _upload(client, cid, sid, token=token, repo_url="https://github.com/test/repo")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["status"] == "uploaded"
    assert out["uploaded_at"]
    assert out["repo_url"] == "https://github.com/test/repo"

    cleaned_path = Path(config.DATA_DIR) / "uploads" / cid / sid / "cleaned.jsonl"
    assert cleaned_path.is_file()
    assert cleaned_path.read_text(encoding="utf-8") == CLEANED_CONTENT


def test_upload_without_token_401(client, cid):
    data = _enroll_draft(client, cid, "08267002")
    res = _upload(client, cid, data["session_id"], token=None)
    assert res.status_code == 401


def test_upload_wrong_token_401(client, cid):
    data = _enroll_draft(client, cid, "08267003")
    res = _upload(client, cid, data["session_id"], token="wrong-token")
    assert res.status_code == 401


def test_upload_duplicate_409_already_uploaded(client, cid):
    data = _enroll_draft(client, cid, "08267004")
    sid, token = data["session_id"], data["session_token"]
    res = _upload(client, cid, sid, token=token)
    assert res.status_code == 200
    res = _upload(client, cid, sid, token=token)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "already_uploaded"


def test_upload_missing_cleaned_422(client, cid):
    data = _enroll_draft(client, cid, "08267005")
    res = _upload(client, cid, data["session_id"], token=data["session_token"],
                  cleaned=False)
    assert res.status_code in (400, 422)


def test_upload_bad_repo_url_400(client, cid):
    data = _enroll_draft(client, cid, "08267006")
    res = _upload(client, cid, data["session_id"], token=data["session_token"],
                  repo_url="ftp://not-http")
    assert res.status_code == 400


def test_admin_download_cleaned(client, cid, admin_headers):
    data = _enroll_draft(client, cid, "08267007")
    sid, token = data["session_id"], data["session_token"]
    res = _upload(client, cid, sid, token=token)
    assert res.status_code == 200

    res = client.get(f"/api/campaigns/{cid}/sessions/{sid}/files/cleaned",
                     headers=admin_headers)
    assert res.status_code == 200
    assert res.content == CLEANED_CONTENT.encode("utf-8")

    # 未带管理凭证不可下载
    res = client.get(f"/api/campaigns/{cid}/sessions/{sid}/files/cleaned")
    assert res.status_code == 401
    # 无附件的会话下载附件应 404
    res = client.get(f"/api/campaigns/{cid}/sessions/{sid}/files/attachment",
                     headers=admin_headers)
    assert res.status_code == 404


def test_admin_download_attachment(client, cid, admin_headers):
    data = _enroll_draft(client, cid, "08267008")
    sid, token = data["session_id"], data["session_token"]
    att_bytes = b"PK\x03\x04 fake-zip-bytes"
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": sid},
        files={"cleaned": ("cleaned.md", b"# creative work\n", "text/markdown"),
               "attachment": ("work.zip", att_bytes, "application/zip")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text
    assert res.json()["attachment_file_path"]

    res = client.get(f"/api/campaigns/{cid}/sessions/{sid}/files/attachment",
                     headers=admin_headers)
    assert res.status_code == 200
    assert res.content == att_bytes

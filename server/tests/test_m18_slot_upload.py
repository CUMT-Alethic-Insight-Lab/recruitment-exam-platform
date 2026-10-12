"""M18：逐题提交（slot）——同一考试会话按题各传各的清洗件，评审按题调取。

- slot >= 0 的上传按槽位幂等（同槽位重复 → 409 already_uploaded）
- 同一会话可连续上传多个槽位（不受整卷 already_uploaded 限制）
- 评审列表带 slots；cleaned?slot=N 与 files/cleaned?slot=N 按题取件
- 作废的会话拒绝任何槽位上传；旧的整卷（无 slot）管线不受影响
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import applicant_headers  # noqa: E402


def _app_payload(student_id="08265501", name="分题一号"):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "male",
        "class_name": "计科244",
        "qq": "700700700",
        "lab_intent": True,
        "domains": ["llm_arch", "rl"],
        "focus_next_year": "project",
        "expectation": "测试报名",
    }


def _enroll(client, cid, sid):
    client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(sid))
    headers = applicant_headers(client, cid, sid)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    return body["session_id"], body["session_token"]


def _upload_slot(client, cid, session_id, token, slot, text):
    return client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": session_id, "slot": str(slot),
              "repo_url": "https://github.com/x/y"},
        files={"cleaned": (f"cleaned_q{slot}.jsonl",
                           f'{{"q": {slot}, "text": "{text}"}}\n'.encode(),
                           "application/octet-stream")},
        headers={"X-Session-Token": token},
    )


def test_slot_uploads_independent_and_idempotent(client, cid, admin_headers):
    session_id, token = _enroll(client, cid, "08265501")

    for i, text in enumerate(("第一题", "第二题", "第三题")):
        res = _upload_slot(client, cid, session_id, token, i, text)
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "uploaded"

    # 同槽位重复 → 409 already_uploaded（客户端视为成功）
    res = _upload_slot(client, cid, session_id, token, 1, "重复")
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "already_uploaded"

    # 评审列表带 slots，且不判为局域网存储
    res = client.get(f"/api/campaigns/{cid}/review/sessions",
                     headers=admin_headers)
    assert res.status_code == 200
    item = next(s for s in res.json() if s["session_id"] == session_id)
    assert sorted(item["slots"].keys()) == ["0", "1", "2"]
    assert item["stored_lan"] is False
    assert item["repo_url"] == "https://github.com/x/y"

    # 按槽位取清洗件：各题内容互不串扰
    for i, text in enumerate(("第一题", "第二题", "第三题")):
        res = client.get(
            f"/api/campaigns/{cid}/sessions/{session_id}/cleaned?slot={i}",
            headers=admin_headers)
        assert res.status_code == 200
        events = res.json()
        assert events[0]["q"] == i and events[0]["text"] == text

    # 未上传的槽位 → 404
    res = client.get(
        f"/api/campaigns/{cid}/sessions/{session_id}/cleaned?slot=9",
        headers=admin_headers)
    assert res.status_code == 404

    # 按槽位下载
    res = client.get(
        f"/api/campaigns/{cid}/sessions/{session_id}/files/cleaned?slot=2",
        headers=admin_headers)
    assert res.status_code == 200
    assert "第三题" in res.text


def test_cleaned_download_md_format(client, cid, admin_headers):
    session_id, token = _enroll(client, cid, "08265504")
    payload = (
        '{"k": "user", "time": 1760000000000, "text": "你好"}\n'
        '{"k": "assistant", "time": 1760000001000, "text": "回复正文"}\n'
        '{"k": "tool", "time": 1760000002000, "name": "Read",'
        ' "arg": "README.md", "out_len": 42}\n'
    ).encode()
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": session_id},
        files={"cleaned": ("cleaned.jsonl", payload, "application/octet-stream")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text

    # format=md：服务端转换为 Markdown 报告，带 .md 文件名与 text/markdown 类型
    res = client.get(
        f"/api/campaigns/{cid}/sessions/{session_id}/files/cleaned?format=md",
        headers=admin_headers)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert 'filename="cleaned.md"' in res.headers["content-disposition"]
    assert "08265504" in res.text
    assert "## 👤 用户" in res.text
    assert "## 🤖 助手" in res.text
    assert "🔧 工具：Read" in res.text
    assert "（输出 42 字符）" in res.text

    # 默认 format=jsonl：原始 NDJSON 原样返回，不回归
    res = client.get(
        f"/api/campaigns/{cid}/sessions/{session_id}/files/cleaned",
        headers=admin_headers)
    assert res.status_code == 200
    assert "回复正文" in res.text
    assert "## 👤 用户" not in res.text

    # 非法格式 → 400
    res = client.get(
        f"/api/campaigns/{cid}/sessions/{session_id}/files/cleaned?format=pdf",
        headers=admin_headers)
    assert res.status_code == 400


def test_slot_download_md_filename(client, cid, admin_headers):
    session_id, token = _enroll(client, cid, "08265505")
    res = _upload_slot(client, cid, session_id, token, 1, "第二题")
    assert res.status_code == 200, res.text

    res = client.get(
        f"/api/campaigns/{cid}/sessions/{session_id}"
        "/files/cleaned?slot=1&format=md",
        headers=admin_headers)
    assert res.status_code == 200
    assert 'filename="cleaned_q1.md"' in res.headers["content-disposition"]
    assert "第 2 题" in res.text


def test_slot_upload_rejected_after_void(client, cid, admin_headers):
    session_id, token = _enroll(client, cid, "08265502")
    res = _upload_slot(client, cid, session_id, token, 0, "先交一题")
    assert res.status_code == 200

    res = client.post(f"/api/campaigns/{cid}/exam/void",
                      json={"student_id": "08265502"}, headers=admin_headers)
    assert res.status_code == 200

    res = _upload_slot(client, cid, session_id, token, 1, "作废后再交")
    assert res.status_code == 409
    assert "作废" in res.json()["detail"]


def test_legacy_whole_upload_unaffected(client, cid, admin_headers):
    session_id, token = _enroll(client, cid, "08265503")
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": session_id},
        files={"cleaned": ("cleaned.jsonl", b'{"q": "whole"}\n',
                           "application/octet-stream")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text

    res = client.get(f"/api/campaigns/{cid}/sessions/{session_id}/cleaned",
                     headers=admin_headers)
    assert res.status_code == 200
    assert res.json()[0]["q"] == "whole"

    # 整卷已交后再整卷交 → 409
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": session_id},
        files={"cleaned": ("cleaned.jsonl", b"{}\n",
                           "application/octet-stream")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "already_uploaded"

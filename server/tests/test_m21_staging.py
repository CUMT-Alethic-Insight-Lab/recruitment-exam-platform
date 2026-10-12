"""M21：静态优先云端 + 动态优先 lab —— edge 暂存、转发、lab ingest 收口。

- edge 模式交卷不再 503：multipart 原样落 data/staging/{session_id}/，
  响应 202 pending_sync，不写主库；热备库能查到会话时仍校验会话凭证
- staging_forward：lab 可达才转发，成功即删（云端不留数据），失败/不可达保留
- ingest-staged：共享密钥鉴权；同 (session, slot, hash) 台账幂等 ack；
  新件走与正常交卷一致的落库收敛点（uploaded/tested/slots），source=staged
- 管理端会话列表 edge 模式合并 staging 显示 pending_sync；作废仍只对主库会话开放
"""

import hashlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import applicant_headers  # noqa: E402

from app import config, staging, staging_forward, submission  # noqa: E402


def _app_payload(student_id, name="暂存考生"):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "male",
        "class_name": "计科244",
        "qq": "700700701",
        "lab_intent": True,
        "domains": ["llm_arch", "rl"],
        "focus_next_year": "project",
        "expectation": "M21 暂存测试",
    }


def _enroll(client, cid, student_id):
    client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student_id))
    headers = applicant_headers(client, cid, student_id)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    return body["session_id"], body["session_token"]


def _post_upload(client, cid, session_id, token, slot=-1, text="你好",
                 repo_url="", token_override=None):
    data = {"session_id": session_id, "slot": str(slot)}
    if repo_url:
        data["repo_url"] = repo_url
    fname = f"cleaned_q{slot}.jsonl" if slot >= 0 else "cleaned.jsonl"
    return client.post(
        f"/api/campaigns/{cid}/sessions",
        data=data,
        files={"cleaned": (fname,
                           f'{{"k": "user", "text": "{text}"}}\n'.encode(),
                           "application/octet-stream")},
        headers={"X-Session-Token": token_override or token},
    )


@pytest.fixture
def edge_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROLE", "edge")
    monkeypatch.setattr(config, "IS_EDGE", True)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


# ---------- edge 暂存 ----------

def test_edge_upload_staged_pending_sync(client, cid, edge_mode):
    sid, token = _enroll(client, cid, "08271101")
    res = _post_upload(client, cid, sid, token, slot=-1, text="整卷",
                       repo_url="https://github.com/x/edge")
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "pending_sync"

    sdir = edge_mode / "staging" / sid
    assert (sdir / "cleaned.jsonl").is_file()
    meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["campaign_id"] == cid
    assert meta["session_id"] == sid
    assert meta["student_id"] == "08271101"
    assert meta["repo_url"] == "https://github.com/x/edge"
    entry = meta["uploads"]["-1"]
    fmeta = entry["files"][0]
    digest = hashlib.sha256((sdir / "cleaned.jsonl").read_bytes()).hexdigest()
    assert fmeta["sha256"] == digest

    # 不写主库：会话仍是 draft
    headers = applicant_headers(client, cid, "08271101")
    res = client.get(f"/api/campaigns/{cid}/exam/my_sessions", headers=headers)
    assert res.status_code == 200
    assert res.json()[0]["status"] == "draft"


def test_edge_slot_upload_staged(client, cid, edge_mode):
    sid, token = _enroll(client, cid, "08271102")
    res = _post_upload(client, cid, sid, token, slot=2, text="第三题")
    assert res.status_code == 202
    sdir = edge_mode / "staging" / sid
    assert (sdir / "cleaned_q2.jsonl").is_file()
    meta = json.loads((sdir / "meta.json").read_text(encoding="utf-8"))
    assert meta["uploads"]["2"]["slot"] == 2


def test_edge_wrong_token_401(client, cid, edge_mode):
    sid, token = _enroll(client, cid, "08271103")
    res = _post_upload(client, cid, sid, token, token_override="wrong-token")
    assert res.status_code == 401
    assert not (edge_mode / "staging" / sid).exists()


def test_edge_unknown_session_still_staged(client, cid, edge_mode):
    """热备库同步窗口内的新会话查不到也照常暂存（不 503）。"""
    res = _post_upload(client, cid, "newsession01", "whatever", text="新会话")
    assert res.status_code == 202
    meta = json.loads(
        (edge_mode / "staging" / "newsession01" / "meta.json").read_text(
            encoding="utf-8"))
    assert meta["student_id"] == ""


def test_edge_bad_session_id_400(client, cid, edge_mode):
    res = _post_upload(client, cid, "../escape", "whatever")
    assert res.status_code == 400


def test_edge_list_sessions_pending_sync(client, cid, admin_headers, edge_mode):
    sid, token = _enroll(client, cid, "08271104")
    assert _post_upload(client, cid, sid, token, slot=0).status_code == 202
    # 纯暂存（主库无会话）的合成条目
    staging.stage_upload(
        campaign_id=cid, session_id="stageonly01", student_id="08271999",
        slot=-1, repo_url="", cleaned_name="cleaned.jsonl",
        cleaned_file=io.BytesIO(b'{"k": "user"}\n'))

    res = client.get(f"/api/campaigns/{cid}/exam/sessions",
                     headers=admin_headers)
    assert res.status_code == 200
    by_id = {s["id"]: s for s in res.json()}
    assert by_id[sid]["status"] == "pending_sync"  # 库里 draft 被暂存覆盖
    assert by_id["stageonly01"]["status"] == "pending_sync"
    assert by_id["stageonly01"]["student_id"] == "08271999"

    # 作废/重考仍只对主库 uploaded 开放：纯暂存学号在主库没有会话 → 404
    res = client.post(f"/api/campaigns/{cid}/exam/void",
                      json={"student_id": "08271999"}, headers=admin_headers)
    assert res.status_code == 404


# ---------- staging_forward ----------

def _stage_one(tmp_path, session_id="ses_fwd_01"):
    return staging.stage_upload(
        campaign_id="2026", session_id=session_id, student_id="08271001",
        slot=0, repo_url="", cleaned_name="cleaned_q0.jsonl",
        cleaned_file=io.BytesIO(b'{"k": "user"}\n'))


def test_forward_success_deletes(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LAB_ORIGIN_URL", "https://lab.example")
    monkeypatch.setattr(config, "STAGING_SECRET", "s3cret")
    sdir = _stage_one(tmp_path)
    posted = {}
    monkeypatch.setattr(staging_forward, "_http_get",
                        lambda url, timeout=15: (200, '{"ok": true}'))

    def fake_post(url, fields, files, timeout=300):
        posted["url"] = url
        posted["fields"] = fields
        posted["files"] = files
        return 200, '{"status": "ok"}'

    monkeypatch.setattr(staging_forward, "_http_post_ingest", fake_post)
    summary = staging_forward.run()
    assert summary["forwarded"] == ["ses_fwd_01"]
    assert not sdir.exists()  # 云端不留数据
    assert posted["url"] == "https://lab.example/api/internal/ingest-staged"
    assert posted["fields"]["session_id"] == "ses_fwd_01"
    manifest = json.loads(posted["fields"]["manifest"])
    assert "0" in manifest["uploads"]
    assert posted["files"][0][0] == "file:cleaned_q0.jsonl"


def test_forward_lab_unreachable_keeps(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LAB_ORIGIN_URL", "https://lab.example")
    sdir = _stage_one(tmp_path)
    monkeypatch.setattr(staging_forward, "_http_get",
                        lambda url, timeout=15: (0, "connect timeout"))
    summary = staging_forward.run()
    assert summary["forwarded"] == []
    assert sdir.exists()


def test_forward_lab_error_keeps(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LAB_ORIGIN_URL", "https://lab.example")
    sdir = _stage_one(tmp_path)
    monkeypatch.setattr(staging_forward, "_http_get",
                        lambda url, timeout=15: (200, '{"ok": true}'))
    monkeypatch.setattr(staging_forward, "_http_post_ingest",
                        lambda url, fields, files, timeout=300: (500, "boom"))
    summary = staging_forward.run()
    assert summary["kept"] == ["ses_fwd_01"]
    assert sdir.exists()


# ---------- ingest-staged（authoritative） ----------

@pytest.fixture
def ingest_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "STAGING_SECRET", "s3cret")
    return tmp_path


def _ingest(client, cid, sid, secret="s3cret", slot_payloads=None,
            repo_url="", student_id=""):
    slot_payloads = slot_payloads if slot_payloads is not None else {0: b"{}\n"}
    uploads = {}
    files = []
    for slot, content in slot_payloads.items():
        rel = f"cleaned_q{slot}.jsonl" if slot >= 0 else "cleaned.jsonl"
        uploads[str(slot)] = {
            "slot": slot,
            "staged_at": "2026-10-12T00:00:00+00:00",
            "files": [{"field": "cleaned", "name": rel, "path": rel,
                       "sha256": hashlib.sha256(content).hexdigest()}],
        }
        files.append((f"file:{rel}", (rel, content, "application/octet-stream")))
    data = {
        "campaign_id": cid,
        "session_id": sid,
        "student_id": student_id,
        "repo_url": repo_url,
        "manifest": json.dumps({"uploads": uploads}),
    }
    headers = {"X-Staging-Secret": secret} if secret else {}
    return client.post("/api/internal/ingest-staged", data=data, files=files,
                       headers=headers)


def test_ingest_new_and_idempotent(client, cid, admin_headers, ingest_env):
    sid, _ = _enroll(client, cid, "08271201")
    content = b'{"k": "user", "text": "staged"}\n'
    res = _ingest(client, cid, sid, slot_payloads={0: content},
                  repo_url="https://github.com/x/staged")
    assert res.status_code == 200, res.text
    assert res.json()["results"] == [{"slot": 0, "status": "ingested"}]

    # 与正常交卷同口径落库：文件归位 + uploaded + slots + 报名 tested
    assert (ingest_env / "uploads" / cid / sid / "cleaned_q0.jsonl").is_file()
    res = client.get(f"/api/campaigns/{cid}/exam/sessions",
                     params={"student_id": "08271201"}, headers=admin_headers)
    item = res.json()[0]
    assert item["status"] == "uploaded"
    assert item["repo_url"] == "https://github.com/x/staged"
    res = client.get(f"/api/campaigns/{cid}/applications",
                     params={"q": "08271201"}, headers=admin_headers)
    assert res.json()[0]["status"] == "tested"

    # 台账登记 source=staged
    entries = submission.load_ledger()
    digest = hashlib.sha256(content).hexdigest()
    assert any(e["session_id"] == sid and e["slot"] == 0
               and e["sha256"] == digest and e["source"] == "staged"
               for e in entries)

    # 同 hash 重发 → 幂等 ack，不报错不重复写
    res = _ingest(client, cid, sid, slot_payloads={0: content})
    assert res.status_code == 200
    assert res.json()["results"] == [{"slot": 0, "status": "deduped"}]


def test_ingest_direct_race_deduped(client, cid, ingest_env):
    """直连上传与暂存转发竞态：同 session+slot+hash 已由 direct 登记 → ack。"""
    sid, token = _enroll(client, cid, "08271202")
    content = b'{"k": "user", "text": "race"}\n'
    res = _post_upload(client, cid, sid, token, slot=0, text="race")
    assert res.status_code == 200, res.text  # direct 落库并登记台账
    res = _ingest(client, cid, sid, slot_payloads={0: content})
    assert res.status_code == 200
    assert res.json()["results"] == [{"slot": 0, "status": "deduped"}]


def test_ingest_secret_required(client, cid, ingest_env):
    sid, _ = _enroll(client, cid, "08271203")
    assert _ingest(client, cid, sid, secret="").status_code == 403
    assert _ingest(client, cid, sid, secret="wrong").status_code == 403


def test_ingest_unknown_session_404(client, cid, ingest_env):
    res = _ingest(client, cid, "nosuchsession")
    assert res.status_code == 404


def test_ingest_hash_mismatch_400(client, cid, ingest_env):
    sid, _ = _enroll(client, cid, "08271204")
    uploads = {"0": {"slot": 0, "files": [
        {"field": "cleaned", "name": "cleaned_q0.jsonl",
         "path": "cleaned_q0.jsonl", "sha256": "0" * 64}]}}
    res = client.post(
        "/api/internal/ingest-staged",
        data={"campaign_id": cid, "session_id": sid, "student_id": "",
              "repo_url": "", "manifest": json.dumps({"uploads": uploads})},
        files={"file:cleaned_q0.jsonl": ("cleaned_q0.jsonl", b"{}\n",
                                         "application/octet-stream")},
        headers={"X-Staging-Secret": "s3cret"})
    assert res.status_code == 400

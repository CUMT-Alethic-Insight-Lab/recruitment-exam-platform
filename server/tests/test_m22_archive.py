"""M22：exam-archive 即时备份（lab/authoritative）。

会话转 uploaded（直连/P2P/ingest 共用收敛点触发）后后台归档到本地 git 克隆：
- 目录 {campaign_id}/{student_id}/；清洗件转 md（逐题 cleaned_qN.md / 单槽
  cleaned.md，头部学号/轨迹/开考/交卷——与下载接口同口径）
- 有 repo_url 写 README.md；zip 附件解压成 attachment/（防 zip-slip），非 zip 原样复制
- git add/commit/push（无变更跳过）；push 失败入 data/archive_queue.jsonl，
  flush_queue 补推成功后清空

测试用 tmp_path 本地 git 仓 + 裸 remote，不触网；BACKGROUND=False 同步执行。
"""

import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import applicant_headers  # noqa: E402

from app import archive, config  # noqa: E402

CLEANED = (
    '{"k": "user", "time": 1760000000000, "text": "帮我写个函数"}\n'
    '{"k": "assistant", "time": 1760000001000, "text": "好的"}\n'
).encode()


def _app_payload(student_id):
    return {
        "student_id": student_id,
        "name": "归档考生",
        "gender": "female",
        "class_name": "计科244",
        "qq": "700700702",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "M22 归档测试",
    }


def _enroll(client, cid, student_id):
    client.post(f"/api/campaigns/{cid}/applications", json=_app_payload(student_id))
    headers = applicant_headers(client, cid, student_id)
    res = client.post(f"/api/campaigns/{cid}/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()
    return body["session_id"], body["session_token"]


def _make_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def archive_repo(tmp_path, monkeypatch):
    """本地 git 克隆 + 裸 remote（真实 push，不触网）。"""
    repo = tmp_path / "exam-archive"
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-b", "main", str(repo)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    str(bare)], check=True, capture_output=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "ARCHIVE_ENABLED", True)
    monkeypatch.setattr(config, "ARCHIVE_REPO_DIR", str(repo))
    monkeypatch.setattr(config, "ARCHIVE_GITHUB_TOKEN", "")
    monkeypatch.setattr(archive, "BACKGROUND", False)
    return repo, bare


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def test_archive_slot_layout(client, cid, archive_repo):
    repo, bare = archive_repo
    sid, token = _enroll(client, cid, "08272101")
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": sid, "slot": "0",
              "repo_url": "https://github.com/x/arch"},
        files={
            "cleaned": ("cleaned_q0.jsonl", CLEANED, "application/octet-stream"),
            "attachment": ("attachment.zip",
                           _make_zip({"notes/readme.txt": b"hello",
                                      "../evil.txt": b"x"}),
                           "application/zip"),
        },
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text

    target = repo / cid / "08272101"
    md = (target / "cleaned_q0.md").read_text(encoding="utf-8")
    assert "学号：08272101" in md
    assert "第 1 题" in md
    assert "## 👤 用户" in md
    assert "帮我写个函数" in md
    assert "https://github.com/x/arch" in (target / "README.md").read_text(
        encoding="utf-8")
    # zip 解压成 attachment/ 入库，不存 zip 本体；zip-slip 成员被拒
    assert (target / "attachment" / "notes" / "readme.txt").is_file()
    assert not (target / "attachment.zip").exists()
    assert not (repo / cid / "evil.txt").exists()

    # commit + push 到裸 remote
    assert sid in _git(repo, "log", "-1", "--format=%s")
    assert _git(repo, "ls-remote", "origin", "refs/heads/main").strip()
    # 待推队列为空
    assert archive.load_queue() == []


def test_archive_whole_upload_cleaned_md(client, cid, archive_repo):
    repo, _ = archive_repo
    sid, token = _enroll(client, cid, "08272102")
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": sid},
        files={"cleaned": ("cleaned.jsonl", CLEANED, "application/octet-stream")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text
    target = repo / cid / "08272102"
    md = (target / "cleaned.md").read_text(encoding="utf-8")
    assert "学号：08272102" in md
    assert not (target / "README.md").exists()  # 无 repo_url 不写


def test_archive_push_failure_enqueues_then_flush(client, cid, archive_repo,
                                                  monkeypatch):
    repo, bare = archive_repo
    sid, token = _enroll(client, cid, "08272103")
    real_push = archive.push

    def boom(repo_path=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(archive, "push", boom)
    res = client.post(
        f"/api/campaigns/{cid}/sessions",
        data={"session_id": sid},
        files={"cleaned": ("cleaned.jsonl", CLEANED, "application/octet-stream")},
        headers={"X-Session-Token": token},
    )
    assert res.status_code == 200, res.text  # push 失败不阻塞交卷
    queue = archive.load_queue()
    assert len(queue) == 1
    assert queue[0]["session_id"] == sid
    assert queue[0]["reason"] == "network down"
    # commit 已在本地，remote 尚未收到
    assert sid in _git(repo, "log", "-1", "--format=%s")
    assert not _git(repo, "ls-remote", "origin").strip()

    # 网络恢复 → flush_queue 补推成功并清空队列
    monkeypatch.setattr(archive, "push", real_push)
    summary = archive.flush_queue()
    assert summary["pushed"] is True
    assert summary["cleared"] == 1
    assert archive.load_queue() == []
    assert _git(repo, "ls-remote", "origin", "refs/heads/main").strip()


def test_archive_disabled_noop(client, cid, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ARCHIVE_ENABLED", False)
    assert archive.archive_session("whatever")["archived"] is False

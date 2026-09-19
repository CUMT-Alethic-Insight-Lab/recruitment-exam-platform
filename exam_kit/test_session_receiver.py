"""session_receiver（局域网会话接收服务）与 exam_server 交卷分流（upload_mode=lan）单测。

stdlib unittest，无第三方依赖；接收机在用例内以临时端口起真实 HTTP 服务。
"""

import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import exam_server as es
import session_receiver as sr


def _request(base: str, method: str, path: str, data: bytes | None = None,
             headers: dict | None = None, timeout: float = 10):
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


class _Receiver:
    """用例内启动的真实接收机（临时目录 + 随机端口）。"""

    def __init__(self, root: Path, token: str = ""):
        sr.ROOT = root
        sr.TOKEN = token
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), sr.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TestSessionReceiver(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "received"
        self.rx = _Receiver(self.root)

    def tearDown(self):
        self.rx.close()
        self._tmp.cleanup()

    def _begin(self, cid="2026autumn", sid="ses-1", **extra):
        payload = {"student_id": "08260001", "session_token": "tok-1",
                   "track": "track1", "agent": "kimi", "repo_url": "",
                   "attachment_name": ""}
        payload.update(extra)
        body = json.dumps(payload).encode("utf-8")
        return _request(self.rx.base, "POST", f"/api/sessions/{cid}/{sid}/begin",
                        data=body, headers={"Content-Type": "application/json"})

    def test_health(self):
        status, raw = _request(self.rx.base, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["ok"])

    def test_full_flow(self):
        self._begin(repo_url="https://git.example/repo")
        api = "/api/sessions/2026autumn/ses-1"
        status, _ = _request(self.rx.base, "POST", f"{api}/file/cleaned_jsonl",
                             data=b'{"a": 1}\n', headers={"X-Filename": "cleaned.jsonl"})
        self.assertEqual(status, 200)
        status, _ = _request(self.rx.base, "POST", f"{api}/file/cleaned_md",
                             data="# report".encode("utf-8"))
        self.assertEqual(status, 200)
        status, _ = _request(self.rx.base, "POST", f"{api}/file/attachment",
                             data=b"PK-zip-bytes", headers={"X-Filename": "att.zip"})
        self.assertEqual(status, 200)
        status, raw = _request(self.rx.base, "POST", f"{api}/done",
                               data=json.dumps({"repo_url": "https://git.example/repo"}).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        manifest = json.loads((self.root / "2026autumn" / "ses-1" / "manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(manifest["student_id"], "08260001")
        self.assertEqual(manifest["repo_url"], "https://git.example/repo")
        self.assertIn("cleaned_jsonl", manifest["files"])
        self.assertEqual(manifest["files"]["attachment"]["bytes"], len(b"PK-zip-bytes"))
        # 文件可取回
        status, raw = _request(self.rx.base, "GET", f"{api}/files/cleaned_jsonl")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b'{"a": 1}\n')
        status, raw = _request(self.rx.base, "GET", f"{api}/files/attachment?dl=1")
        self.assertEqual(status, 200)
        self.assertEqual(raw, b"PK-zip-bytes")
        # 首页列表含学号与会话
        status, raw = _request(self.rx.base, "GET", "/")
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        self.assertIn("08260001", html)
        self.assertIn("已收齐", html)

    def test_path_traversal_rejected(self):
        for bad in ("../evil", "a/b", "ses%2f..%2fx", "ses with space"):
            try:
                _request(self.rx.base, "POST",
                         f"/api/sessions/2026autumn/{bad}/begin", data=b"{}")
            except urllib.error.HTTPError as exc:
                self.assertIn(exc.code, (400, 404))
            except (ValueError, urllib.error.URLError, http.client.HTTPException):
                pass  # 客户端就拒了（如带空格的 URL），同样到不了服务端
        # 无论怎么绕，received 根目录外不能出现新文件
        self.assertFalse((self.root.parent / "evil").exists())

    def test_file_before_begin_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request(self.rx.base, "POST", "/api/sessions/2026autumn/s1/file/cleaned_jsonl",
                     data=b"x")
        self.assertEqual(ctx.exception.code, 404)

    def test_done_without_cleaned_rejected(self):
        self._begin(sid="ses-2")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            _request(self.rx.base, "POST", "/api/sessions/2026autumn/ses-2/done",
                     data=b"{}")
        self.assertEqual(ctx.exception.code, 400)

    def test_rebegin_allows_retry(self):
        self._begin(sid="ses-3")
        api = "/api/sessions/2026autumn/ses-3"
        _request(self.rx.base, "POST", f"{api}/file/cleaned_jsonl", data=b"old")
        _request(self.rx.base, "POST", f"{api}/done", data=b"{}")
        self._begin(sid="ses-3")  # 重传：回到接收态并允许覆盖
        status, _ = _request(self.rx.base, "POST", f"{api}/file/cleaned_jsonl", data=b"new")
        self.assertEqual(status, 200)
        _request(self.rx.base, "POST", f"{api}/done", data=b"{}")
        status, raw = _request(self.rx.base, "GET", f"{api}/files/cleaned_jsonl")
        self.assertEqual(raw, b"new")

    def test_token_gate(self):
        self.rx.close()
        self.rx = _Receiver(self.root, token="sekret")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._begin(sid="ses-4")
        self.assertEqual(ctx.exception.code, 401)
        status, _ = _request(self.rx.base, "POST", "/api/sessions/2026autumn/ses-4/begin",
                             data=json.dumps({"student_id": "08260001"}).encode("utf-8"),
                             headers={"X-Receiver-Token": "sekret"})
        self.assertEqual(status, 200)


class TestUploadSessionLan(unittest.TestCase):
    """upload_mode=lan 分流：文件进接收机、云端只收轻量标记。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.rx = _Receiver(root / "received")
        self.orig_pending = es.PENDING_DIR
        es.PENDING_DIR = root / "pending_upload"
        es.PENDING_DIR.mkdir(parents=True)
        (es.PENDING_DIR / "a").mkdir()
        (es.PENDING_DIR / "a" / "cleaned.jsonl").write_bytes(b'{"k":1}\n')
        (es.PENDING_DIR / "a" / "cleaned.md").write_text("# r", encoding="utf-8")
        (es.PENDING_DIR / "attachment_08260001.zip").write_bytes(b"zip")
        self.meta = {
            "campaign_id": "2026autumn",
            "session_id": "cloud-ses-1",
            "session_token": "tok-1",
            "student_id": "08260001",
            "track": "track1",
            "agent": "kimi",
            "cleaned_jsonl": "a/cleaned.jsonl",
            "cleaned_md": "a/cleaned.md",
            "repo_url": "https://git.example/r",
            "attachment_name": "attachment_08260001.zip",
            "uploaded": False,
        }
        self.cfg = {"upload_mode": "lan", "lan_receiver_base": self.rx.base,
                    "cloud_base": "https://cloud.example", "campaign_id": "2026autumn",
                    "http_timeout": 5}
        self.cloud_calls = []
        self.orig_cloud_request = es.cloud_request

        def fake_cloud_request(cfg, method, path, body=None, session_token=None,
                               bearer=None):
            self.cloud_calls.append((method, path, body, session_token))
            return 200, {}

        es.cloud_request = fake_cloud_request

    def tearDown(self):
        es.cloud_request = self.orig_cloud_request
        es.PENDING_DIR = self.orig_pending
        self.rx.close()
        self._tmp.cleanup()

    def test_files_to_receiver_lightweight_mark_to_cloud(self):
        es.upload_session(self.cfg, self.meta)
        manifest = json.loads(
            (sr.ROOT / "2026autumn" / "cloud-ses-1" / "manifest.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"]["cleaned_jsonl"]["bytes"], 8)
        self.assertEqual(manifest["files"]["attachment"]["bytes"], 3)
        self.assertEqual(len(self.cloud_calls), 1)
        method, path, body, token = self.cloud_calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/campaigns/2026autumn/sessions/cloud-ses-1/lan-uploaded")
        self.assertEqual(body, {"repo_url": "https://git.example/r"})
        self.assertEqual(token, "tok-1")

    def test_cloud_already_uploaded_is_idempotent(self):
        def already(cfg, method, path, body=None, session_token=None, bearer=None):
            raise es.CloudError("该会话已交卷", status=409,
                                detail={"code": "already_uploaded"})
        es.cloud_request = already
        es.upload_session(self.cfg, self.meta)  # 不抛即通过

    def test_receiver_unreachable_raises_friendly(self):
        self.cfg["lan_receiver_base"] = "http://127.0.0.1:1"
        with self.assertRaises(es.CloudError) as ctx:
            es.upload_session(self.cfg, self.meta)
        self.assertIn("无法连接", str(ctx.exception))

    def test_missing_receiver_base_config(self):
        cfg = dict(self.cfg, lan_receiver_base="")
        with self.assertRaises(es.CloudError) as ctx:
            es.upload_session(cfg, self.meta)
        self.assertIn("lan_receiver_base", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

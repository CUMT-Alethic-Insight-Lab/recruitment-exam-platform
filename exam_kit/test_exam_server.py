"""exam_server 纯函数单测（stdlib unittest，无第三方依赖）。"""

import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import cleaner  # 真实清洗模块（并行交付）；单测内会被下面的假实现覆盖
except ImportError:
    cleaner = None


def _make_fake_cleaner(with_session: bool = False) -> types.ModuleType:
    """与 cleaner.clean_export 契约一致的假实现，保证单测不依赖并行模块。"""
    mod = types.ModuleType("cleaner")

    def clean_export(zip_path, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        jl = out_dir / "cleaned.jsonl"
        md = out_dir / "cleaned.md"
        jl.write_text('{"cleaned": true}\n', encoding="utf-8")
        md.write_text("# cleaned\n", encoding="utf-8")
        return {"cleaned_jsonl": jl, "cleaned_md": md, "stats": {"events": 1}}

    mod.clean_export = clean_export
    if with_session:
        mod.clean_session = clean_export  # M11 统一入口（契约相同）
    return mod


# 注意：不要在 import 时把假 cleaner 塞进 sys.modules——同一进程里后加载的
# test_cleaner 会拿到假模块而全军覆没。假实现通过各用例 monkeypatch
# exam_server.load_cleaner 注入（见 TestFinishFlow）。

import exam_server as es  # noqa: E402


def _make_kimi_home(root: Path, sessions: list[dict]) -> Path:
    """构造假的 kimi_home：session_index.jsonl + 每个会话的 state.json。"""
    home = root / ".kimi-code"
    home.mkdir(parents=True)
    lines = []
    for s in sessions:
        sdir = home / "sessions" / "wd_test" / s["sessionId"]
        sdir.mkdir(parents=True)
        (sdir / "state.json").write_text(
            json.dumps({"id": s["sessionId"], "createdAt": s["createdAt"]}),
            encoding="utf-8")
        wire = sdir / "agents" / "main"
        wire.mkdir(parents=True)
        (wire / "wire.jsonl").write_text('{"msg":"hello"}\n', encoding="utf-8")
        lines.append(json.dumps({
            "sessionId": s["sessionId"],
            "sessionDir": str(sdir),
            "workDir": "E:/work",
        }))
    (home / "session_index.jsonl").write_text("\n".join(lines), encoding="utf-8")
    return home


def _make_opencode_db(home: Path, sessions: list[dict]) -> Path:
    """按真实 opencode.db schema（docs_opencode_db.md 勘察结论）建最小测试库，
    插入无害的假会话数据。sessions 结构：
    [{"id","directory","time_created","time_updated","slug"?,"title"?,"project_id"?,
      "messages":[{"id","time_created","data","parts":[{"id","data","time_created"?}]}]}]。
    """
    db = home / "opencode.db"
    con = sqlite3.connect(db)
    with con:
        con.executescript(
            "CREATE TABLE session (id text PRIMARY KEY, project_id text NOT NULL,"
            " parent_id text, slug text NOT NULL, directory text NOT NULL,"
            " title text NOT NULL, version text NOT NULL,"
            " time_created integer NOT NULL, time_updated integer NOT NULL);"
            "CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL,"
            " time_created integer NOT NULL, time_updated integer NOT NULL,"
            " data text NOT NULL);"
            "CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL,"
            " session_id text NOT NULL, time_created integer NOT NULL,"
            " time_updated integer NOT NULL, data text NOT NULL);")
        for s in sessions:
            con.execute(
                "INSERT INTO session (id, project_id, parent_id, slug, directory,"
                " title, version, time_created, time_updated)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (s["id"], s.get("project_id", "global"), None,
                 s.get("slug", "test-slug"), s.get("directory", "P:/proj"),
                 s.get("title", "测试会话"), "1.18.29",
                 s["time_created"], s["time_updated"]))
            for m in s.get("messages", []):
                con.execute(
                    "INSERT INTO message (id, session_id, time_created,"
                    " time_updated, data) VALUES (?,?,?,?,?)",
                    (m["id"], s["id"], m["time_created"], m["time_created"],
                     json.dumps(m["data"], ensure_ascii=False)))
                for i, p in enumerate(m.get("parts", [])):
                    ptime = p.get("time_created", m["time_created"] + i)
                    con.execute(
                        "INSERT INTO part (id, message_id, session_id,"
                        " time_created, time_updated, data) VALUES (?,?,?,?,?,?)",
                        (p["id"], m["id"], s["id"], ptime, ptime,
                         json.dumps(p["data"], ensure_ascii=False)))
    con.close()
    return db


def _parse_multipart(body: bytes, content_type: str):
    """极简 multipart/form-data 解析（仅用于单测断言 build_multipart 输出）。"""
    boundary = content_type.split("boundary=", 1)[1].encode("utf-8")
    fields, files = {}, {}
    for seg in body.split(b"--" + boundary)[1:]:
        if seg.startswith(b"--"):
            continue  # 结束标记
        seg = seg[2:] if seg.startswith(b"\r\n") else seg.lstrip(b"\r\n")
        head, _, content = seg.partition(b"\r\n\r\n")
        if content.endswith(b"\r\n"):
            content = content[:-2]
        disp = ""
        for line in head.split(b"\r\n"):
            key, _, val = line.partition(b":")
            if key.strip().lower() == b"content-disposition":
                disp = val.strip().decode("utf-8")
        name_m = re.search(r'name="([^"]*)"', disp)
        if not name_m:
            continue
        name = name_m.group(1)
        if re.search(r'filename="([^"]*)"', disp):
            files[name] = content
        else:
            fields[name] = content.decode("utf-8")
    return fields, files


class TestKimiSessionScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.sessions = [
            {"sessionId": "ses_old", "createdAt": 1000},
            {"sessionId": "ses_new", "createdAt": 3000},
            {"sessionId": "ses_mid", "createdAt": 2000},
        ]
        self.home = _make_kimi_home(root, self.sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def test_iter_reads_created_at(self):
        found = {s["sessionId"]: s["createdAt"] for s in es.iter_kimi_sessions(self.home)}
        self.assertEqual(found, {"ses_old": 1000, "ses_new": 3000, "ses_mid": 2000})

    def test_pick_candidates_filters_and_sorts(self):
        cands = es.pick_candidates(es.iter_kimi_sessions(self.home), 1500)
        self.assertEqual([c["sessionId"] for c in cands], ["ses_new", "ses_mid"])

    def test_pick_candidates_empty(self):
        self.assertEqual(es.pick_candidates(es.iter_kimi_sessions(self.home), 9999), [])

    def test_missing_index_returns_empty(self):
        self.assertEqual(es.iter_kimi_sessions(Path(self.tmp.name) / "nope"), [])


class TestExportFallback(unittest.TestCase):
    def test_export_via_zip_contains_wire(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _make_kimi_home(root, [{"sessionId": "ses_a", "createdAt": 1}])
            sdir = home / "sessions" / "wd_test" / "ses_a"
            out = root / "out.zip"
            es.export_via_zip(sdir, out)
            names = zipfile.ZipFile(out).namelist()
            self.assertIn("state.json", names)
            self.assertIn("agents/main/wire.jsonl", names)

    def test_export_session_falls_back_when_kimi_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = _make_kimi_home(root, [{"sessionId": "ses_a", "createdAt": 1}])
            session = es.iter_kimi_sessions(home)[0]
            cfg = dict(es.DEFAULT_CONFIG, kimi_bin="definitely-not-a-real-kimi-bin")
            out = root / "out.zip"
            method = es.export_session(cfg, session, out)
            self.assertEqual(method, "zip 回退")
            self.assertTrue(out.exists())


class TestHelpers(unittest.TestCase):
    def test_iso_to_ms(self):
        self.assertEqual(es.iso_to_ms("1970-01-01T00:00:01+00:00"), 1000)


class TestComputeClientHash(unittest.TestCase):
    """PBKDF2 client_hash 与云端/浏览器 WebCrypto 契约互通（固定写死期望值）。"""

    SALT = "00112233445566778899aabbccddeeff"

    def test_known_value(self):
        # 已知值：hashlib.pbkdf2_hmac("sha256", b"08260001", bytes.fromhex(SALT),
        # 100000, 32).hex() —— 与 server/app/auth.py compute_client_hash 同式
        self.assertEqual(
            es.compute_client_hash("08260001", self.SALT),
            "b75af24018deac4cfc2ca0171256f933687560106abc8412edb29cf410eb0afb")

    def test_password_is_utf8_encoded(self):
        expected = hashlib.pbkdf2_hmac(
            "sha256", "密码0826".encode("utf-8"),
            bytes.fromhex("00" * 16), 100000, 32).hex()
        self.assertEqual(es.compute_client_hash("密码0826", "00" * 16), expected)


class TestBuildMultipart(unittest.TestCase):
    def test_roundtrip_fields_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            jl = Path(tmp) / "cleaned.jsonl"
            content = '{"a": 1}\n{"b": 2}\n'.encode("utf-8")
            jl.write_bytes(content)
            body, ctype = es.build_multipart(
                {"session_id": "ses1", "repo_url": "https://github.com/x/y"},
                [("cleaned", jl)])
            self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
            fields, files = _parse_multipart(body, ctype)
            self.assertEqual(fields, {
                "session_id": "ses1",
                "repo_url": "https://github.com/x/y",
            })
            self.assertEqual(files, {"cleaned": content})
            # 结束标记必须存在
            boundary = ctype.split("boundary=", 1)[1].encode("utf-8")
            self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))


class _StubCloudHandler(BaseHTTPRequestHandler):
    """云端 POST /api/campaigns/{cid}/sessions 的行为由 server.mode 决定，并记录请求。"""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.requests.append({
            "path": self.path,
            "session_token": self.headers.get("X-Session-Token"),
            "content_type": self.headers.get("Content-Type", ""),
            "body": self.rfile.read(length),
        })
        mode = self.server.mode
        payload = {
            "ok": (200, {"ok": True}),
            "conflict": (409, {"detail": {"code": "already_uploaded", "message": "已上传"}}),
            "unauthorized": (401, {"detail": {"code": "invalid_token", "message": "凭证无效"}}),
            "error": (500, {"detail": {"code": "internal", "message": "boom"}}),
        }[mode]
        body = json.dumps(payload[1]).encode("utf-8")
        self.send_response(payload[0])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class TestUploadSession(unittest.TestCase):
    """upload_session 与云端契约：multipart 字段/头、409 幂等、401/5xx 报错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.orig_pending = es.PENDING_DIR
        es.PENDING_DIR = root / "pending_upload"
        es.PENDING_DIR.mkdir(parents=True)
        cleaned = es.PENDING_DIR / "s1" / "cleaned.jsonl"
        cleaned.parent.mkdir()
        cleaned.write_bytes(b'{"x": 1}\n')
        self.meta = {
            "session_id": "cloud-ses-1",
            "session_token": "tok-123",
            "student_id": "08260001",
            "cleaned_jsonl": "s1/cleaned.jsonl",
            "repo_url": "https://github.com/x/y",
            "attachment_name": "",
        }
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _StubCloudHandler)
        self.httpd.requests = []
        self.httpd.mode = "ok"
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        es.PENDING_DIR = self.orig_pending
        self.tmp.cleanup()

    def _cfg(self):
        return dict(es.DEFAULT_CONFIG, cloud_base=f"http://127.0.0.1:{self.port}")

    def test_ok_sends_contract_fields(self):
        es.upload_session(self._cfg(), self.meta)
        req = self.httpd.requests[0]
        self.assertEqual(req["path"], "/api/campaigns/2026autumn/sessions")
        self.assertEqual(req["session_token"], "tok-123")
        self.assertTrue(req["content_type"].startswith("multipart/form-data; boundary="))
        fields, files = _parse_multipart(req["body"], req["content_type"])
        self.assertEqual(fields, {
            "session_id": "cloud-ses-1",
            "repo_url": "https://github.com/x/y",
        })
        self.assertEqual(files["cleaned"], b'{"x": 1}\n')

    def test_409_already_uploaded_is_idempotent(self):
        self.httpd.mode = "conflict"
        es.upload_session(self._cfg(), self.meta)  # 不应抛异常

    def test_500_raises_cloud_error(self):
        self.httpd.mode = "error"
        with self.assertRaises(es.CloudError) as cm:
            es.upload_session(self._cfg(), self.meta)
        self.assertEqual(cm.exception.status, 500)

    def test_401_raises_cloud_error(self):
        self.httpd.mode = "unauthorized"
        with self.assertRaises(es.CloudError) as cm:
            es.upload_session(self._cfg(), self.meta)
        self.assertEqual(cm.exception.status, 401)

    def test_attachment_appended_when_present(self):
        att = es.PENDING_DIR / "attachment_08260001.zip"
        att.write_bytes(b"PK\x03\x04zipdata")
        self.meta["attachment_name"] = "work.zip"
        es.upload_session(self._cfg(), self.meta)
        _, files = _parse_multipart(
            self.httpd.requests[0]["body"], self.httpd.requests[0]["content_type"])
        self.assertEqual(files["attachment"], b"PK\x03\x04zipdata")


class _LocalServerTestCase(unittest.TestCase):
    """起一个真实 ExamHandler（随机端口），STATE/PENDING 指到临时目录。"""

    def install_tmp_dirs(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.orig_state = es.STATE_PATH
        self.orig_pending = es.PENDING_DIR
        es.STATE_PATH = root / "state.json"
        es.PENDING_DIR = root / "pending_upload"
        es.PENDING_DIR.mkdir(parents=True)
        self.root = root

    def restore_dirs(self):
        es.STATE_PATH = self.orig_state
        es.PENDING_DIR = self.orig_pending
        self.tmp.cleanup()

    def start_server(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), es.ExamHandler)
        self.httpd.cfg = dict(es.DEFAULT_CONFIG)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def write_state(self, **extra):
        es.STATE_PATH.write_text(
            json.dumps({"stage": "logged_in", "student_id": "08260001", **extra},
                       ensure_ascii=False),
            encoding="utf-8")

    def read_state(self) -> dict:
        return json.loads(es.STATE_PATH.read_text(encoding="utf-8"))


class TestAttachmentAPI(_LocalServerTestCase):
    def setUp(self):
        self.install_tmp_dirs()
        self.write_state()
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.restore_dirs()

    def test_put_non_zip_rejected_400(self):
        req = urllib.request.Request(
            self.url("/api/attachment"), data=b"hello",
            headers={"X-Filename": "notes.txt"}, method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()
        self.assertFalse((es.PENDING_DIR / "attachment_08260001.zip").exists())

    def test_put_then_delete_zip_roundtrip(self):
        data = b"PK\x03\x04fake-zip-bytes"
        req = urllib.request.Request(
            self.url("/api/attachment"), data=data,
            headers={"X-Filename": "my work.zip"}, method="PUT")
        with urllib.request.urlopen(req) as resp:
            out = json.loads(resp.read())
        self.assertEqual(out["attachment_name"], "my work.zip")
        dest = es.PENDING_DIR / "attachment_08260001.zip"
        self.assertEqual(dest.read_bytes(), data)
        self.assertEqual(self.read_state()["attachment_name"], "my work.zip")
        req = urllib.request.Request(self.url("/api/attachment"), method="DELETE")
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
        self.assertFalse(dest.exists())
        self.assertIsNone(self.read_state().get("attachment_name"))

    def test_put_requires_login(self):
        es.STATE_PATH.write_text(json.dumps({"stage": ""}), encoding="utf-8")
        req = urllib.request.Request(
            self.url("/api/attachment"), data=b"PK",
            headers={"X-Filename": "a.zip"}, method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()


_AUTH_SALT = "00112233445566778899aabbccddeeff"
_AUTH_TOKEN = "tok-auth-1"

_POOL_QUESTIONS = [
    {"id": "q-1", "domain": "网络", "track": "track1", "title": "T1", "body": "B1"},
    {"id": "q-2", "domain": "网络", "track": "track1", "title": "T2", "body": "B2"},
    {"id": "q-3", "domain": "系统", "track": "track2", "title": "T3", "body": "B3"},
]


class _StubAuthCloudHandler(BaseHTTPRequestHandler):
    """打桩云端账号体系（salt/login/auth/me/questions/enroll/reroll），行为由 server.mode 决定并记录请求。"""

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.server.requests.append({
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "session_token": self.headers.get("X-Session-Token"),
            "body": self.rfile.read(length),
        })

    def do_GET(self):
        self._record()
        if self.path.startswith("/api/auth/salt"):
            if self.server.mode == "no_application":
                self._json(404, {"detail": "未找到报名记录"})
            else:
                self._json(200, {"salt": _AUTH_SALT})
        elif self.path.endswith("/auth/me"):
            self._json(200, {"student_id": "08260001", "name": "张三",
                             "status": "approved",
                             "exam_mode": self.server.exam_mode,
                             "has_session": False, "track": None})
        elif self.path.endswith("/exam/questions"):
            if self.server.mode == "expired":
                self._json(401, {"detail": "请重新登录"})
            else:
                self._json(200, _POOL_QUESTIONS)
        else:
            self._json(404, {"detail": "not found"})

    def do_POST(self):
        self._record()
        mode = self.server.mode
        expired = mode == "expired" or \
            self.headers.get("Authorization") != f"Bearer {_AUTH_TOKEN}"
        if self.path.endswith("/auth/login"):
            if mode == "unauthorized":
                self._json(401, {"detail": "学号或密码错误"})
            else:
                self._json(200, {"token": _AUTH_TOKEN, "student_id": "08260001",
                                 "name": "张三", "pw_changed": False})
        elif self.path.endswith("/exam/enroll"):
            if expired:
                self._json(401, {"detail": "请重新登录"})
            elif mode == "already_enrolled":
                self._json(409, {"detail": {"code": "already_enrolled",
                                            "message": "已存在未交卷的考试会话",
                                            "session_id": "cloud-ses-9",
                                            "session_token": "tok-9",
                                            "machine_id": "m-9",
                                            "track": "track1",
                                            "questions": [],
                                            "rerolls_left": 2}})
            else:
                self._json(200, {"session_id": "cloud-ses-1", "session_token": "tok-1",
                                 "machine_id": "m-1", "track": "track1",
                                 "questions": [], "track3_guide_text": "",
                                 "rerolls_left": 2})
        elif self.path.endswith("/exam/reroll"):
            if expired or not self.headers.get("X-Session-Token"):
                self._json(401, {"detail": "请重新登录"})
            else:
                self._json(200, {"session_id": "cloud-ses-1", "track": "track1",
                                 "questions": [{"domain": "网络", "title": "T",
                                                "body": "B"}],
                                 "rerolls_left": 1})
        else:
            self._json(404, {"detail": "not found"})

    def log_message(self, fmt, *args):
        pass


class TestAuthFlowBase(_LocalServerTestCase):
    """本地服务 + 打桩云端的登录/开考/换题流程基类。"""

    def setUp(self):
        self.install_tmp_dirs()
        self.install_cloud()
        self.start_server()
        self.httpd.cfg["cloud_base"] = f"http://127.0.0.1:{self.cloud_port}"

    def tearDown(self):
        self.stop_server()
        self.stop_cloud()
        self.restore_dirs()

    def install_cloud(self):
        self.cloud = ThreadingHTTPServer(("127.0.0.1", 0), _StubAuthCloudHandler)
        self.cloud.requests = []
        self.cloud.mode = "ok"
        self.cloud.exam_mode = "standard"
        self.cloud_port = self.cloud.server_address[1]
        threading.Thread(target=self.cloud.serve_forever, daemon=True).start()

    def stop_cloud(self):
        self.cloud.shutdown()
        self.cloud.server_close()

    def post_json(self, path: str, body: dict):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.url(path), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def get_json(self, path: str):
        with urllib.request.urlopen(self.url(path)) as resp:
            return json.loads(resp.read())

    def cloud_enroll_requests(self) -> list[dict]:
        return [r for r in self.cloud.requests
                if r["path"].endswith("/exam/enroll")]


class TestLoginFlow(TestAuthFlowBase):
    """本地 /api/login → 云端 salt + login 的打桩流程。"""

    def test_login_success_stores_auth_token(self):
        out = self.post_json("/api/login",
                             {"student_id": "08260001", "password": "08260001"})
        self.assertEqual(out["stage"], "logged_in")
        self.assertEqual(out["student_id"], "08260001")
        self.assertEqual(out["name"], "张三")
        self.assertFalse(out["pw_changed"])
        # 云端调用契约：GET salt 带.cid/学号，POST login 带 pbkdf2 client_hash
        salt_req, login_req = self.cloud.requests[0], self.cloud.requests[1]
        self.assertEqual(salt_req["path"],
                         "/api/auth/salt?cid=2026autumn&student_id=08260001")
        self.assertEqual(login_req["path"], "/api/campaigns/2026autumn/auth/login")
        self.assertEqual(json.loads(login_req["body"]), {
            "student_id": "08260001",
            "client_hash": es.compute_client_hash("08260001", _AUTH_SALT),
        })
        state = self.read_state()
        self.assertEqual(state["auth_token"], _AUTH_TOKEN)
        self.assertEqual(state["name"], "张三")
        self.assertFalse(state["pw_changed"])
        # 登录后调 auth/me 分流：带 Bearer，state 存 exam_mode
        me_req = self.cloud.requests[2]
        self.assertEqual(me_req["method"], "GET")
        self.assertEqual(me_req["path"], "/api/campaigns/2026autumn/auth/me")
        self.assertEqual(me_req["authorization"], f"Bearer {_AUTH_TOKEN}")
        self.assertEqual(state["exam_mode"], "standard")
        self.assertIn("exam_mode", out)

    def test_login_unknown_student_maps_404(self):
        self.cloud.mode = "no_application"
        req = urllib.request.Request(
            self.url("/api/login"),
            data=json.dumps({"student_id": "08269999", "password": "x"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 404)
        self.assertIn("未找到该学号的报名记录",
                      json.loads(cm.exception.read())["error"])
        cm.exception.close()
        self.assertFalse(es.STATE_PATH.exists())

    def test_login_wrong_password_maps_401(self):
        self.cloud.mode = "unauthorized"
        req = urllib.request.Request(
            self.url("/api/login"),
            data=json.dumps({"student_id": "08260001", "password": "wrong"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)
        self.assertIn("学号或密码错误", json.loads(cm.exception.read())["error"])
        cm.exception.close()
        self.assertFalse(es.STATE_PATH.exists())

    def test_login_requires_password(self):
        req = urllib.request.Request(
            self.url("/api/login"),
            data=json.dumps({"student_id": "08260001"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()
        self.assertEqual(self.cloud.requests, [])


class TestEnrollRerollAuth(TestAuthFlowBase):
    """enroll/reroll 必须带考生 Bearer 会话；云端 401 时本地清登录态。"""

    def write_logged_in(self):
        self.write_state(stage="logged_in", name="张三", pw_changed=True,
                         auth_token=_AUTH_TOKEN)

    def test_enroll_sends_bearer_and_track_only(self):
        self.write_logged_in()
        out = self.post_json("/api/enroll", {"track": "track1"})
        self.assertEqual(out["stage"], "enrolled")
        self.assertEqual(out["session_id"], "cloud-ses-1")
        self.assertEqual(out["track_name"], "研发型（理论/概念导向）")
        req = self.cloud.requests[-1]
        self.assertEqual(req["path"], "/api/campaigns/2026autumn/exam/enroll")
        self.assertEqual(req["authorization"], f"Bearer {_AUTH_TOKEN}")
        self.assertEqual(json.loads(req["body"]), {"track": "track1"})
        state = self.read_state()
        self.assertEqual(state["session_token"], "tok-1")

    def test_enroll_without_login_rejected(self):
        es.STATE_PATH.write_text(json.dumps({"stage": ""}), encoding="utf-8")
        req = urllib.request.Request(
            self.url("/api/enroll"), data=json.dumps({"track": "track1"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()
        self.assertEqual(self.cloud.requests, [])

    def test_enroll_cloud_401_clears_auth(self):
        self.write_logged_in()
        self.cloud.mode = "expired"
        req = urllib.request.Request(
            self.url("/api/enroll"), data=json.dumps({"track": "track1"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)
        self.assertIn("重新登录", json.loads(cm.exception.read())["error"])
        cm.exception.close()
        state = self.read_state()
        self.assertNotIn("auth_token", state)
        self.assertEqual(state["stage"], "")

    def test_enroll_already_enrolled_409_continues(self):
        self.write_logged_in()
        self.cloud.mode = "already_enrolled"
        out = self.post_json("/api/enroll", {"track": "track1"})
        self.assertEqual(out["stage"], "enrolled")
        self.assertEqual(out["session_id"], "cloud-ses-9")
        self.assertEqual(self.read_state()["session_token"], "tok-9")

    def test_reroll_sends_bearer_and_session_token(self):
        self.write_state(stage="enrolled", session_id="cloud-ses-1",
                         session_token="tok-1", track="track1",
                         auth_token=_AUTH_TOKEN)
        out = self.post_json("/api/reroll", {})
        self.assertEqual(out["rerolls_left"], 1)
        self.assertEqual(out["questions"][0]["title"], "T")
        req = self.cloud.requests[-1]
        self.assertEqual(req["path"], "/api/campaigns/2026autumn/exam/reroll")
        self.assertEqual(req["authorization"], f"Bearer {_AUTH_TOKEN}")
        self.assertEqual(req["session_token"], "tok-1")
        self.assertEqual(json.loads(req["body"]), {"session_id": "cloud-ses-1"})

    def test_reroll_cloud_401_clears_auth(self):
        self.write_state(stage="enrolled", session_id="cloud-ses-1",
                         session_token="tok-1", track="track1",
                         auth_token=_AUTH_TOKEN)
        self.cloud.mode = "expired"
        req = urllib.request.Request(
            self.url("/api/reroll"), data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        state = self.read_state()
        self.assertNotIn("auth_token", state)
        self.assertEqual(state["stage"], "")


class TestExamModeFlow(TestAuthFlowBase):
    """exam_mode 分流：auth/me 拉取、自选题模式 enroll 带 question_ids、题库代理。"""

    def login(self, exam_mode="standard"):
        self.cloud.exam_mode = exam_mode
        return self.post_json("/api/login",
                              {"student_id": "08260001", "password": "08260001"})

    def test_login_own_project_stores_mode_and_page_notice(self):
        out = self.login("own_project")
        self.assertEqual(out["exam_mode"], "own_project")
        state = self.read_state()
        self.assertEqual(state["exam_mode"], "own_project")
        # 页面含个人项目轨提示文案，且提示文案与常量一致
        self.assertIn(es.OWN_PROJECT_NOTICE, es.PAGE)
        self.assertIn("无需使用本机考试", es.OWN_PROJECT_NOTICE)

    def test_login_free_creation_stores_mode_and_repo_input(self):
        out = self.login("free_creation")
        self.assertEqual(out["exam_mode"], "free_creation")
        # 交卷上传界面必须有 GitHub 链接输入框（随 cleaned 一起作为 repo_url 提交）
        self.assertIn('id="repo-url"', es.PAGE)

    def test_login_rejects_unknown_exam_mode_as_standard(self):
        self.cloud.exam_mode = "sneaky_mode"
        out = self.post_json("/api/login",
                             {"student_id": "08260001", "password": "08260001"})
        self.assertEqual(out["exam_mode"], "standard")

    def test_exam_questions_requires_login(self):
        es.STATE_PATH.write_text(json.dumps({"stage": ""}), encoding="utf-8")
        req = urllib.request.Request(self.url("/api/exam_questions"), method="GET")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()
        self.assertEqual(self.cloud.requests, [])

    def test_exam_questions_proxies_bearer_and_clean_payload(self):
        self.write_state(stage="logged_in", name="张三", pw_changed=True,
                         auth_token=_AUTH_TOKEN)
        questions = self.get_json("/api/exam_questions")
        req = self.cloud.requests[-1]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/api/campaigns/2026autumn/exam/questions")
        self.assertEqual(req["authorization"], f"Bearer {_AUTH_TOKEN}")
        self.assertEqual([q["id"] for q in questions], ["q-1", "q-2", "q-3"])
        for q in questions:  # 题库下发绝不带 meta/评审字段
            self.assertEqual(set(q), {"id", "domain", "track", "title", "body"})

    def test_exam_questions_cloud_401_clears_auth(self):
        self.write_state(stage="logged_in", name="张三", pw_changed=True,
                         auth_token=_AUTH_TOKEN)
        self.cloud.mode = "expired"
        req = urllib.request.Request(self.url("/api/exam_questions"), method="GET")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)
        cm.exception.close()
        state = self.read_state()
        self.assertNotIn("auth_token", state)
        self.assertEqual(state["stage"], "")

    def write_logged_in(self):
        self.write_state(stage="logged_in", name="张三", pw_changed=True,
                         auth_token=_AUTH_TOKEN)

    def test_enroll_with_question_ids_sends_body_and_marks_self_selected(self):
        self.write_logged_in()
        out = self.post_json("/api/enroll",
                             {"track": "track1", "question_ids": ["q-1", "q-2"]})
        self.assertEqual(out["stage"], "enrolled")
        req = self.cloud_enroll_requests()[-1]
        self.assertEqual(json.loads(req["body"]),
                         {"track": "track1", "question_ids": ["q-1", "q-2"]})
        state = self.read_state()
        self.assertTrue(state["self_selected"])

    def test_enroll_random_does_not_send_question_ids(self):
        self.write_logged_in()
        out = self.post_json("/api/enroll", {"track": "track2"})
        req = self.cloud_enroll_requests()[-1]
        self.assertEqual(json.loads(req["body"]), {"track": "track2"})
        self.assertNotIn("self_selected", out)
        self.assertNotIn("self_selected", self.read_state())

    def test_enroll_question_ids_local_validation(self):
        self.write_logged_in()
        for bad in ([], ["q-1"] * 4, "q-1", ["q-1", 2], ["", " "]):
            req = urllib.request.Request(
                self.url("/api/enroll"),
                data=json.dumps({"track": "track1", "question_ids": bad}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 400)
            cm.exception.close()
        self.assertEqual(self.cloud_enroll_requests(), [])


class TestFinishFlow(_LocalServerTestCase):
    """交卷端到端（导出/定位均打桩）：成功→uploaded；失败→finished+upload_pending。"""

    def setUp(self):
        self.install_tmp_dirs()
        self.orig = {name: getattr(es, name) for name in
                     ("upload_session", "export_session", "iter_kimi_sessions",
                      "kimi_home", "load_cleaner")}
        es.load_cleaner = lambda: _make_fake_cleaner()
        es.export_session = lambda cfg, session, out_path: "测试导出"
        es.iter_kimi_sessions = lambda home: [
            {"sessionId": "kimi-1", "sessionDir": "D:/s", "createdAt": 1}]
        es.kimi_home = lambda cfg: Path("D:/nonexistent")
        self.write_state(
            stage="enrolled", session_id="cloud-ses-1", session_token="tok-1",
            track="track1", opened_at="1970-01-01T00:00:00+00:00")
        self.start_server()

    def tearDown(self):
        self.stop_server()
        for name, val in self.orig.items():
            setattr(es, name, val)
        self.restore_dirs()

    def _post_finish(self, body: dict | None = None):
        data = json.dumps(body if body is not None else {}).encode("utf-8")
        req = urllib.request.Request(
            self.url("/api/finish"), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_finish_success_sets_uploaded(self):
        calls = []

        def fake_upload(cfg, meta):
            calls.append(meta)

        es.upload_session = fake_upload
        state = self._post_finish({"repo_url": "https://github.com/x/y"})
        self.assertEqual(state["stage"], "uploaded")
        self.assertTrue(state["uploaded"])
        self.assertEqual(state["repo_url"], "https://github.com/x/y")
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["uploaded"])
        self.assertEqual(meta["cleaned_jsonl"], "08260001_cloud-ses-1/cleaned.jsonl")
        self.assertTrue((es.PENDING_DIR / meta["cleaned_jsonl"]).exists())
        self.assertEqual(calls[0]["repo_url"], "https://github.com/x/y")

    def test_finish_upload_failure_keeps_pending(self):
        def failing_upload(cfg, meta):
            raise es.CloudError("无法连接云端", status=0)

        es.upload_session = failing_upload
        state = self._post_finish()
        self.assertEqual(state["stage"], "finished")
        self.assertTrue(state["upload_pending"])
        self.assertFalse(state["uploaded"])
        self.assertIn("upload_error", state)
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["uploaded"])
        # 清洗产物仍然留存本地
        self.assertTrue((es.PENDING_DIR / meta["cleaned_jsonl"]).exists())

    def test_finish_without_repo_url(self):
        es.upload_session = lambda cfg, meta: None
        state = self._post_finish()
        self.assertEqual(state["repo_url"], "")
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["repo_url"], "")

    def test_finish_rejects_bad_repo_url(self):
        calls = []
        es.upload_session = lambda cfg, meta: calls.append(meta)
        data = json.dumps({"repo_url": "github.com/x/y"}).encode("utf-8")
        req = urllib.request.Request(
            self.url("/api/finish"), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()
        # stage 不变，无任何交卷副作用
        self.assertEqual(self.read_state()["stage"], "enrolled")
        self.assertEqual(calls, [])
        self.assertFalse((es.PENDING_DIR / "08260001_cloud-ses-1.meta.json").exists())

    def test_finish_accepts_http_repo_url(self):
        es.upload_session = lambda cfg, meta: None
        state = self._post_finish({"repo_url": "http://github.com/x/y"})
        self.assertEqual(state["stage"], "uploaded")
        self.assertEqual(state["repo_url"], "http://github.com/x/y")


class TestRetryUpload(_LocalServerTestCase):
    def setUp(self):
        self.install_tmp_dirs()
        self.start_server()
        self.orig_upload = es.upload_session

    def tearDown(self):
        es.upload_session = self.orig_upload
        self.stop_server()
        self.restore_dirs()

    def _post(self, path):
        req = urllib.request.Request(self.url(path), data=b"{}",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_retry_success(self):
        self.write_state(stage="finished", pending_base="08260001_s1",
                         uploaded=False, upload_pending=True)
        es.PENDING_DIR.joinpath("08260001_s1.meta.json").write_text(
            json.dumps({"session_id": "s1", "session_token": "t",
                        "student_id": "08260001", "cleaned_jsonl": "s1/cleaned.jsonl",
                        "uploaded": False}),
            encoding="utf-8")
        es.PENDING_DIR.joinpath("s1").mkdir()
        es.PENDING_DIR.joinpath("s1", "cleaned.jsonl").write_bytes(b"{}")
        calls = []
        es.upload_session = lambda cfg, meta: calls.append(meta)
        state = self._post("/api/retry_upload")
        self.assertEqual(state["stage"], "uploaded")
        self.assertTrue(state["uploaded"])
        self.assertNotIn("upload_pending", state)
        meta = json.loads(
            es.PENDING_DIR.joinpath("08260001_s1.meta.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["uploaded"])
        self.assertEqual(len(calls), 1)

    def test_retry_failure_reports_pending(self):
        self.write_state(stage="finished", pending_base="08260001_s1",
                         uploaded=False, upload_pending=True)
        es.PENDING_DIR.joinpath("08260001_s1.meta.json").write_text(
            json.dumps({"uploaded": False}), encoding="utf-8")

        def failing(cfg, meta):
            raise es.CloudError("无法连接云端", status=0)

        es.upload_session = failing
        state = self._post("/api/retry_upload")
        self.assertEqual(state["stage"], "finished")
        self.assertTrue(state["upload_pending"])

    def test_retry_without_pending_base_400(self):
        self.write_state(stage="finished")
        req = urllib.request.Request(self.url("/api/retry_upload"), data=b"{}",
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()


class TestRetryPendingWorker(unittest.TestCase):
    def test_worker_uploads_only_unuploaded_and_marks_meta(self):
        tmp = tempfile.TemporaryDirectory()
        orig_pending = es.PENDING_DIR
        es.PENDING_DIR = Path(tmp.name) / "pending"
        es.PENDING_DIR.mkdir(parents=True)
        orig_upload = es.upload_session
        orig_sleep = es.time.sleep
        es.time.sleep = lambda s: None
        try:
            cleaned = es.PENDING_DIR / "a" / "cleaned.jsonl"
            cleaned.parent.mkdir()
            cleaned.write_bytes(b"{}")
            es.PENDING_DIR.joinpath("a.meta.json").write_text(
                json.dumps({"uploaded": False, "cleaned_jsonl": "a/cleaned.jsonl",
                            "session_id": "s", "session_token": "t",
                            "student_id": "08260001"}),
                encoding="utf-8")
            es.PENDING_DIR.joinpath("done.meta.json").write_text(
                json.dumps({"uploaded": True}), encoding="utf-8")
            calls = []
            es.upload_session = lambda cfg, meta: calls.append(meta["session_id"])
            es.retry_pending_uploads(cfg=dict(es.DEFAULT_CONFIG))
            self.assertEqual(calls, ["s"])
            meta = json.loads(
                es.PENDING_DIR.joinpath("a.meta.json").read_text(encoding="utf-8"))
            self.assertTrue(meta["uploaded"])
        finally:
            es.upload_session = orig_upload
            es.time.sleep = orig_sleep
            es.PENDING_DIR = orig_pending
            tmp.cleanup()


class TestAgentConfig(unittest.TestCase):
    """M11 config 扩展：agent 选择与 web 启动指令配置。"""

    def test_defaults(self):
        cfg = es.DEFAULT_CONFIG
        self.assertEqual(cfg["agent"], "kimi")
        self.assertEqual(cfg["kimi_web_cmd"], "kimi web")
        # R1 实测：opencode web 默认随机端口，必须显式 --port 固定（8898）
        self.assertEqual(cfg["opencode_web_cmd"], "opencode web --port 8898")

    def test_config_json_ships_fixed_port(self):
        """仓库自带的 config.json 同样固定 opencode web 端口（8898）。"""
        self.assertTrue(es.CONFIG_PATH.exists(), "仓库缺少 config.json")
        shipped = json.loads(es.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(shipped.get("opencode_web_cmd"), "opencode web --port 8898")

    def test_load_config_normalizes_agent(self):
        with tempfile.TemporaryDirectory() as d:
            orig = es.CONFIG_PATH
            try:
                path = Path(d) / "config.json"
                es.CONFIG_PATH = path
                path.write_text(json.dumps({"agent": "opencode"}),
                                encoding="utf-8")
                self.assertEqual(es.load_config()["agent"], "opencode")
                path.write_text(json.dumps({"agent": "claude"}),
                                encoding="utf-8")
                self.assertEqual(es.load_config()["agent"], "kimi")
            finally:
                es.CONFIG_PATH = orig

    def test_agent_web_cmd_reads_config(self):
        cfg = dict(es.DEFAULT_CONFIG)
        self.assertEqual(es.agent_web_cmd(cfg), "kimi web")
        cfg["agent"] = "opencode"
        self.assertEqual(es.agent_web_cmd(cfg), "opencode web --port 8898")
        cfg["opencode_web_cmd"] = "opencode serve"
        self.assertEqual(es.agent_web_cmd(cfg), "opencode serve")

    def test_opencode_home_and_workdir_defaults(self):
        cfg = dict(es.DEFAULT_CONFIG)
        self.assertEqual(es.opencode_home(cfg),
                         Path.home() / ".local" / "share" / "opencode")
        cfg["opencode_home"] = "D:/oc"
        self.assertEqual(es.opencode_home(cfg), Path("D:/oc"))
        self.assertEqual(es.agent_workdir(cfg), es.BASE_DIR)
        cfg["agent_workdir"] = "D:/work"
        self.assertEqual(es.agent_workdir(cfg), Path("D:/work"))


class TestOpenCodeDiscovery(unittest.TestCase):
    """opencode 会话发现：导出单文件 + 存储分立文件两种布局。"""

    T0 = 1788362472414

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        # 布局 A：存储分立文件（session/message/part 各自一个 json）
        sdir = self.home / "storage" / "session"
        sdir.mkdir(parents=True)
        self._write(sdir / "ses_a.json", {
            "id": "ses_a", "directory": "P:\\proj",
            "time": {"created": self.T0, "updated": self.T0 + 500},
        })
        mdir = self.home / "storage" / "message" / "ses_a"
        mdir.mkdir(parents=True)
        self._write(mdir / "msg_1.json", {
            "id": "msg_1", "sessionID": "ses_a", "role": "user",
            "time": {"created": self.T0 + 10},
        })
        self._write(mdir / "msg_2.json", {
            "id": "msg_2", "sessionID": "ses_a", "role": "assistant",
            "time": {"created": self.T0 + 20},
        })
        pdir = self.home / "storage" / "part" / "msg_2"
        pdir.mkdir(parents=True)
        self._write(pdir / "prt_1.json", {
            "id": "prt_1", "messageID": "msg_2", "type": "text",
            "text": "正文",
        })
        # 布局 B：导出单文件（真实样例格式，更新时间更晚）
        edir = self.home / "exports"
        edir.mkdir(parents=True)
        self._write(edir / "ses_b.json", {
            "info": {"id": "ses_b", "directory": "C:\\elsewhere",
                     "time": {"created": self.T0 + 1000,
                              "updated": self.T0 + 2000}},
            "messages": [
                {"info": {"id": "msg_9", "sessionID": "ses_b",
                          "role": "user", "time": {"created": self.T0 + 1010}},
                 "parts": [{"id": "prt_9", "messageID": "msg_9",
                            "type": "text", "text": "你好"}]},
            ],
        })
        # 非会话 json（应忽略）
        self._write(self.home / "config.json", {"foo": 1})

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _write(path: Path, obj):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def test_iter_finds_both_layouts_newest_first(self):
        sessions = es.iter_opencode_sessions(self.home)
        self.assertEqual([s["session_id"] for s in sessions],
                         ["ses_b", "ses_a"])
        by_id = {s["session_id"]: s for s in sessions}
        self.assertEqual(by_id["ses_b"]["source"], str(self.home / "exports" / "ses_b.json"))
        self.assertEqual(by_id["ses_a"]["source"], "storage")

    def test_storage_doc_assembled_with_parts(self):
        sessions = es.iter_opencode_sessions(self.home)
        doc = next(s for s in sessions if s["session_id"] == "ses_a")["doc"]
        self.assertEqual(doc["info"]["id"], "ses_a")
        self.assertEqual([m["info"]["id"] for m in doc["messages"]],
                         ["msg_1", "msg_2"])  # 按 created 排序
        self.assertEqual(doc["messages"][1]["parts"][0]["text"], "正文")

    def test_cleaner_accepts_assembled_doc(self):
        import cleaner as cleaner_mod
        sessions = es.iter_opencode_sessions(self.home)
        doc = next(s for s in sessions if s["session_id"] == "ses_b")["doc"]
        self.assertTrue(cleaner_mod.looks_like_opencode(doc))
        records, info = cleaner_mod.clean_opencode_records(doc)
        self.assertEqual([r["k"] for r in records], ["user"])
        self.assertEqual(records[0]["text"], "你好")

    def test_pick_candidates_prefers_workdir(self):
        sessions = es.iter_opencode_sessions(self.home)
        since = self.T0 + 501  # 只有 ses_b（updated 更晚）在时间上入选
        cands = es.pick_opencode_candidates(sessions, since)
        self.assertEqual([c["session_id"] for c in cands], ["ses_b"])
        # 工作目录匹配优先：两个都开考后更新时，选目录匹配的 ses_a
        sessions2 = [dict(s, updated=self.T0 + 9000) for s in sessions]
        cands = es.pick_opencode_candidates(sessions2, since,
                                            workdir=Path("P:\\proj"))
        self.assertEqual([c["session_id"] for c in cands], ["ses_a"])
        # 早于开考时间的会话不入选
        self.assertEqual(es.pick_opencode_candidates(sessions, self.T0 + 9999), [])


class TestFinishFlowOpenCode(_LocalServerTestCase):
    """opencode 交卷端到端（发现/上传打桩）：clean_session 路径 + 手动兜底。"""

    DOC = {
        "info": {"id": "ses_oc_1", "directory": "P:\\proj",
                 "time": {"created": 1000, "updated": 2000}},
        "messages": [
            {"info": {"id": "msg_1", "sessionID": "ses_oc_1", "role": "user",
                      "time": {"created": 1010}},
             "parts": [{"id": "prt_1", "messageID": "msg_1",
                        "type": "text", "text": "作答内容"}]},
        ],
    }

    def setUp(self):
        self.install_tmp_dirs()
        self.orig = {name: getattr(es, name) for name in
                     ("upload_session", "iter_opencode_sessions",
                      "opencode_home", "load_cleaner", "agent_workdir")}
        es.load_cleaner = lambda: _make_fake_cleaner(with_session=True)
        es.upload_session = lambda cfg, meta: None
        es.iter_opencode_sessions = lambda home: [{
            "session_id": "ses_oc_1", "created": 1000, "updated": 2000,
            "directory": "P:\\proj", "source": "x.json", "doc": self.DOC,
        }]
        es.opencode_home = lambda cfg: Path("D:/nonexistent-oc")
        self.write_state(
            stage="enrolled", session_id="cloud-ses-1", session_token="tok-1",
            track="track1", opened_at="1970-01-01T00:00:00+00:00",
            agent="opencode")
        self.start_server()
        self.httpd.cfg = dict(self.httpd.cfg, agent="opencode")

    def tearDown(self):
        self.stop_server()
        for name, val in self.orig.items():
            setattr(es, name, val)
        self.restore_dirs()

    def _post(self, path, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.url(path), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_finish_opencode_cleans_and_uploads(self):
        state = self._post("/api/finish", {})
        self.assertEqual(state["stage"], "uploaded")
        self.assertEqual(state["agent_session_id"], "ses_oc_1")
        self.assertEqual(state["export_method"], "opencode 会话文件")
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(meta["agent"], "opencode")
        self.assertEqual(meta["agent_session_id"], "ses_oc_1")
        self.assertEqual(meta["export_file"], "08260001_cloud-ses-1.json")
        # 原始会话文件本地留存（不上传，仅清洗结果上传）
        self.assertTrue((es.PENDING_DIR / meta["export_file"]).exists())
        self.assertTrue((es.PENDING_DIR / meta["cleaned_jsonl"]).exists())

    def test_finish_no_session_404_with_manual_hint(self):
        es.iter_opencode_sessions = lambda home: []
        data = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(
            self.url("/api/finish"), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 404)
        payload = json.loads(cm.exception.read())
        cm.exception.close()
        self.assertEqual(payload["hint"], "manual")
        self.assertIn("没找到你的作答记录", payload["error"])
        self.assertNotIn("Traceback", payload["error"])

    def test_finish_manual_accepts_session_json(self):
        state = self._post("/api/finish_manual", self.DOC)
        self.assertEqual(state["stage"], "uploaded")
        self.assertEqual(state["agent_session_id"], "ses_oc_1")
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(meta["export_method"], "手动选择的会话文件")

    def test_finish_manual_rejects_non_session(self):
        req = urllib.request.Request(
            self.url("/api/finish_manual"),
            data=json.dumps({"hello": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        self.assertIn("不是 opencode 会话", json.loads(cm.exception.read())["error"])
        cm.exception.close()

    def test_finish_manual_garbage_400(self):
        req = urllib.request.Request(
            self.url("/api/finish_manual"), data=b"not json",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()


class TestOpenCodeDbDiscovery(unittest.TestCase):
    """opencode.db（v1.18.29+ SQLite 存储）发现与拼装：真实 schema 的最小 fixture 库。"""

    T0 = 1788362472414

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = _make_opencode_db(self.home, [
            {   # 更早的会话（时间过滤用）
                "id": "ses_old", "directory": "P:/proj",
                "time_created": self.T0, "time_updated": self.T0 + 100,
                "messages": [{
                    "id": "msg_o1", "time_created": self.T0 + 10,
                    "data": {"role": "user", "time": {"created": self.T0 + 10}},
                    "parts": [{"id": "prt_o1",
                               "data": {"type": "text", "text": "旧会话"}}],
                }],
            },
            {   # 最新会话：user + assistant(reasoning/tool/text)
                "id": "ses_new", "directory": "P:/proj",
                "time_created": self.T0 + 1000, "time_updated": self.T0 + 9000,
                "slug": "tidy-eagle", "title": "答题",
                "messages": [
                    {"id": "msg_n1", "time_created": self.T0 + 1010,
                     "data": {"role": "user", "time": {"created": self.T0 + 1010}},
                     "parts": [{"id": "prt_n1",
                                "data": {"type": "text", "text": "第一问"}}]},
                    {"id": "msg_n2", "time_created": self.T0 + 1020,
                     "data": {"role": "assistant",
                              "time": {"created": self.T0 + 1020,
                                       "completed": self.T0 + 1080}},
                     "parts": [
                         {"id": "prt_n2",
                          "data": {"type": "reasoning", "text": "思考链"}},
                         {"id": "prt_n3",
                          "data": {"type": "tool", "tool": "bash",
                                   "state": {"status": "completed",
                                             "input": {"command": "dir"},
                                             "output": "a b c"}}},
                         {"id": "prt_n4",
                          "data": {"type": "text", "text": "回答正文"}},
                     ]},
                ],
            },
            {   # 没有消息的会话壳（应被排除）
                "id": "ses_empty", "directory": "C:/elsewhere",
                "time_created": self.T0 + 5000, "time_updated": self.T0 + 9500,
            },
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_sessions_newest_first_and_excludes_empty(self):
        sessions = es.opencode_db_sessions(self.home)
        self.assertEqual([s["session_id"] for s in sessions],
                         ["ses_new", "ses_old"])
        newest = sessions[0]
        self.assertEqual(newest["source"], "db")
        self.assertIsNone(newest["doc"])  # 列表阶段不读消息，内容按需取
        self.assertEqual(newest["directory"], "P:/proj")
        self.assertEqual(newest["updated"], self.T0 + 9000)

    def test_missing_db_returns_empty(self):
        self.assertEqual(
            es.opencode_db_sessions(Path(self.tmp.name) / "nope"), [])

    def test_corrupt_db_returns_empty(self):
        (self.home / "opencode.db").write_bytes(b"not a sqlite file" * 10)
        self.assertEqual(es.opencode_db_sessions(self.home), [])

    def test_locked_db_falls_back_to_copy(self):
        """mode=ro 打不开（模拟 WAL 锁）时拷 db+wal+shm 副本再读。"""
        real_connect = es.sqlite3.connect
        calls = {"n": 0}

        def flaky_connect(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise es.sqlite3.OperationalError("unable to open database file")
            return real_connect(*args, **kwargs)

        es.sqlite3.connect = flaky_connect
        try:
            sessions = es.opencode_db_sessions(self.home)
        finally:
            es.sqlite3.connect = real_connect
        self.assertEqual([s["session_id"] for s in sessions],
                         ["ses_new", "ses_old"])
        self.assertGreaterEqual(calls["n"], 2)  # 第一次失败后走了副本

    def test_doc_assembled_matches_export_shape(self):
        doc = es.opencode_db_session_doc(self.home, "ses_new")
        self.assertIsInstance(doc, dict)
        if cleaner is not None:
            self.assertTrue(cleaner.looks_like_opencode(doc))
        info = doc["info"]
        self.assertEqual(info["id"], "ses_new")
        self.assertEqual(info["time"]["created"], self.T0 + 1000)
        self.assertEqual(info["time"]["updated"], self.T0 + 9000)
        self.assertEqual([m["info"]["id"] for m in doc["messages"]],
                         ["msg_n1", "msg_n2"])
        m1, m2 = doc["messages"]
        self.assertEqual(m1["info"]["sessionID"], "ses_new")
        self.assertEqual([p["id"] for p in m2["parts"]],
                         ["prt_n2", "prt_n3", "prt_n4"])
        self.assertTrue(all(p["messageID"] == "msg_n2" for p in m2["parts"]))
        self.assertEqual(m1["parts"][0]["text"], "第一问")

    def test_doc_of_unknown_session_is_none(self):
        self.assertIsNone(es.opencode_db_session_doc(self.home, "ses_nope"))

    def test_doc_cleanable_by_real_cleaner(self):
        if cleaner is None:
            self.skipTest("cleaner 模块缺失")
        doc = es.opencode_db_session_doc(self.home, "ses_new")
        records, info = cleaner.clean_opencode_records(doc)
        self.assertEqual([r["k"] for r in records],
                         ["user", "tool", "assistant"])
        self.assertEqual(records[0]["text"], "第一问")
        self.assertEqual(records[1]["name"], "bash")
        self.assertEqual(records[2]["text"], "回答正文")
        self.assertEqual(info["created_at"], self.T0 + 1000)

    def test_pick_candidates_workdir_prefers_db_layout(self):
        sessions = es.opencode_db_sessions(self.home)
        cands = es.pick_opencode_candidates(sessions, self.T0 + 200,
                                            workdir=Path("P:\\proj"))
        self.assertEqual([c["session_id"] for c in cands], ["ses_new"])
        # db 里存的是正斜杠目录，与反斜杠工作目录同样能匹配
        cands2 = es.pick_opencode_candidates(sessions, self.T0 + 200,
                                             workdir=Path("P:/proj"))
        self.assertEqual(cands2[0]["session_id"], "ses_new")
        self.assertEqual(
            es.pick_opencode_candidates(sessions, self.T0 + 9999), [])


class TestOpenCodeExportChannel(unittest.TestCase):
    """opencode export CLI 通道：打桩 subprocess.run，校验 argv / PATH / 产物。"""

    DOC = {"info": {"id": "ses_x", "time": {"created": 1, "updated": 2}},
           "messages": []}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _patch(self, returncode=0, doc=None, raw=None):
        """打桩 es.subprocess.run（写入指定产物）与 find_opencode_bin。"""
        recorded = {}

        def fake_run(argv_, stdout=None, stderr=None, stdin=None,
                     timeout=None, env=None):
            recorded["argv"] = argv_
            recorded["env"] = env
            recorded["timeout"] = timeout
            if stdout is not None:
                if raw is not None:
                    stdout.write(raw)
                else:
                    stdout.write(
                        json.dumps(doc if doc is not None else self.DOC,
                                   ensure_ascii=False).encode("utf-8"))
            return types.SimpleNamespace(returncode=returncode)

        orig_run, orig_bin = es.subprocess.run, es.find_opencode_bin
        es.subprocess.run = fake_run
        es.find_opencode_bin = lambda: "C:\\fake\\opencode.exe"
        self.addCleanup(setattr, es.subprocess, "run", orig_run)
        self.addCleanup(setattr, es, "find_opencode_bin", orig_bin)
        return recorded

    def test_export_invokes_cli_and_prepends_path(self):
        recorded = self._patch()
        self.assertTrue(es.opencode_export_session({}, "ses_x", self.out))
        self.assertEqual(recorded["argv"],
                         ["C:\\fake\\opencode.exe", "export", "ses_x"])
        exe_dir = str(Path("C:\\fake\\opencode.exe").resolve().parent)
        self.assertTrue(recorded["env"]["PATH"].startswith(exe_dir + os.pathsep))
        self.assertEqual(recorded["timeout"], es.OPENCODE_EXPORT_TIMEOUT)
        self.assertEqual(json.loads(self.out.read_text(encoding="utf-8")),
                         self.DOC)

    def test_export_rejects_foreign_session_json(self):
        self._patch(doc={"info": {"id": "ses_other"}, "messages": []})
        self.assertFalse(es.opencode_export_session({}, "ses_x", self.out))

    def test_export_nonzero_returncode(self):
        self._patch(returncode=1)
        self.assertFalse(es.opencode_export_session({}, "ses_x", self.out))

    def test_export_invalid_json_output(self):
        self._patch(raw=b"{broken")
        self.assertFalse(es.opencode_export_session({}, "ses_x", self.out))

    def test_no_bin_skips_channel(self):
        orig_bin = es.find_opencode_bin
        es.find_opencode_bin = lambda: None
        self.addCleanup(setattr, es, "find_opencode_bin", orig_bin)
        self.assertFalse(es.opencode_export_session({}, "ses_x", self.out))
        self.assertFalse(self.out.exists())

    def test_find_opencode_bin_alethic_tools(self):
        """PATH 里没有时，回退到安装器默认目录 AlethicTools\\opencode。"""
        orig_which = es.shutil.which
        orig_local = os.environ.get("LOCALAPPDATA")
        es.shutil.which = lambda name: None
        exe = (Path(self.tmp.name) / "Programs" / "AlethicTools"
               / "opencode" / "opencode.exe")
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        os.environ["LOCALAPPDATA"] = self.tmp.name

        def restore():
            es.shutil.which = orig_which
            if orig_local is None:
                os.environ.pop("LOCALAPPDATA", None)
            else:
                os.environ["LOCALAPPDATA"] = orig_local

        self.addCleanup(restore)
        self.assertEqual(es.find_opencode_bin(), str(exe))


class TestFinishFlowOpenCodeDb(_LocalServerTestCase):
    """交卷走 opencode.db 通道端到端：export 优先，失败降级 db 副本拼装，
    db 无候选再降级 JSON 目录扫描。"""

    EXPORT_DOC = {
        "info": {"id": "ses_db_1", "directory": "P:\\workdir",
                 "time": {"created": 1500, "updated": 2000}},
        "messages": [],
    }

    def setUp(self):
        self.install_tmp_dirs()
        self.tmp_home = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_home.name)
        _make_opencode_db(self.home, [{
            "id": "ses_db_1", "directory": "P:/workdir",
            "time_created": 1500, "time_updated": 2000,
            "messages": [{
                "id": "msg_d1", "time_created": 1600,
                "data": {"role": "user", "time": {"created": 1600}},
                "parts": [{"id": "prt_d1",
                           "data": {"type": "text", "text": "db 作答内容"}}],
            }],
        }])
        self.orig = {name: getattr(es, name) for name in
                     ("upload_session", "iter_opencode_sessions",
                      "opencode_home", "load_cleaner",
                      "opencode_export_session")}
        es.load_cleaner = lambda: _make_fake_cleaner(with_session=True)
        es.upload_session = lambda cfg, meta: None
        es.opencode_home = lambda cfg: self.home
        es.opencode_export_session = lambda cfg, sid, out: False
        es.iter_opencode_sessions = lambda home: []
        self.write_state(
            stage="enrolled", session_id="cloud-ses-1", session_token="tok-1",
            track="track1", opened_at="1970-01-01T00:00:00+00:00",
            agent="opencode")
        self.start_server()
        self.httpd.cfg = dict(self.httpd.cfg, agent="opencode")

    def tearDown(self):
        self.stop_server()
        for name, val in self.orig.items():
            setattr(es, name, val)
        self.restore_dirs()
        self.tmp_home.cleanup()

    def _post(self, path, body):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.url(path), data=data,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_finish_prefers_db_export_channel(self):
        def fake_export(cfg, sid, out_path):
            self.assertEqual(sid, "ses_db_1")
            out_path.write_text(
                json.dumps(self.EXPORT_DOC, ensure_ascii=False),
                encoding="utf-8")
            return True

        es.opencode_export_session = fake_export
        state = self._post("/api/finish", {})
        self.assertEqual(state["stage"], "uploaded")
        self.assertEqual(state["agent_session_id"], "ses_db_1")
        self.assertEqual(state["export_method"], "opencode export")
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(meta["export_method"], "opencode export")
        self.assertEqual(meta["agent_session_id"], "ses_db_1")

    def test_finish_db_copy_fallback_when_export_fails(self):
        state = self._post("/api/finish", {})
        self.assertEqual(state["stage"], "uploaded")
        self.assertEqual(state["agent_session_id"], "ses_db_1")
        self.assertEqual(state["export_method"], "opencode 数据库副本")
        raw = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(raw["info"]["id"], "ses_db_1")
        self.assertEqual(raw["messages"][0]["info"]["role"], "user")
        self.assertEqual(raw["messages"][0]["parts"][0]["text"], "db 作答内容")

    def test_finish_empty_db_falls_to_json_scan_then_404(self):
        (self.home / "opencode.db").unlink()
        req = urllib.request.Request(
            self.url("/api/finish"), data=json.dumps({}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 404)
        payload = json.loads(cm.exception.read())
        cm.exception.close()
        self.assertEqual(payload["hint"], "manual")
        self.assertIn("没找到你的作答记录", payload["error"])
        self.assertNotIn("Traceback", payload["error"])


class TestPageCopyM11(unittest.TestCase):
    """小白化页面文案钉死：声明条 / 大按钮 / 兜底入口。"""

    def test_declaration_banner(self):
        self.assertIn("只上传", es.PAGE)
        self.assertIn("清洗后的会话记录", es.PAGE)
        self.assertIn("第三方软件", es.PAGE)
        self.assertIn("pending_upload", es.PAGE)
        self.assertIn("自动补传", es.PAGE)

    def test_big_buttons(self):
        self.assertIn("我已理解，开始在 AI 工具里作答", es.PAGE)
        self.assertIn("清洗并上传我的作答", es.PAGE)
        self.assertIn("重试上传", es.PAGE)

    def test_manual_fallback_input(self):
        self.assertIn('id="manual-session-file"', es.PAGE)

    def test_finish_error_copy_is_plain(self):
        # 服务端「没找到会话」的 404 文案是白话（不出现 traceback/术语）
        import inspect
        src = inspect.getsource(es)
        self.assertIn("没找到你的作答记录", src)
        self.assertIn("手动选择会话文件", src)

    def test_opencode_zen_declaration(self):
        # R1 结论：免费模型是 OpenCode Zen 限时型号，仍需注册登录，不许宣称免登录
        self.assertIn("OpenCode Zen", es.PAGE)
        self.assertIn("限时免费型号", es.PAGE)
        self.assertIn("opencode auth login", es.PAGE)
        self.assertNotIn("免登录", es.PAGE)


if __name__ == "__main__":
    unittest.main()

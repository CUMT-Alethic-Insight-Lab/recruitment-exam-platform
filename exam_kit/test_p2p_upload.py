"""P2P 直连上传（bundled tsnet-socks + Headscale mesh）单测。

stdlib unittest，无第三方依赖；全部在打桩边界上进行：
cloud_request（p2p-key / 云端标记）、_curl_exec（curl HTTP 传输）、
lan_http（接收机 HTTP）、P2PManager._spawn（tsnet-socks 子进程，假 stdout 行）。
不需要真实 tsnet-socks 二进制与真实网络。
"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import exam_server as es


_P2P_KEY_PAYLOAD = {
    "login_server": "https://headscale.alethicinsight.com",
    "auth_key": "hskey-auth-testkey",
    "lab_ip": "100.64.0.5",
    "receiver_port": 8897,
    "api_port": 8901,
}


class _FakeProc:
    """假 tsnet-socks 子进程：stdout 吐出预定行后 EOF，记录 terminate/kill。"""

    def __init__(self, stdout_lines=()):
        self.stdout = list(stdout_lines)
        self.stderr: list[str] = []
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class _HungStdout:
    """模拟驻留但无输出的 stdout（进程挂着不入网）：迭代时先睡 hang 秒再 EOF。"""

    def __init__(self, hang: float = 3.0):
        self._hang = hang

    def __iter__(self):
        time.sleep(self._hang)
        return iter([])


class _P2PTestBase(unittest.TestCase):
    """公共脚手架：临时 STATE/PENDING、假 tsnet-socks 目录、干净的 P2PManager。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.orig_state = es.STATE_PATH
        self.orig_pending = es.PENDING_DIR
        self.orig_p2p = es._P2P
        self.orig_curl_exec = es._curl_exec
        self.orig_cloud_request = es.cloud_request
        self.orig_lan_http = es.lan_http
        self.orig_port_in_use = es._port_in_use
        self.orig_transport_env = os.environ.get("ALE_THIC_HTTP_TRANSPORT")
        os.environ["ALE_THIC_HTTP_TRANSPORT"] = "curl"  # 云端直传固定走 curl 桩
        es._port_in_use = lambda port: False  # 确定性：1055 视为空闲
        es.STATE_PATH = root / "state.json"
        es.STATE_PATH.write_text(
            json.dumps({"stage": "finished", "student_id": "08260001",
                        "auth_token": "tok-applicant-1"}), encoding="utf-8")
        es.PENDING_DIR = root / "pending_upload"
        (es.PENDING_DIR / "s1").mkdir(parents=True)
        (es.PENDING_DIR / "s1" / "cleaned.jsonl").write_bytes(b'{"x": 1}\n')
        # 假 bundled tsnet-socks：只要求文件存在（不会真的执行）
        self.ts_dir = root / "tailscale"
        self.ts_dir.mkdir()
        (self.ts_dir / "tsnet-socks.exe").write_bytes(b"MZ")
        self.mgr = es.P2PManager()
        self.mgr.state_dir = root / "ts_state"
        es._P2P = self.mgr
        self.meta = {
            "session_id": "cloud-ses-1",
            "session_token": "tok-123",
            "student_id": "08260001",
            "cleaned_jsonl": "s1/cleaned.jsonl",
            "repo_url": "",
            "attachment_name": "",
        }
        self.curl_calls: list[list[str]] = []
        self.cloud_calls: list[tuple] = []
        self.lan_calls: list[tuple] = []

        def fake_curl_exec(argv, input_data=None, timeout=60):
            self.curl_calls.append(list(argv))
            return 200, b"{}"

        es._curl_exec = fake_curl_exec

    def tearDown(self):
        self.mgr.teardown()
        es._P2P = self.orig_p2p
        es._curl_exec = self.orig_curl_exec
        es.cloud_request = self.orig_cloud_request
        es.lan_http = self.orig_lan_http
        es._port_in_use = self.orig_port_in_use
        es.STATE_PATH = self.orig_state
        es.PENDING_DIR = self.orig_pending
        if self.orig_transport_env is None:
            os.environ.pop("ALE_THIC_HTTP_TRANSPORT", None)
        else:
            os.environ["ALE_THIC_HTTP_TRANSPORT"] = self.orig_transport_env
        self._tmp.cleanup()

    def _cfg(self, **extra):
        cfg = dict(es.DEFAULT_CONFIG)
        cfg.update({"cloud_base": "https://cloud.example",
                    "campaign_id": "2026autumn",
                    "tailscale_dir": str(self.ts_dir),
                    "p2p_connect_timeout": 2,
                    "http_timeout": 5})
        cfg.update(extra)
        return cfg

    def _fake_cloud_request(self, cfg, method, path, body=None,
                            session_token=None, bearer=None):
        self.cloud_calls.append((method, path, bearer, session_token))
        if path.endswith("/exam/p2p-key"):
            return 200, dict(_P2P_KEY_PAYLOAD)
        return 200, {}


class TestP2PKeyFallback(_P2PTestBase):
    """p2p-key 不可用（503 / 其他错误）→ 静默回退云端直传，且本会话不重试。"""

    def test_503_falls_back_to_cloud_silently(self):
        def cloud_503(cfg, method, path, body=None, session_token=None, bearer=None):
            self.cloud_calls.append((method, path, bearer, session_token))
            if path.endswith("/exam/p2p-key"):
                raise es.CloudError("p2p unavailable", status=503,
                                    detail="p2p unavailable")
            return 200, {}

        es.cloud_request = cloud_503
        es.upload_session(self._cfg(), self.meta)  # 不抛即通过
        # 走了云端直传：multipart POST 到云端 sessions 端点
        self.assertEqual(len(self.curl_calls), 1)
        argv = self.curl_calls[0]
        self.assertEqual(
            argv[-1], "https://cloud.example/api/campaigns/2026autumn/sessions")
        self.assertIn("X-Session-Token: tok-123", argv)
        # p2p-key 用申请人 Bearer 请求过且只请求一次；未拉起 tsnet-socks
        key_calls = [c for c in self.cloud_calls if c[1].endswith("/exam/p2p-key")]
        self.assertEqual(len(key_calls), 1)
        self.assertEqual(key_calls[0][2], "tok-applicant-1")
        self.assertIsNone(self.mgr._proc)
        self.assertEqual(self.mgr.status(), "fallback")
        # 第二次上传不再重试 P2P
        es.upload_session(self._cfg(), self.meta)
        self.assertEqual(
            len([c for c in self.cloud_calls if c[1].endswith("/exam/p2p-key")]), 1)
        self.assertEqual(len(self.curl_calls), 2)

    def test_other_key_error_also_falls_back(self):
        def cloud_500(cfg, method, path, body=None, session_token=None, bearer=None):
            if path.endswith("/exam/p2p-key"):
                raise es.CloudError("云端返回错误（HTTP 500）", status=500)
            return 200, {}

        es.cloud_request = cloud_500
        es.upload_session(self._cfg(), self.meta)
        self.assertEqual(self.curl_calls[0][-1],
                         "https://cloud.example/api/campaigns/2026autumn/sessions")
        self.assertEqual(self.mgr.status(), "fallback")

    def test_disabled_goes_cloud_directly(self):
        es.cloud_request = self._fake_cloud_request
        es.upload_session(self._cfg(p2p_enabled=False), self.meta)
        self.assertEqual(len(self.curl_calls), 1)
        self.assertEqual(self.cloud_calls, [])  # 完全没碰 p2p-key
        self.assertEqual(self.mgr.status(), "off")

    def test_missing_exe_goes_cloud(self):
        cfg = self._cfg(tailscale_dir=str(Path(self._tmp.name) / "no_such_dir"))
        es.cloud_request = self._fake_cloud_request
        es.upload_session(cfg, self.meta)
        self.assertEqual(len(self.curl_calls), 1)
        self.assertEqual(self.cloud_calls, [])
        self.assertEqual(self.mgr.status(), "fallback")


class TestP2PJoinSuccess(_P2PTestBase):
    """成功接入 tailnet：复用 lan 分流上传，接收机 base 指向 tailnet IP，
    且 HTTP 经 tsnet-socks 的 SOCKS5 代理（curl --socks5-hostname）。"""

    def setUp(self):
        super().setUp()
        self.spawned: list[list[str]] = []
        self.proc = _FakeProc(stdout_lines=["READY 100.64.0.9\n"])

        def fake_spawn(argv):
            self.spawned.append(list(argv))
            return self.proc

        self.mgr._spawn = fake_spawn

        def fake_lan_http(base, method, path, data=None, headers=None,
                          timeout=60, socks5=None):
            self.lan_calls.append((base, method, path, socks5))
            return {}

        es.lan_http = fake_lan_http
        es.cloud_request = self._fake_cloud_request

    def test_join_then_lan_style_upload_over_socks5(self):
        es.upload_session(self._cfg(), self.meta)  # 不抛即通过
        # tsnet-socks 以契约 flags 拉起（键值分离形式）
        self.assertEqual(len(self.spawned), 1)
        argv = self.spawned[0]
        self.assertTrue(argv[0].endswith("tsnet-socks.exe"))
        self.assertEqual(argv[argv.index("-control-url") + 1],
                         "https://headscale.alethicinsight.com")
        self.assertEqual(argv[argv.index("-auth-key") + 1], "hskey-auth-testkey")
        self.assertEqual(argv[argv.index("-hostname") + 1], "exam-08260001")
        self.assertEqual(argv[argv.index("-state-dir") + 1], str(self.mgr.state_dir))
        self.assertEqual(argv[argv.index("-socks5") + 1], "127.0.0.1:1055")
        self.assertEqual(argv[argv.index("-timeout") + 1], "25s")
        # READY 行带回本节点 tailnet IP
        self.assertEqual(self.mgr._tailnet_ip, "100.64.0.9")
        # lan 分流：base 指向实验室 tailnet IP，且全程带 SOCKS5 代理
        self.assertTrue(self.lan_calls)
        bases = {c[0] for c in self.lan_calls}
        self.assertEqual(bases, {"http://100.64.0.5:8897"})
        self.assertTrue(all(c[3] == "127.0.0.1:1055" for c in self.lan_calls))
        paths = [c[2] for c in self.lan_calls]
        self.assertEqual(paths[0], "/api/sessions/2026autumn/cloud-ses-1/begin")
        self.assertIn("/api/sessions/2026autumn/cloud-ses-1/file/cleaned_jsonl",
                      paths)
        self.assertEqual(paths[-1], "/api/sessions/2026autumn/cloud-ses-1/done")
        # 云端只收轻量标记（X-Session-Token，不经 SOCKS）
        marks = [c for c in self.cloud_calls if "lan-uploaded" in c[1]]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0][3], "tok-123")
        # 状态上报 connected；curl 直传没有被动用
        self.assertEqual(self.mgr.status(), "connected")
        self.assertEqual(self.curl_calls, [])
        # 已连接时后续上传直接复用，不再重新接入
        es.upload_session(self._cfg(), self.meta)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(
            len([c for c in self.cloud_calls if c[1].endswith("/exam/p2p-key")]), 1)

    def test_socks_port_busy_probes_free_port(self):
        es._port_in_use = lambda port: True  # 1055 被占
        es.upload_session(self._cfg(), self.meta)
        argv = self.spawned[0]
        socks = argv[argv.index("-socks5") + 1]
        self.assertTrue(socks.startswith("127.0.0.1:"))
        self.assertNotEqual(socks, "127.0.0.1:1055")
        # lan 分流用的是同一个探测出来的地址
        self.assertTrue(all(c[3] == socks for c in self.lan_calls))

    def test_teardown_terminate_cleanup(self):
        es.upload_session(self._cfg(), self.meta)
        self.assertTrue(self.mgr.state_dir.exists())
        self.mgr.teardown()
        self.assertTrue(self.proc.terminated)
        self.assertFalse(self.mgr.state_dir.exists())
        self.assertEqual(self.mgr.status(), "fallback")  # 已断开且本会话不再接入

    def test_lan_transfer_failure_falls_back_to_cloud(self):
        def failing_lan(base, method, path, data=None, headers=None,
                        timeout=60, socks5=None):
            raise es.CloudError(f"无法连接会话接收服务（{base}）： refused")

        es.lan_http = failing_lan
        es.upload_session(self._cfg(), self.meta)  # 不抛：回退云端直传
        self.assertEqual(len(self.curl_calls), 1)
        self.assertEqual(
            self.curl_calls[0][-1],
            "https://cloud.example/api/campaigns/2026autumn/sessions")
        self.assertEqual(self.mgr.status(), "fallback")


class TestP2PJoinFailure(_P2PTestBase):
    """tsnet-socks 拉起后入网失败（FAIL 行 / 超时 / 秒退）→ 静默回退云端。"""

    def setUp(self):
        super().setUp()
        es.cloud_request = self._fake_cloud_request
        self.spawned: list[list[str]] = []

    def _spawn_returning(self, proc):
        def fake_spawn(argv):
            self.spawned.append(list(argv))
            return proc
        self.mgr._spawn = fake_spawn

    def test_fail_line_falls_back_to_cloud(self):
        proc = _FakeProc(stdout_lines=["FAIL up: context deadline exceeded\n"])
        proc.returncode = 1
        self._spawn_returning(proc)
        es.upload_session(self._cfg(), self.meta)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.curl_calls[0][-1],
                         "https://cloud.example/api/campaigns/2026autumn/sessions")
        self.assertEqual(self.mgr.status(), "fallback")
        self.assertTrue(proc.terminated)  # 入网失败的进程被清理

    def test_ready_timeout_falls_back_to_cloud(self):
        proc = _FakeProc()  # 驻留但一直无输出
        proc.stdout = _HungStdout(hang=3.0)
        self._spawn_returning(proc)
        es.upload_session(self._cfg(p2p_connect_timeout=1), self.meta)
        self.assertEqual(self.curl_calls[0][-1],
                         "https://cloud.example/api/campaigns/2026autumn/sessions")
        self.assertEqual(self.mgr.status(), "fallback")
        self.assertTrue(proc.terminated)


class TestLanHttpSocks5(unittest.TestCase):
    """lan_http 带 socks5 时强制 curl 传输并加 --socks5-hostname（urllib 不走 SOCKS）。"""

    def setUp(self):
        self.orig_curl_exec = es._curl_exec
        self.orig_curl_exe = es._curl_exe
        self.orig_transport_env = os.environ.get("ALE_THIC_HTTP_TRANSPORT")
        os.environ["ALE_THIC_HTTP_TRANSPORT"] = "urllib"  # 即便环境强制 urllib 也必须走 curl

    def tearDown(self):
        es._curl_exec = self.orig_curl_exec
        es._curl_exe = self.orig_curl_exe
        if self.orig_transport_env is None:
            os.environ.pop("ALE_THIC_HTTP_TRANSPORT", None)
        else:
            os.environ["ALE_THIC_HTTP_TRANSPORT"] = self.orig_transport_env

    def test_socks5_forces_curl_with_proxy_flag(self):
        calls = []

        def fake_curl_exec(argv, input_data=None, timeout=60):
            calls.append((list(argv), input_data))
            return 200, b'{"ok": true}'

        es._curl_exec = fake_curl_exec
        out = es.lan_http("http://100.64.0.5:8897/", "POST", "/api/sessions/c/s/done",
                          data=b'{"repo_url": ""}', headers={"Content-Type": "application/json"},
                          socks5="127.0.0.1:1055")
        self.assertEqual(out, {"ok": True})
        argv, sent = calls[0]
        self.assertIn("--socks5-hostname", argv)
        self.assertIn("127.0.0.1:1055", argv)
        self.assertEqual(argv[-1], "http://100.64.0.5:8897/api/sessions/c/s/done")
        self.assertEqual(sent, b'{"repo_url": ""}')

    def test_socks5_without_curl_raises(self):
        es._curl_exe = lambda: None
        with self.assertRaises(es.CloudError) as ctx:
            es.lan_http("http://100.64.0.5:8897", "POST", "/x", data=b"1",
                        socks5="127.0.0.1:1055")
        self.assertIn("curl", str(ctx.exception))


class TestP2PStateAPI(unittest.TestCase):
    """/api/state 暴露 p2p 字段，页面/运维可分辨上传走的链路。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.orig_state = es.STATE_PATH
        self.orig_p2p = es._P2P
        es.STATE_PATH = root / "state.json"
        es.STATE_PATH.write_text(json.dumps({"stage": "logged_in"}),
                                 encoding="utf-8")
        self.mgr = es.P2PManager()
        self.mgr.state_dir = root / "ts_state"
        es._P2P = self.mgr
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), es.ExamHandler)
        self.httpd.cfg = dict(es.DEFAULT_CONFIG)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        es._P2P = self.orig_p2p
        es.STATE_PATH = self.orig_state
        self._tmp.cleanup()

    def _get_state(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/state", timeout=5) as resp:
            return json.loads(resp.read())

    def test_state_reports_off_before_any_attempt(self):
        self.assertEqual(self._get_state()["p2p"], "off")

    def test_state_reports_fallback_after_failure(self):
        self.mgr._attempted = True
        self.assertEqual(self._get_state()["p2p"], "fallback")

    def test_state_reports_connected_when_up(self):
        self.mgr._attempted = True
        self.mgr._connected = True
        self.assertEqual(self._get_state()["p2p"], "connected")


if __name__ == "__main__":
    unittest.main()

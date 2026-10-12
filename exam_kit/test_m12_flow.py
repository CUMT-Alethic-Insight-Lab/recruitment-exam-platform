"""M12 流程重构单测（stdlib unittest，无第三方依赖）。

覆盖：
- free_creation 两步向导：选领域 → 选题（question_ids）/ 自由发挥（domain）分支，
  本地校验（领域必须 ∈ 问卷所选、仅 track3 支持 domain）与云端请求体契约；
- free_creation 分领域循环交卷：my_sessions 对账（只认 uploaded、本地 pending_base
  保留、云端故障本地兜底、全部交完收敛完成页）、交完一个领域回领域列表、
  已交领域本地拦截 400、换领域清上一轮字段、作废领域释放重开、重登录对账进完成页；
- 清洗结果留档下载：cleaned.html 优先 / kind=md 显式 / md 兜底、
  中文文件名 RFC5987 filename*、越权/非法 base 拒绝；
- own_project 自有项目直交：登录存 account_url（账号页兜底入口）、
  /api/open_account_page 拉起浏览器、机试包内 multipart 直交页面要素；
- 手动导出清洗上传：zip 与 json 两种文件、X-Source-Name 中文名 URL 解码；
- 预览端点：cleaned.html 优先、cleaned.md 退化、越权/不存在 base 404；
- 自动发现「在考试工作区帮我找」：仅在点击后执行、严格限定考试工作区目录、
  候选字段（目录/更新时间/来源）、clean_scanned 三通道（kimi 导出 / opencode.db
  export 优先、db 副本拼装 / opencode 导出 JSON 文件）、伪造候选 404。
"""

import io
import json
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import exam_server as es
from test_exam_server import (
    _AUTH_TOKEN,
    TestAuthFlowBase,
    _LocalServerTestCase,
    _StubAuthCloudHandler,
    _make_fake_cleaner,
    _make_opencode_db,
)


def _make_html_cleaner() -> types.ModuleType:
    """产出 cleaned.html 的假清洗模块（模拟 A 线升级后的 cleaner）。"""
    mod = types.ModuleType("cleaner")

    def clean_session(path, out_dir):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        jl = out_dir / "cleaned.jsonl"
        md = out_dir / "cleaned.md"
        html = out_dir / "cleaned.html"
        jl.write_text('{"cleaned": true}\n', encoding="utf-8")
        md.write_text("# cleaned\n", encoding="utf-8")
        html.write_text("<html><body><h1>清洗预览</h1></body></html>",
                        encoding="utf-8")
        return {"cleaned_jsonl": jl, "cleaned_md": md, "cleaned_html": html,
                "stats": {"events": 1, "user": 1}}

    mod.clean_session = clean_session
    return mod


def _opencode_doc(directory: str, sid: str = "ses_oc_1") -> dict:
    return {
        "info": {"id": sid, "directory": directory,
                 "time": {"created": 1000, "updated": 2000}},
        "messages": [
            {"info": {"id": "msg_1", "sessionID": sid, "role": "user",
                      "time": {"created": 1010}},
             "parts": [{"id": "prt_1", "messageID": "msg_1",
                        "type": "text", "text": "作答内容"}]},
        ],
    }


def _kimi_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("state.json", json.dumps({"id": "kimi-1"}))
        z.writestr("agents/main/wire.jsonl", '{"msg":"hello"}\n')
    return buf.getvalue()


class _M12CloudHandler(_StubAuthCloudHandler):
    """打桩云端（M12）：额外服务本人报名记录（domains）、my_sessions 对账、
    enroll domain 回显（每次 enroll 派发递增的会话号，模拟多领域多会话）。"""

    def do_GET(self):
        if "/applications/" in self.path:
            self._record()
            return self._json(200, {"domains": self.server.app_domains})
        if self.path.endswith("/exam/my_sessions"):
            self._record()
            if self.server.mode == "expired":
                return self._json(401, {"detail": "请重新登录"})
            return self._json(200, self.server.my_sessions)
        return super().do_GET()

    def do_POST(self):
        if self.path.endswith("/exam/enroll"):
            self._record()
            payload = json.loads(self.server.requests[-1]["body"] or b"{}")
            self.server.enroll_count += 1
            n = self.server.enroll_count
            self._json(200, {
                "session_id": f"cloud-ses-{n}", "session_token": f"tok-{n}",
                "machine_id": "m-1", "track": payload.get("track") or "selected",
                "domain_tags": [payload["domain"]] if payload.get("domain") else [],
                "questions": [], "track3_guide_text": "自由创作引导语",
                "rerolls_left": 2})
            return
        return super().do_POST()


def _uploaded_session(session_id: str, domain_tags: list,
                      repo_url: str = "", status: str = "uploaded") -> dict:
    return {"session_id": session_id, "track": "track3",
            "domain_tags": domain_tags, "status": status,
            "opened_at": "2026-09-26T01:00:00+00:00",
            "uploaded_at": "2026-09-26T02:00:00+00:00" if status == "uploaded" else None,
            "repo_url": repo_url}


class _FreeCreationBase(TestAuthFlowBase):
    """free_creation 模式基类：打桩云端带 domains / my_sessions / 递增 enroll 会话号。"""

    def install_cloud(self):
        self.cloud = ThreadingHTTPServer(("127.0.0.1", 0), _M12CloudHandler)
        self.cloud.requests = []
        self.cloud.mode = "ok"
        self.cloud.exam_mode = "free_creation"
        self.cloud.app_domains = ["llm_arch", "memory", "agent"]
        self.cloud.my_sessions = []
        self.cloud.enroll_count = 0
        self.cloud_port = self.cloud.server_address[1]
        threading.Thread(target=self.cloud.serve_forever, daemon=True).start()

    def setUp(self):
        super().setUp()
        # 登录建领域工作区：工作目录指到临时目录，绝不碰真实考试目录
        self.httpd.cfg["agent_workdir"] = str(self.root / "考试工作目录")

    def login_free(self):
        return self.post_json("/api/login",
                              {"student_id": "08260001", "password": "pw"})

    def _post_error(self, path, body: dict):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        payload = json.loads(cm.exception.read() or b"{}")
        code = cm.exception.code
        cm.exception.close()
        return code, payload

    def _get_error(self, path: str):
        req = urllib.request.Request(self.url(path), method="GET")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        payload = json.loads(cm.exception.read() or b"{}")
        code = cm.exception.code
        cm.exception.close()
        return code, payload


class TestWizardEnrollFlow(_FreeCreationBase):
    """free_creation 两步向导：选领域 → 选题 / 自由发挥两条分支的 enroll 契约。"""

    def test_login_free_creation_gets_domain_keys(self):
        out = self.login_free()
        self.assertEqual(out["exam_mode"], "free_creation")
        self.assertEqual(out["domain_keys"], ["llm_arch", "memory", "agent"])
        # 领域工作区建在（临时）考试工作目录下
        self.assertTrue((self.root / "考试工作目录" / "语言-LLM").is_dir())

    def test_wizard_pick_question_enrolls_track3_with_question_ids(self):
        self.login_free()
        out = self.post_json("/api/enroll",
                             {"track": "track3", "question_ids": ["q-1"]})
        self.assertEqual(out["stage"], "enrolled")
        req = self.cloud_enroll_requests()[-1]
        self.assertEqual(json.loads(req["body"]),
                         {"track": "track3", "question_ids": ["q-1"]})
        state = self.read_state()
        self.assertTrue(state["self_selected"])
        self.assertEqual(state["domain_tags"], [])

    def test_wizard_free_pick_enrolls_track3_with_domain(self):
        self.login_free()
        out = self.post_json("/api/enroll",
                             {"track": "track3", "domain": "llm_arch"})
        self.assertEqual(out["stage"], "enrolled")
        req = self.cloud_enroll_requests()[-1]
        self.assertEqual(json.loads(req["body"]),
                         {"track": "track3", "domain": "llm_arch"})
        state = self.read_state()
        self.assertEqual(state["track"], "track3")
        self.assertEqual(state["questions"], [])
        self.assertEqual(state["domain_tags"], ["llm_arch"])
        self.assertTrue(state["self_selected"])

    def test_wizard_domain_outside_applicant_rejected_locally(self):
        self.login_free()
        code, payload = self._post_error(
            "/api/enroll", {"track": "track3", "domain": "infra"})
        self.assertEqual(code, 400)
        self.assertIn("只能在自己报名的领域内开考", payload["error"])
        self.assertEqual(self.cloud_enroll_requests(), [])  # 未发云端

    def test_wizard_domain_with_non_track3_rejected_locally(self):
        self.login_free()
        code, payload = self._post_error(
            "/api/enroll", {"track": "track1", "domain": "llm_arch"})
        self.assertEqual(code, 400)
        self.assertIn("只有自由创作轨迹支持按领域开考", payload["error"])
        self.assertEqual(self.cloud_enroll_requests(), [])


class TestMySessionsSync(_FreeCreationBase):
    """GET /api/my_sessions 对账：服务端为准重建 submissions（只认 uploaded），
    本地 pending_base 保留；云端故障本地兜底；全部交完收敛到完成页。"""

    def _write_logged_in(self, **extra):
        self.write_state(stage="logged_in", name="张三", auth_token=_AUTH_TOKEN,
                         exam_mode="free_creation",
                         domain_keys=["llm_arch", "memory", "agent"], **extra)

    def test_my_sessions_reconciles_uploaded_only(self):
        self._write_logged_in()
        self.cloud.my_sessions = [
            _uploaded_session("s-up", ["llm_arch"], "https://github.com/x/a"),
            _uploaded_session("s-void", ["memory"], status="voided"),
            _uploaded_session("s-draft", ["agent"], status="draft"),
        ]
        out = self.get_json("/api/my_sessions")
        self.assertTrue(out["ok"])
        self.assertTrue(out["synced"])
        # 只认 uploaded：voided/draft 不计入完成状态
        self.assertEqual(list(out["submissions"]), ["llm_arch"])
        info = out["submissions"]["llm_arch"]
        self.assertEqual(info["session_id"], "s-up")
        self.assertEqual(info["repo_url"], "https://github.com/x/a")
        self.assertEqual(info["domains"], ["llm_arch"])
        self.assertEqual(len(out["my_sessions"]), 3)  # 原始列表照常透传给页面
        # 对账结果持久化（刷新/重开不丢）
        self.assertEqual(self.read_state()["submissions"], out["submissions"])
        # 云端请求契约：Bearer 考生令牌
        req = [r for r in self.cloud.requests
               if r["path"].endswith("/exam/my_sessions")][-1]
        self.assertEqual(req["authorization"], f"Bearer {_AUTH_TOKEN}")

    def test_my_sessions_multi_domain_session_covers_all_tags(self):
        self._write_logged_in()
        self.cloud.my_sessions = [
            _uploaded_session("s-multi", ["memory", "agent"])]
        out = self.get_json("/api/my_sessions")
        self.assertEqual(set(out["submissions"]), {"memory", "agent"})
        # 同一多领域会话的各领域共享同一条提交信息
        self.assertEqual(out["submissions"]["memory"]["session_id"],
                         out["submissions"]["agent"]["session_id"])

    def test_my_sessions_preserves_local_pending_base(self):
        self._write_logged_in(submissions={"llm_arch": {
            "session_id": "s-up", "pending_base": "08260001_s-up",
            "repo_url": "", "domains": ["llm_arch"], "uploaded_at": ""}})
        self.cloud.my_sessions = [
            _uploaded_session("s-up", ["llm_arch"], "https://github.com/x/a")]
        out = self.get_json("/api/my_sessions")
        info = out["submissions"]["llm_arch"]
        self.assertEqual(info["pending_base"], "08260001_s-up")  # 本地留档路径保留
        self.assertEqual(info["repo_url"], "https://github.com/x/a")  # 其余以服务端为准

    def test_my_sessions_requires_login(self):
        self.write_state()  # 无 auth_token
        code, payload = self._get_error("/api/my_sessions")
        self.assertEqual(code, 400)
        self.assertIn("请先登录", payload["error"])

    def test_my_sessions_cloud_401_expires_session(self):
        self._write_logged_in()
        self.cloud.mode = "expired"
        code, payload = self._get_error("/api/my_sessions")
        self.assertEqual(code, 401)
        self.assertIn("重新登录", payload["error"])
        state = self.read_state()
        self.assertNotIn("auth_token", state)
        self.assertEqual(state["stage"], "")

    def test_my_sessions_cloud_down_returns_local_fallback(self):
        self._write_logged_in(submissions={"memory": {
            "session_id": "s-9", "pending_base": "08260001_s-9",
            "repo_url": "", "domains": ["memory"], "uploaded_at": ""}})
        self.httpd.cfg["cloud_base"] = "http://127.0.0.1:1"  # 不可达
        out = self.get_json("/api/my_sessions")
        self.assertEqual(out["synced"], False)
        self.assertEqual(out["ok"], False)
        self.assertEqual(list(out["submissions"]), ["memory"])  # 本地兜底
        self.assertIn("error", out)

    def test_my_sessions_all_done_collapses_to_done_page(self):
        self._write_logged_in()
        self.cloud.my_sessions = [
            _uploaded_session("s-1", ["llm_arch"]),
            _uploaded_session("s-2", ["memory", "agent"]),
        ]
        out = self.get_json("/api/my_sessions")
        self.assertEqual(out["stage"], "uploaded")
        self.assertTrue(out["uploaded"])
        self.assertEqual(self.read_state()["stage"], "uploaded")


class TestDomainLoopFlow(_FreeCreationBase):
    """free_creation 分领域循环交卷：交完一个领域回领域列表，全部交完进完成页；
    已交领域本地拦截；换领域清上一轮字段（repo_url 每次单独问）。"""

    def setUp(self):
        super().setUp()
        self.orig = {n: getattr(es, n) for n in ("upload_session", "load_cleaner")}
        es.load_cleaner = lambda: _make_fake_cleaner(with_session=True)
        self.uploads = []
        es.upload_session = lambda cfg, meta: self.uploads.append(dict(meta))

    def tearDown(self):
        for n, v in self.orig.items():
            setattr(es, n, v)
        super().tearDown()

    def _clean_current(self):
        req = urllib.request.Request(
            self.url("/api/clean_export"), data=_kimi_zip_bytes(),
            headers={"X-Source-Name": "session.zip"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            out = json.loads(resp.read())
        self.assertTrue(out["ok"])
        return out

    def _submit_domain(self, domain: str, repo_url: str = ""):
        out = self.post_json("/api/enroll", {"track": "track3", "domain": domain})
        self.assertEqual(out["stage"], "enrolled")
        self.assertEqual(out["domain_tags"], [domain])
        self._clean_current()
        return self.post_json("/api/upload_cleaned", {"repo_url": repo_url})

    def test_first_domain_submit_returns_to_wizard(self):
        self.login_free()
        state = self._submit_domain("llm_arch", "https://github.com/x/a")
        # 还剩 memory/agent：回领域列表（logged_in），本轮会话字段已清
        self.assertEqual(state["stage"], "logged_in")
        self.assertNotIn("session_id", state)
        self.assertNotIn("pending_base", state)
        self.assertNotIn("upload_pending", state)
        subs = state["submissions"]
        self.assertEqual(list(subs), ["llm_arch"])
        info = subs["llm_arch"]
        self.assertEqual(info["session_id"], "cloud-ses-1")
        self.assertEqual(info["repo_url"], "https://github.com/x/a")
        self.assertEqual(info["pending_base"], "08260001_cloud-ses-1")
        self.assertEqual(info["domains"], ["llm_arch"])
        # 上传打桩收到的 meta 带当次会话与 repo_url
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.uploads[0]["session_id"], "cloud-ses-1")
        self.assertEqual(self.uploads[0]["repo_url"], "https://github.com/x/a")
        # submissions 持久化到 state.json（刷新/重开不丢）
        self.assertEqual(self.read_state()["submissions"], subs)

    def test_submitted_domain_enroll_blocked_locally(self):
        self.login_free()
        self._submit_domain("llm_arch")
        n = len(self.cloud_enroll_requests())
        code, payload = self._post_error(
            "/api/enroll", {"track": "track3", "domain": "llm_arch"})
        self.assertEqual(code, 400)
        self.assertIn("该领域已提交", payload["error"])
        self.assertEqual(len(self.cloud_enroll_requests()), n)  # 未发云端

    def test_next_domain_enroll_resets_round_fields(self):
        self.login_free()
        self._submit_domain("llm_arch", "https://github.com/x/a")
        out = self.post_json("/api/enroll", {"track": "track3", "domain": "memory"})
        self.assertEqual(out["stage"], "enrolled")
        self.assertEqual(out["session_id"], "cloud-ses-2")  # 新会话
        self.assertEqual(out["domain_tags"], ["memory"])
        # 上一轮残留字段清空：repo_url 不带入下一领域（每次交卷单独问）
        self.assertNotIn("repo_url", out)
        self.assertNotIn("pending_base", out)
        self.assertNotIn("uploaded", out)
        self.assertNotIn("finished_at", out)
        self.assertIn("llm_arch", out["submissions"])  # 进度仍在
        # 完成第二领域后全部交完 → 进完成页
        state = self._submit_domain_after_enroll("memory", "https://github.com/x/b")
        self.assertEqual(state["stage"], "logged_in")  # 还剩 agent
        self.assertEqual(state["submissions"]["memory"]["repo_url"],
                         "https://github.com/x/b")

    def _submit_domain_after_enroll(self, domain: str, repo_url: str = ""):
        """已 enroll 后的后半程：清洗 → 上传。"""
        self._clean_current()
        return self.post_json("/api/upload_cleaned", {"repo_url": repo_url})

    def test_single_domain_student_finishes_to_done_page(self):
        self.cloud.app_domains = ["llm_arch"]
        self.login_free()
        state = self._submit_domain("llm_arch", "https://github.com/x/a")
        self.assertEqual(state["stage"], "uploaded")  # 唯一领域交完 → 完成页
        self.assertTrue(state["uploaded"])
        self.assertEqual(state["submissions"]["llm_arch"]["repo_url"],
                         "https://github.com/x/a")
        self.assertEqual(self.read_state()["stage"], "uploaded")

    def test_relogin_all_uploaded_goes_done_page(self):
        self.cloud.my_sessions = [
            _uploaded_session("s-1", ["llm_arch"], "https://github.com/x/a"),
            _uploaded_session("s-2", ["memory", "agent"]),
        ]
        out = self.login_free()
        self.assertEqual(out["stage"], "uploaded")
        self.assertTrue(out["uploaded"])
        self.assertEqual(set(out["submissions"]), {"llm_arch", "memory", "agent"})

    def test_relogin_partial_uploaded_back_to_wizard(self):
        self.cloud.my_sessions = [
            _uploaded_session("s-1", ["llm_arch"], "https://github.com/x/a")]
        out = self.login_free()
        self.assertEqual(out["stage"], "logged_in")
        self.assertEqual(set(out["submissions"]), {"llm_arch"})

    def test_relogin_same_machine_keeps_pending_base(self):
        # 模拟上一轮交卷后的本机 state.json（含下载留档用的 pending_base）
        self.write_state(submissions={"llm_arch": {
            "session_id": "s-1", "pending_base": "08260001_s-1",
            "repo_url": "", "domains": ["llm_arch"], "uploaded_at": ""}})
        self.cloud.my_sessions = [
            _uploaded_session("s-1", ["llm_arch"], "https://github.com/x/a")]
        out = self.login_free()
        info = out["submissions"]["llm_arch"]
        self.assertEqual(info["pending_base"], "08260001_s-1")
        self.assertEqual(info["repo_url"], "https://github.com/x/a")

    def test_voided_session_releases_domain_for_reenroll(self):
        self.cloud.my_sessions = [
            _uploaded_session("s-0", ["llm_arch"], status="voided")]
        out = self.login_free()
        self.assertEqual(out["stage"], "logged_in")
        self.assertEqual(out["submissions"], {})  # 作废不算完成
        out = self.post_json("/api/enroll", {"track": "track3", "domain": "llm_arch"})
        self.assertEqual(out["stage"], "enrolled")  # 该领域可重新开考


class TestOwnProjectFlow(TestAuthFlowBase):
    """own_project 直提流程：登录存 account_url，页面直提指引完备。"""

    def setUp(self):
        super().setUp()
        self.cloud.exam_mode = "own_project"
        self.httpd.cfg["agent_workdir"] = str(self.root / "考试工作目录")

    def test_login_own_project_stores_account_url(self):
        out = self.post_json("/api/login",
                             {"student_id": "08260001", "password": "pw"})
        self.assertEqual(out["exam_mode"], "own_project")
        expected = self.httpd.cfg["cloud_base"] + "/account"
        self.assertEqual(out["account_url"], expected)
        self.assertEqual(self.get_json("/api/state")["account_url"], expected)

    def test_own_project_page_copy_complete(self):
        # 机试包内直交：仓库链接输入 + 可选附件 + 提交按钮 + 改为做题入口 + 账号页兜底
        self.assertIn('id="stage-ownproject"', es.PAGE)
        self.assertIn('id="project-repo-url"', es.PAGE)
        self.assertIn('id="project-attachment-file"', es.PAGE)
        self.assertIn('id="project-submit-btn"', es.PAGE)
        self.assertIn("提交自有项目，完成交卷", es.PAGE)
        self.assertIn("submitOwnProject()", es.PAGE)
        self.assertIn("/api/submit_project", es.PAGE)
        self.assertIn("改为做题（从题库选题作答）", es.PAGE)
        self.assertIn("打开账号页", es.PAGE)

    def test_own_project_can_switch_to_pool(self):
        # exam_mode 只推荐默认落页：own_project 默认 project 视图，
        # 但页面保留切去题库自选/作答方式选择的入口（不硬拦）
        self.assertIn("preferredView", es.PAGE)
        self.assertIn('state.exam_mode === "own_project" ? "project"', es.PAGE)
        self.assertIn("enterPoolMode()", es.PAGE)
        self.assertIn("backToQuestionMode()", es.PAGE)


class TestOpenAccountPageEndpoint(_LocalServerTestCase):
    """own_project 账号页拉起端点：调用 open_browser，未登录 400。"""

    def setUp(self):
        self.install_tmp_dirs()
        self.start_server()
        self.orig_browser = es.open_browser

    def tearDown(self):
        es.open_browser = self.orig_browser
        self.stop_server()
        self.restore_dirs()

    def _post(self):
        req = urllib.request.Request(
            self.url("/api/open_account_page"), data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_open_calls_browser_with_account_url(self):
        self.write_state(account_url="http://cloud.test/account")
        calls = []
        es.open_browser = lambda url: calls.append(url) or True
        out = self._post()
        self.assertTrue(out["ok"])
        self.assertEqual(calls, ["http://cloud.test/account"])
        self.assertIn("账号页", out["hint"])

    def test_open_fallback_url_from_config(self):
        self.write_state()  # state 无 account_url：用 cloud_base 推导
        calls = []
        es.open_browser = lambda url: calls.append(url) or True
        out = self._post()
        self.assertTrue(out["ok"])
        self.assertEqual(calls, [self.httpd.cfg["cloud_base"] + "/account"])

    def test_requires_login(self):
        es.STATE_PATH.write_text(json.dumps({"stage": ""}), encoding="utf-8")
        es.open_browser = lambda url: True
        req = urllib.request.Request(
            self.url("/api/open_account_page"), data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()


class TestCleanExportAndPreview(_LocalServerTestCase):
    """手动导出清洗（zip/json）+ 预览端点（html 优先 / md 退化 / 越权 404）。"""

    def setUp(self):
        self.install_tmp_dirs()
        self.orig_cleaner = es.load_cleaner
        es.load_cleaner = lambda: _make_fake_cleaner(with_session=True)
        self.write_state(
            stage="enrolled", session_id="cloud-ses-1", session_token="tok-1",
            track="track1", opened_at="1970-01-01T00:00:00+00:00")
        self.start_server()

    def tearDown(self):
        es.load_cleaner = self.orig_cleaner
        self.stop_server()
        self.restore_dirs()

    def _post_bytes(self, data: bytes, name="session.zip"):
        req = urllib.request.Request(
            self.url("/api/clean_export"), data=data,
            headers={"X-Source-Name": name}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_clean_zip_via_fake_cleaner(self):
        out = self._post_bytes(_kimi_zip_bytes())
        self.assertTrue(out["ok"])
        self.assertEqual(out["preview_kind"], "md")
        self.assertEqual(out["agent_session_id"], "kimi-1")
        # 原始导出按内容识别为 zip 落盘
        self.assertTrue(
            (es.PENDING_DIR / "08260001_cloud-ses-1.zip").exists())

    def test_clean_json_via_fake_cleaner(self):
        doc = json.dumps(_opencode_doc("P:\\proj"),
                         ensure_ascii=False).encode("utf-8")
        out = self._post_bytes(doc, name="session.json")
        self.assertTrue(out["ok"])
        self.assertEqual(out["agent_session_id"], "ses_oc_1")
        self.assertTrue(
            (es.PENDING_DIR / "08260001_cloud-ses-1.json").exists())
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json")
            .read_text(encoding="utf-8"))
        self.assertIn("session.json", meta["export_method"])

    def test_source_name_url_decoded(self):
        quoted = urllib.parse.quote("粘贴的会话.json")
        out = self._post_bytes(
            json.dumps(_opencode_doc("P:\\proj"),
                       ensure_ascii=False).encode("utf-8"),
            name=quoted)
        self.assertTrue(out["ok"])
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json")
            .read_text(encoding="utf-8"))
        self.assertIn("粘贴的会话.json", meta["export_method"])

    def test_preview_html_preferred(self):
        es.load_cleaner = _make_html_cleaner
        out = self._post_bytes(_kimi_zip_bytes())
        self.assertEqual(out["preview_kind"], "html")
        with urllib.request.urlopen(
                self.url("/api/preview?base=08260001_cloud-ses-1")) as resp:
            self.assertIn("text/html", resp.headers["Content-Type"])
            self.assertIn("清洗预览", resp.read().decode("utf-8"))

    def test_preview_rejects_foreign_or_missing_base(self):
        self._post_bytes(_kimi_zip_bytes())
        for bad in ("../etc", "other_base", ""):
            req = urllib.request.Request(
                self.url("/api/preview?base=" + urllib.parse.quote(bad)),
                method="GET")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            self.assertEqual(cm.exception.code, 404, bad)
            cm.exception.close()

    def test_preview_before_clean_404(self):
        req = urllib.request.Request(
            self.url("/api/preview?base=08260001_cloud-ses-1"), method="GET")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()


class _ScanFlowBase(_LocalServerTestCase):
    """「在考试工作区帮我找」流程基类：临时 opencode_home / 工作目录 / 打桩清洗与上传。"""

    AGENT = "opencode"

    def setUp(self):
        self.install_tmp_dirs()
        self.workdir = self.root / "考试工作目录"
        self.in_dir = (self.workdir / "语言-LLM").as_posix()
        self.out_dir = (self.root / "elsewhere").as_posix()
        self.home = self.root / "oc_home"
        self.home.mkdir(parents=True)
        self.orig = {name: getattr(es, name) for name in
                     ("upload_session", "opencode_home", "load_cleaner")}
        es.load_cleaner = lambda: _make_fake_cleaner(with_session=True)
        es.upload_session = lambda cfg, meta: None
        es.opencode_home = lambda cfg: self.home
        self.write_state(
            stage="enrolled", session_id="cloud-ses-1", session_token="tok-1",
            track="track1", opened_at="1970-01-01T00:00:00+00:00",
            agent=self.AGENT)
        self.start_server()
        self.httpd.cfg = dict(self.httpd.cfg, agent=self.AGENT,
                              agent_workdir=str(self.workdir))

    def tearDown(self):
        self.stop_server()
        for name, val in self.orig.items():
            setattr(es, name, val)
        self.restore_dirs()

    def _post(self, path, body):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def _post_error(self, path, body):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        payload = json.loads(cm.exception.read() or b"{}")
        code = cm.exception.code
        cm.exception.close()
        return code, payload


class TestScanSessionsOpenCode(_ScanFlowBase):
    """自动发现：点击才执行、严格限定考试工作区、候选字段齐全、三通道清洗。"""

    def _make_db(self):
        _make_opencode_db(self.home, [
            {"id": "ses_in", "directory": self.in_dir,
             "time_created": 1500, "time_updated": 2000,
             "messages": [{
                 "id": "msg_d1", "time_created": 1600,
                 "data": {"role": "user", "time": {"created": 1600}},
                 "parts": [{"id": "prt_d1",
                            "data": {"type": "text", "text": "db 作答内容"}}],
             }]},
            {"id": "ses_out", "directory": self.out_dir,
             "time_created": 1600, "time_updated": 3000,
             "messages": [{
                 "id": "msg_o1", "time_created": 1700,
                 "data": {"role": "user", "time": {"created": 1700}},
                 "parts": [{"id": "prt_o1",
                            "data": {"type": "text", "text": "隐私会话不应出现"}}],
             }]},
        ])

    def test_scan_lists_only_workdir_sessions_with_fields(self):
        self._make_db()
        out = self._post("/api/scan_sessions", {})
        cands = out["candidates"]
        self.assertEqual([c["session_id"] for c in cands], ["ses_in"])
        c = cands[0]
        self.assertEqual(c["kind"], "opencode_db")
        self.assertEqual(c["directory"], self.in_dir)
        self.assertEqual(c["updated"], 2000)
        self.assertEqual(c["source"], "opencode 数据库")

    def test_scan_nothing_found_404_plain_copy(self):
        self._make_db()
        # 开考时间设到所有会话之后
        state = self.read_state()
        state["opened_at"] = "2100-01-01T00:00:00+00:00"
        es.STATE_PATH.write_text(json.dumps(state, ensure_ascii=False),
                                 encoding="utf-8")
        code, payload = self._post_error("/api/scan_sessions", {})
        self.assertEqual(code, 404)
        self.assertIn("没找到你的作答记录", payload["error"])
        self.assertNotIn("Traceback", payload["error"])

    def test_scan_only_runs_on_demand(self):
        self._make_db()
        calls = {"db": 0, "json": 0}
        orig_db, orig_json = es.opencode_db_sessions, es.iter_opencode_sessions
        es.opencode_db_sessions = lambda home: (calls.__setitem__("db", calls["db"] + 1),
                                                orig_db(home))[1]
        es.iter_opencode_sessions = lambda home: (calls.__setitem__("json", calls["json"] + 1),
                                                  orig_json(home))[1]
        try:
            # 手动导出清洗（第 2 小步主路径）：绝不触发会话库扫描
            req = urllib.request.Request(
                self.url("/api/clean_export"),
                data=json.dumps(_opencode_doc(self.in_dir),
                                ensure_ascii=False).encode("utf-8"),
                headers={"X-Source-Name": "session.json"}, method="POST")
            with urllib.request.urlopen(req) as resp:
                self.assertTrue(json.loads(resp.read())["ok"])
            self.assertEqual(calls, {"db": 0, "json": 0})
            # 点击「在考试工作区帮我找」后才扫描
            self._post("/api/scan_sessions", {})
            self.assertGreaterEqual(calls["db"], 1)
        finally:
            es.opencode_db_sessions, es.iter_opencode_sessions = orig_db, orig_json

    def test_clean_scanned_db_copy_fallback_then_upload(self):
        self._make_db()
        orig_export = es.opencode_export_session
        es.opencode_export_session = lambda cfg, sid, out: False
        try:
            out = self._post("/api/clean_scanned",
                             {"kind": "opencode_db", "session_id": "ses_in"})
        finally:
            es.opencode_export_session = orig_export
        self.assertTrue(out["ok"])
        self.assertEqual(out["agent_session_id"], "ses_in")
        raw = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(raw["info"]["id"], "ses_in")
        self.assertEqual(raw["messages"][0]["parts"][0]["text"], "db 作答内容")
        state = self._post("/api/upload_cleaned", {})
        self.assertEqual(state["stage"], "uploaded")
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(meta["export_method"], "opencode 数据库副本")

    def test_clean_scanned_prefers_export_channel(self):
        self._make_db()
        export_doc = _opencode_doc(self.in_dir, sid="ses_in")
        orig_export = es.opencode_export_session

        def fake_export(cfg, sid, out_path):
            self.assertEqual(sid, "ses_in")
            out_path.write_text(json.dumps(export_doc, ensure_ascii=False),
                                encoding="utf-8")
            return True

        es.opencode_export_session = fake_export
        try:
            out = self._post("/api/clean_scanned",
                             {"kind": "opencode_db", "session_id": "ses_in"})
        finally:
            es.opencode_export_session = orig_export
        self.assertTrue(out["ok"])
        state = self._post("/api/upload_cleaned", {})
        self.assertEqual(state["agent_session_id"], "ses_in")
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(meta["export_method"], "opencode export")

    def test_clean_scanned_json_file_channel(self):
        # 数据目录里有一个导出 JSON，会话目录在考试工作区
        edir = self.home / "exports"
        edir.mkdir(parents=True)
        doc = _opencode_doc(self.in_dir, sid="ses_file")
        (edir / "ses_file.json").write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        out = self._post("/api/scan_sessions", {})
        cands = [c for c in out["candidates"] if c["kind"] == "opencode_json"]
        self.assertEqual(len(cands), 1)
        self.assertTrue(cands[0]["file"].endswith("ses_file.json"))
        out = self._post("/api/clean_scanned",
                         {"kind": "opencode_json", "session_id": "ses_file",
                          "file": cands[0]["file"]})
        self.assertTrue(out["ok"])
        self.assertEqual(out["agent_session_id"], "ses_file")

    def test_clean_scanned_rejects_forged_candidate(self):
        self._make_db()
        code, payload = self._post_error(
            "/api/clean_scanned",
            {"kind": "opencode_db", "session_id": "ses_out"})  # 工作区外会话
        self.assertEqual(code, 404)
        self.assertIn("找不到了", payload["error"])
        code, _ = self._post_error(
            "/api/clean_scanned", {"kind": "evil", "session_id": "ses_in"})
        self.assertEqual(code, 404)  # 扫描结果里没有这种 kind，直接匹配失败


class TestScanSessionsKimi(_LocalServerTestCase):
    """kimi 通道的自动发现：只列考试工作区内的会话，clean_scanned 走导出管线。"""

    def setUp(self):
        self.install_tmp_dirs()
        self.workdir = self.root / "考试工作目录"
        self.in_dir = (self.workdir / "语言-LLM").as_posix()
        self.out_dir = (self.root / "elsewhere").as_posix()
        home = self.root / ".kimi-code"
        (home / "sessions" / "wd" / "kimi_in" / "agents" / "main").mkdir(parents=True)
        (home / "sessions" / "wd" / "kimi_in" / "state.json").write_text(
            json.dumps({"id": "kimi_in", "createdAt": 1500}), encoding="utf-8")
        (home / "sessions" / "wd" / "kimi_in" / "agents" / "main"
         / "wire.jsonl").write_text('{"msg":"in"}\n', encoding="utf-8")
        (home / "sessions" / "wd" / "kimi_out").mkdir(parents=True)
        (home / "sessions" / "wd" / "kimi_out" / "state.json").write_text(
            json.dumps({"id": "kimi_out", "createdAt": 2500}), encoding="utf-8")
        (home / "session_index.jsonl").write_text("\n".join([
            json.dumps({"sessionId": "kimi_in",
                        "sessionDir": str(home / "sessions" / "wd" / "kimi_in"),
                        "workDir": self.in_dir}),
            json.dumps({"sessionId": "kimi_out",
                        "sessionDir": str(home / "sessions" / "wd" / "kimi_out"),
                        "workDir": self.out_dir}),
        ]), encoding="utf-8")
        self.orig = {name: getattr(es, name) for name in
                     ("upload_session", "kimi_home", "load_cleaner",
                      "export_session")}
        es.load_cleaner = lambda: _make_fake_cleaner(with_session=True)
        es.upload_session = lambda cfg, meta: None
        es.kimi_home = lambda cfg: home
        self.write_state(
            stage="enrolled", session_id="cloud-ses-1", session_token="tok-1",
            track="track1", opened_at="1970-01-01T00:00:00+00:00")
        self.start_server()
        self.httpd.cfg = dict(self.httpd.cfg, agent="kimi",
                              agent_workdir=str(self.workdir))

    def tearDown(self):
        self.stop_server()
        for name, val in self.orig.items():
            setattr(es, name, val)
        self.restore_dirs()

    def _post(self, path, body):
        req = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_scan_lists_only_workdir_kimi_sessions(self):
        out = self._post("/api/scan_sessions", {})
        self.assertEqual([c["session_id"] for c in out["candidates"]],
                         ["kimi_in"])
        c = out["candidates"][0]
        self.assertEqual(c["kind"], "kimi")
        self.assertEqual(c["directory"], self.in_dir)
        self.assertEqual(c["updated"], 1500)
        self.assertEqual(c["source"], "Kimi Code 会话索引")

    def test_clean_scanned_kimi_exports_and_uploads(self):
        def fake_export(cfg, session, out_path):
            self.assertEqual(session["sessionId"], "kimi_in")
            with zipfile.ZipFile(out_path, "w") as z:
                z.writestr("state.json", json.dumps({"id": "kimi_in"}))
                z.writestr("agents/main/wire.jsonl", '{"msg":"in"}\n')
            return "测试导出"

        es.export_session = fake_export
        out = self._post("/api/clean_scanned",
                         {"kind": "kimi", "session_id": "kimi_in"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["agent_session_id"], "kimi_in")
        state = self._post("/api/upload_cleaned", {})
        self.assertEqual(state["stage"], "uploaded")
        meta = json.loads(
            (es.PENDING_DIR / "08260001_cloud-ses-1.meta.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(meta["export_method"], "测试导出")
        self.assertEqual(meta["export_file"], "08260001_cloud-ses-1.zip")


class TestDownloadCleaned(_LocalServerTestCase):
    """留档下载端点：cleaned.html 优先 / kind=md 显式 / md 兜底、
    中文文件名 RFC5987 filename*、越权/非法 base 拒绝。"""

    def setUp(self):
        self.install_tmp_dirs()
        self.start_server()

    def tearDown(self):
        self.stop_server()
        self.restore_dirs()

    def _put_cleaned(self, base: str, html: bool = True, md: bool = True):
        d = es.PENDING_DIR / base
        d.mkdir(parents=True, exist_ok=True)
        if html:
            (d / "cleaned.html").write_bytes(
                "<html><body>留档HTML</body></html>".encode("utf-8"))
        if md:
            (d / "cleaned.md").write_bytes("# 留档\n".encode("utf-8"))

    def _get(self, query: str):
        return urllib.request.urlopen(
            self.url("/api/download_cleaned" + query), timeout=5)

    def _get_error(self, query: str) -> int:
        req = urllib.request.Request(
            self.url("/api/download_cleaned" + query), method="GET")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        code = cm.exception.code
        cm.exception.close()
        return code

    @staticmethod
    def _filename_star(headers) -> str:
        cd = headers["Content-Disposition"]
        return urllib.parse.unquote(cd.split("filename*=UTF-8''", 1)[1])

    def test_download_html_preferred_with_rfc5987_filename(self):
        self.write_state(stage="uploaded", uploaded=True,
                         exam_mode="free_creation",
                         pending_base="08260001_s1", domain_tags=["llm_arch"])
        self._put_cleaned("08260001_s1")
        with self._get("?base=08260001_s1") as resp:
            body = resp.read().decode("utf-8")
            self.assertIn("text/html", resp.headers["Content-Type"])
            cd = resp.headers["Content-Disposition"]
        self.assertEqual(body, "<html><body>留档HTML</body></html>")
        self.assertIn("attachment", cd)
        self.assertIn('filename="08260001-', cd)      # ASCII 兜底名
        self.assertIn("filename*=UTF-8''", cd)         # 中文名走 RFC5987
        name = self._filename_star(resp.headers)
        self.assertEqual(name, "08260001-语言-LLM（偏架构）.html")
        self.assertNotIn("/", name)  # 领域名里的 / 已安全化为 -

    def test_download_kind_md_explicit(self):
        self.write_state(stage="uploaded", uploaded=True,
                         exam_mode="free_creation",
                         pending_base="08260001_s1", domain_tags=["llm_arch"])
        self._put_cleaned("08260001_s1")
        with self._get("?base=08260001_s1&kind=md") as resp:
            body = resp.read().decode("utf-8")
            self.assertIn("text/markdown", resp.headers["Content-Type"])
            name = self._filename_star(resp.headers)
        self.assertEqual(body, "# 留档\n")
        self.assertEqual(name, "08260001-语言-LLM（偏架构）.md")

    def test_download_falls_back_to_md_and_kind_html_missing_404(self):
        self.write_state(stage="uploaded", uploaded=True,
                         pending_base="08260001_s1", domain_tags=[])
        self._put_cleaned("08260001_s1", html=False, md=True)
        with self._get("?base=08260001_s1") as resp:  # 无 html 时退化为 md
            self.assertIn("text/markdown", resp.headers["Content-Type"])
            resp.read()
        self.assertEqual(self._get_error("?base=08260001_s1&kind=html"), 404)

    def test_download_rejects_foreign_or_invalid_base(self):
        self.write_state(stage="uploaded", uploaded=True,
                         pending_base="08260001_s1", domain_tags=["llm_arch"])
        self._put_cleaned("08260001_s1")
        self._put_cleaned("08260001_s2")  # 文件在但状态里没登记
        self.assertEqual(self._get_error("?base=08260001_s2"), 404)
        self.assertEqual(self._get_error("?base=99999999_s1"), 404)  # 异己学号
        self.assertEqual(self._get_error("?base=..%2F..%2Fetc"), 400)  # 非法字符
        self.assertEqual(self._get_error("?base="), 400)

    def test_download_after_advance_via_submissions(self):
        # 交完一个领域回领域列表后：pending_base 已归档进 submissions，仍可下载
        self.write_state(stage="logged_in", exam_mode="free_creation",
                         submissions={"memory": {
                             "session_id": "s2", "pending_base": "08260001_s2",
                             "repo_url": "https://github.com/x/b",
                             "domains": ["memory"], "uploaded_at": ""}})
        self._put_cleaned("08260001_s2", html=False, md=True)
        with self._get("?base=08260001_s2") as resp:
            self.assertIn("text/markdown", resp.headers["Content-Type"])
            name = self._filename_star(resp.headers)
            resp.read()
        self.assertEqual(name, "08260001-记忆系统.md")

    def test_download_requires_login(self):
        es.STATE_PATH.write_text(json.dumps({"stage": ""}), encoding="utf-8")
        self.assertEqual(self._get_error("?base=08260001_s1"), 404)


class TestDomainLoopPageCopy(unittest.TestCase):
    """分领域循环交卷 + 留档下载的页面要素钉死。"""

    def test_wizard_progress_and_badges(self):
        self.assertIn('id="wizard-progress"', es.PAGE)
        self.assertIn("每个领域独立交卷一次", es.PAGE)
        self.assertIn("已提交 ✓", es.PAGE)
        self.assertIn("待作答", es.PAGE)
        self.assertIn("分领域选题交卷", es.PAGE)
        # 向导页顶部有不显眼的「改为交自有项目」入口
        self.assertIn("改为交自有项目（GitHub 仓库）", es.PAGE)
        self.assertIn("enterProjectMode()", es.PAGE)

    def test_done_summary_and_download_button(self):
        self.assertIn('id="done-summary"', es.PAGE)
        self.assertIn("各领域提交汇总", es.PAGE)
        self.assertIn("下载清洗结果留档", es.PAGE)
        self.assertIn("/api/download_cleaned?base=", es.PAGE)
        self.assertIn("renderDoneSummary", es.PAGE)

    def test_my_sessions_sync_wiring(self):
        self.assertIn('"/api/my_sessions"', es.PAGE)
        self.assertIn("maybeSyncMySessions", es.PAGE)
        self.assertIn("本次领域：", es.PAGE)
        self.assertIn("每次交卷都会单独问一次", es.PAGE)


if __name__ == "__main__":
    unittest.main()

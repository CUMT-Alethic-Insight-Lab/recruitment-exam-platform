"""M11 自由创作自选题 + opencode 工作区：领域工作区文件生成/幂等、opencode.db
预注册、open_agent_workspace 端点（打桩 Popen；路径穿越拒绝；端口占用 already）、
track3 自选池单选逻辑（node 执行 PAGE 纯逻辑块）、state 持久含 workdir_abs。"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import exam_server as es
from test_exam_server import TestAuthFlowBase

_REAL_PROJECT_SQL = (
    'CREATE TABLE project (\n'
    '\t`id` text PRIMARY KEY,\n'
    '\t`worktree` text NOT NULL,\n'
    '\t`vcs` text,\n'
    '\t`name` text,\n'
    '\t`icon_url` text,\n'
    '\t`icon_color` text,\n'
    '\t`time_created` integer NOT NULL,\n'
    '\t`time_updated` integer NOT NULL,\n'
    '\t`time_initialized` integer,\n'
    '\t`sandboxes` text NOT NULL\n'
    ', `commands` text, `icon_url_override` text)\n'
)
_REAL_PDIR_SQL = (
    'CREATE TABLE "project_directory" (\n'
    '          `project_id` text NOT NULL,\n'
    '          `directory` text NOT NULL,\n'
    '          `type` text,\n'
    '          `strategy` text,\n'
    '          `time_created` integer NOT NULL,\n'
    '          CONSTRAINT `project_directory_pk` PRIMARY KEY(`project_id`, `directory`),\n'
    '          CONSTRAINT `fk_project_directory_project_id_project_id_fk`'
    ' FOREIGN KEY (`project_id`) REFERENCES `project`(`id`) ON DELETE CASCADE\n'
    '        )\n'
)


def _make_opencode_db(db_path: Path, project_sql=_REAL_PROJECT_SQL,
                      pdir_sql=_REAL_PDIR_SQL):
    con = sqlite3.connect(str(db_path))
    con.execute(project_sql)
    if pdir_sql:
        con.execute(pdir_sql)
    con.commit()
    con.close()


def _cfg(workdir=None, opencode_home=None, agent="opencode"):
    cfg = dict(es.DEFAULT_CONFIG)
    cfg["agent"] = agent
    if workdir is not None:
        cfg["agent_workdir"] = workdir
    if opencode_home is not None:
        cfg["opencode_home"] = opencode_home
    return cfg


class TestDomainWorkspaces(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workdir = root / "考试工作目录"
        # opencode_home 一定指到本测试的临时目录（无 db → 预注册直接跳过），
        # 绝不碰 %USERPROFILE%\.local\share\opencode 的真实库
        self.cfg = _cfg(str(self.workdir), str(root / "oc_home"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_files_created_with_paths(self):
        labels, dirs = es.ensure_domain_workspaces(
            self.cfg, ["llm_arch", "memory"])
        self.assertEqual(dirs, ["语言-LLM", "记忆系统"])
        for d in dirs:
            conf = self.workdir / d / "opencode.json"
            self.assertTrue(conf.exists())
            self.assertEqual(conf.read_text(encoding="utf-8").strip(),
                             '{"$schema": "https://opencode.ai/config.json"}')
            guide = self.workdir / d / "作答指引.txt"
            text = guide.read_text(encoding="utf-8")
            human = "在这里作答（人话路径）：桌面\\" + self.workdir.parent.name \
                + "\\考试工作目录\\" + d
            self.assertIn(human, text)
            self.assertIn(
                "绝对路径（可直接粘贴给 opencode）：" + (self.workdir / d).as_posix(),
                text)
            self.assertIn("请在此文件夹内用 opencode 作答，交卷时会自动找到你的会话。", text)

    def test_workspace_files_idempotent_and_no_overwrite(self):
        es.ensure_domain_workspaces(self.cfg, ["llm_arch"])
        conf = self.workdir / "语言-LLM" / "opencode.json"
        conf.write_text('{"$schema": "https://opencode.ai/config.json",'
                        '"custom": true}\n', encoding="utf-8")
        guide = self.workdir / "语言-LLM" / "作答指引.txt"
        guide.write_text("考生自己改过的内容\n", encoding="utf-8")
        # 再跑一次：目录/行数不变，已有文件不被覆盖
        labels, dirs = es.ensure_domain_workspaces(self.cfg, ["llm_arch", "cv_arch"])
        self.assertEqual(dirs, ["语言-LLM", "CV"])
        self.assertIn('"custom": true', conf.read_text(encoding="utf-8"))
        self.assertEqual(guide.read_text(encoding="utf-8"), "考生自己改过的内容\n")

    def test_legacy_ensure_domain_folders_only_makes_dirs(self):
        # 旧名保持兼容：纯建目录，不写工作区文件、不连 opencode.db
        labels, dirs = es.ensure_domain_folders(self.cfg, ["agent"])
        self.assertEqual(dirs, ["Agent 编排与框架"])
        self.assertFalse((self.workdir / "Agent 编排与框架" / "opencode.json").exists())
        # 幂等 & 未知 key 回退
        _, dirs2 = es.ensure_domain_folders(self.cfg, ["agent", "blah"])
        self.assertEqual(dirs2, ["Agent 编排与框架", "blah"])

    def test_empty_domains_no_workspaces(self):
        labels, dirs = es.ensure_domain_workspaces(self.cfg, [])
        self.assertEqual((labels, dirs), ([], []))


class TestOpencodePreregister(unittest.TestCase):
    """向 opencode.db 预注册领域工作区；异常全部只记日志跳过。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workdir = root / "考试工作目录"
        self.oc_home = root / "oc_home"
        (self.workdir / "语言-LLM").mkdir(parents=True)
        self.cfg = _cfg(str(self.workdir), str(self.oc_home))

    def tearDown(self):
        self.tmp.cleanup()

    def test_registers_project_and_directory_forward_slashes(self):
        db = self.oc_home / "opencode.db"
        self.oc_home.mkdir(parents=True)
        _make_opencode_db(db)
        es.ensure_domain_workspaces(self.cfg, ["llm_arch"])
        con = sqlite3.connect(str(db))
        projects = con.execute(
            "SELECT id, worktree, name, time_created FROM project").fetchall()
        dirs = con.execute(
            "SELECT project_id, directory FROM project_directory").fetchall()
        con.close()
        self.assertEqual(len(projects), 1)
        pid, worktree, name, tc = projects[0]
        self.assertTrue(re.fullmatch(r"[0-9a-f]{40}", pid), pid)  # 按现有行风格
        self.assertEqual(worktree, (self.workdir / "语言-LLM").as_posix())
        self.assertEqual(name, "语言-LLM")
        self.assertGreater(tc, 0)
        self.assertEqual(dirs, [(pid, (self.workdir / "语言-LLM").as_posix())])

    def test_registration_idempotent(self):
        db = self.oc_home / "opencode.db"
        self.oc_home.mkdir(parents=True)
        _make_opencode_db(db)
        es.ensure_domain_workspaces(self.cfg, ["llm_arch"])
        es.ensure_domain_workspaces(self.cfg, ["llm_arch"])
        con = sqlite3.connect(str(db))
        n_proj = con.execute("SELECT COUNT(*) FROM project").fetchone()[0]
        n_dir = con.execute("SELECT COUNT(*) FROM project_directory").fetchone()[0]
        con.close()
        self.assertEqual(n_proj, 1)
        self.assertEqual(n_dir, 1)

    def test_missing_db_skips(self):
        es.ensure_domain_workspaces(self.cfg, ["llm_arch"])  # oc_home 不存在
        self.assertTrue((self.workdir / "语言-LLM" / "opencode.json").exists())

    def test_corrupt_db_skips(self):
        self.oc_home.mkdir(parents=True)
        (self.oc_home / "opencode.db").write_bytes(b"not a sqlite db at all")
        es.ensure_domain_workspaces(self.cfg, ["llm_arch"])  # 不抛异常

    def test_schema_mismatch_skips(self):
        self.oc_home.mkdir(parents=True)
        db = self.oc_home / "opencode.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE project (id text PRIMARY KEY, name text)")
        con.commit()
        con.close()
        es.ensure_domain_workspaces(self.cfg, ["llm_arch"])  # 无 worktree/directory → 跳过


class _OpenWsServerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.workdir = root / "考试工作目录"
        self._orig_state = es.STATE_PATH
        self._orig_pending = es.PENDING_DIR
        es.STATE_PATH = root / "state.json"
        es.PENDING_DIR = root / "pending_upload"
        es.PENDING_DIR.mkdir(parents=True)
        es.STATE_PATH.write_text(
            json.dumps({"stage": "logged_in", "student_id": "08260001",
                        "domains": ["语言/LLM（偏架构）"],
                        "domain_dirs": ["语言-LLM"],
                        "domain_keys": ["llm_arch"]}, ensure_ascii=False),
            encoding="utf-8")
        self.httpd = es.ThreadingHTTPServer(("127.0.0.1", 0), es.ExamHandler)
        self.httpd.cfg = _cfg(str(self.workdir))
        self.port = self.httpd.server_address[1]
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def restore_dirs(self):
        es.STATE_PATH = self._orig_state
        es.PENDING_DIR = self._orig_pending

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.restore_dirs()
        self.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def post(self, path, body):
        import urllib.request
        req = urllib.request.Request(
            self.url(path), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")


class TestOpenAgentWorkspaceEndpoint(_OpenWsServerBase):

    def test_open_launches_cmd_in_domain_dir(self):
        (self.workdir / "语言-LLM").mkdir(parents=True)
        with mock.patch.object(es.subprocess, "Popen") as popen, \
                mock.patch.object(es, "_port_in_use", return_value=False):
            status, data = self.post("/api/open_agent_workspace",
                                     {"domain_dir": "语言-LLM"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertFalse(data["already"])
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["cmd", "/k", es.agent_web_cmd(self.httpd.cfg)])
        self.assertEqual(kwargs["cwd"], str((self.workdir / "语言-LLM").resolve()))
        self.assertEqual(kwargs["creationflags"],
                         getattr(es.subprocess, "CREATE_NEW_CONSOLE", 0))

    def test_open_already_when_port_occupied(self):
        (self.workdir / "语言-LLM").mkdir(parents=True)
        with mock.patch.object(es.subprocess, "Popen") as popen, \
                mock.patch.object(es, "_port_in_use", return_value=True):
            status, data = self.post("/api/open_agent_workspace",
                                     {"domain_dir": "语言-LLM"})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["already"])
        self.assertIn("已经打开", data["hint"])
        popen.assert_not_called()

    def test_traversal_rejected(self):
        for bad in ("..", "../语言-LLM", "..\\语言-LLM", "语言-LLM/evil",
                    "C:/语言-LLM", "语言-LLM:x", ""):
            with mock.patch.object(es.subprocess, "Popen") as popen:
                try:
                    status, data = self.post("/api/open_agent_workspace",
                                             {"domain_dir": bad})
                except Exception:
                    self.fail(f"domain_dir={bad!r} 应返回 400")
            self.assertEqual(status, 400, bad)
            popen.assert_not_called()

    def test_nonexistent_domain_dir_400(self):
        with mock.patch.object(es.subprocess, "Popen") as popen:
            status, data = self.post("/api/open_agent_workspace",
                                     {"domain_dir": "不存在的领域"})
        self.assertEqual(status, 400)
        popen.assert_not_called()

    def test_requires_login(self):
        import threading as _t
        es.STATE_PATH.write_text(json.dumps({"stage": ""}), encoding="utf-8")
        with mock.patch.object(es.subprocess, "Popen") as popen:
            status, _ = self.post("/api/open_agent_workspace",
                                  {"domain_dir": "语言-LLM"})
        self.assertEqual(status, 400)
        popen.assert_not_called()


class TestPoolLogic(unittest.TestCase):
    """node 执行 PAGE 内纯逻辑块：同域互斥/跨域共存、考生领域过滤、组内排序、
    track3 校验（0 题 / 同域多题白话错误）。"""

    @classmethod
    def setUpClass(cls):
        m = re.search(r"/\* __POOL_LOGIC_BEGIN__ \*/(.*?)/\* __POOL_LOGIC_END__ \*/",
                      es.PAGE, re.S)
        assert m, "PAGE 内缺少 __POOL_LOGIC__ 标记块"
        cls.logic = m.group(1)

    def test_pool_same_domain_exclusive_cross_domain_ok(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        js = "const POOL_MAX = 3;\n" + self.logic + """
const assert = require("assert");
const dom = {"llm1": "llm_arch", "llm2": "llm_arch", "mem1": "memory", "ag1": "agent"};
// 同域互斥：勾新题自动取消同域旧题，跨域不受影响
assert.deepStrictEqual(poolToggleIds(["llm1"], "llm2", "llm_arch", dom), ["llm2"]);
assert.deepStrictEqual(
  poolToggleIds(["llm1", "mem1"], "llm2", "llm_arch", dom), ["mem1", "llm2"]);
// 跨域可共存
assert.deepStrictEqual(
  poolToggleIds(["llm1"], "mem1", "memory", dom), ["llm1", "mem1"]);
// 再点同一题取消自身
assert.deepStrictEqual(poolToggleIds(["llm1", "mem1"], "llm1", "llm_arch", dom),
                       ["mem1"]);
// 已有 3 题时再加第 4 题（其它域）不再增加
assert.deepStrictEqual(
  poolToggleIds(["llm1", "mem1", "ag1"], "cv1", "cv_arch", dom),
  ["llm1", "mem1", "ag1"]);
console.log("POOL_TOGGLE_OK");
"""
        self._run_node(js, "POOL_TOGGLE_OK")

    def test_pool_filter_domains_and_sort_track(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        js = "const POOL_MAX = 3;\n" + self.logic + """
const assert = require("assert");
const qs = [
  {id: "a", domain: "llm_arch", track: "track3"},
  {id: "b", domain: "llm_arch", track: "track1"},
  {id: "c", domain: "memory",   track: "track2"},
  {id: "d", domain: "infra",    track: "track1"},
  {id: "e", domain: "llm_arch", track: "zzz"},
];
// 只显示考生领域（llm_arch、memory），infra 的 d 不出现
const filtered = poolFilterByDomains(qs, ["llm_arch", "memory"]);
assert.deepStrictEqual(filtered.map(q => q.id).sort(), ["a", "b", "c", "e"]);
// 空 keys 不过滤
assert.strictEqual(poolFilterByDomains(qs, []).length, 5);
// 组内排序：理论→实操→自由发挥，未知排最后（llm_arch 组内 b=track1, a=track3, e=zzz）
const inGroup = poolSortByTrack(filtered).filter(q => q.domain === "llm_arch");
assert.deepStrictEqual(inGroup.map(q => q.id), ["b", "a", "e"]);
// 全量排序首尾
const all = poolSortByTrack(qs);
assert.strictEqual(all[0].track, "track1");
assert.strictEqual(all[all.length - 1].track, "zzz");
console.log("POOL_FILTER_OK");
"""
        self._run_node(js, "POOL_FILTER_OK")

    def test_pool_track3_validation(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        js = "const POOL_MAX = 3;\n" + self.logic + """
const assert = require("assert");
const dom = {"llm1": "llm_arch", "llm2": "llm_arch", "mem1": "memory"};
assert.strictEqual(poolValidateTrack3([], dom), "请至少选择 1 道题");
assert.strictEqual(poolValidateTrack3(["llm1", "llm2"], dom),
                   "同一领域只能选择一道题作答");
assert.strictEqual(poolValidateTrack3(["llm1", "mem1"], dom), "");
// 标准模式校验保持原样
assert.strictEqual(poolValidateStandard([]), "请先勾选 1~3 道题");
assert.strictEqual(poolValidateStandard(["llm1"]), "");
console.log("POOL_VALIDATE_OK");
"""
        self._run_node(js, "POOL_VALIDATE_OK")

    def _run_node(self, js, marker):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(js)
            name = f.name
        try:
            r = subprocess.run(["node", name], capture_output=True,
                               encoding="utf-8", errors="replace")
        finally:
            os.unlink(name)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(marker, r.stdout)

    def test_page_script_syntax(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        m = re.search(r"<script>(.*?)</script>", es.PAGE, re.S)
        self.assertIsNotNone(m)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(m.group(1))
            name = f.name
        try:
            r = subprocess.run(["node", "--check", name], capture_output=True,
                               encoding="utf-8", errors="replace")
        finally:
            os.unlink(name)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestPoolPageCopy(unittest.TestCase):
    """track3 视图页面要素：按领域分组 + 三类型徽标 + 同域互斥 + 隐藏轨迹筛选。"""

    def test_track3_view_copy_present(self):
        self.assertIn("renderPoolTrack3", es.PAGE)
        self.assertIn('领域：" + (DOMAIN_LABELS[domain] || domain)', es.PAGE)
        self.assertIn('badge.textContent = trackTagLabel(q.track)', es.PAGE)
        self.assertIn('onClick="enrollSelfSelectedTrack3()"'.replace("onClick", "onclick"), es.PAGE)
        self.assertIn('bar.innerHTML = \'<button onclick="enrollSelfSelectedTrack3()">确认自选开考</button> \'', es.PAGE)

    def test_track3_view_filters_domains_and_sorts_track(self):
        # 池子只显示考生领域内的题目，组内按 理论→实操→自由发挥 排序
        self.assertIn("poolFilterByDomains(questions, state.domain_keys || [])", es.PAGE)
        self.assertIn("poolSortByTrack(", es.PAGE)
        self.assertIn('cb.dataset.domain = q.domain || "";', es.PAGE)
        self.assertIn("ev.target.dataset.domain, poolDomById);", es.PAGE)

    def test_track3_view_has_no_track_filter_select(self):
        # track3 分支渲染的确认栏不再构造 pool-track select
        seg = es.PAGE[es.PAGE.index("enrollSelfSelectedTrack3()"):]
        bar = seg[seg.index("bar.innerHTML"):seg.index("updatePoolCount();\n}\n")]
        self.assertNotIn("pool-track", bar)

    def test_type_badges_and_errors(self):
        self.assertIn("理论", es.PAGE)
        self.assertIn("实操", es.PAGE)
        self.assertIn("自由发挥", es.PAGE)
        self.assertIn("已选", es.PAGE)
        self.assertIn("请至少选择 1 道题", es.PAGE)
        self.assertIn("同一领域只能选择一道题作答", es.PAGE)
        self.assertIn("同一领域最多勾 1 道题", es.PAGE)
        self.assertIn('.sel-tag', es.PAGE)
        self.assertIn(".pool-badge", es.PAGE)

    def test_workspace_guidance_copy(self):
        self.assertIn("在这里作答：", es.PAGE)
        self.assertIn("绝对路径（可直接粘给 opencode）：", es.PAGE)
        self.assertIn("已复制", es.PAGE)
        self.assertIn("打开此领域的 ", es.PAGE)
        self.assertIn("在此工作区打开 ", es.PAGE)
        self.assertIn("本题为 ", es.PAGE)
        self.assertIn("/api/open_agent_workspace", es.PAGE)
        # 每域一题：题面页把涉及领域的每个工作区都列出来（去重）
        self.assertIn("const seen = {};", es.PAGE)
        self.assertIn("blocks.push({domain: domain, dir: dir,", es.PAGE)
        self.assertIn('打开「" + b.label + "」工作区', es.PAGE)


class TestLoginWorkdirState(TestAuthFlowBase):
    """登录后 state 持久化 workdir_abs / workdir_human / domain_keys / agent_label。"""

    def test_login_stores_workdir_paths_and_persists(self):
        self.httpd.cfg["agent_workdir"] = str(self.root / "考试工作目录")
        state = self.post_json("/api/login", {
            "student_id": "08260001", "password": "pw"})
        self.assertEqual(state["workdir_abs"], (self.root / "考试工作目录").as_posix())
        self.assertEqual(state["workdir_human"],
                         "桌面\\" + self.root.name + "\\考试工作目录")
        self.assertEqual(state["domain_keys"], [])
        self.assertEqual(state["agent_label"], "Kimi Code")
        persisted = self.get_json("/api/state")
        self.assertEqual(persisted["workdir_abs"], state["workdir_abs"])
        self.assertEqual(persisted["workdir_human"], state["workdir_human"])
        self.assertEqual(persisted["agent_label"], "Kimi Code")
        self.assertTrue((self.root / "考试工作目录").is_dir())


if __name__ == "__main__":
    unittest.main()

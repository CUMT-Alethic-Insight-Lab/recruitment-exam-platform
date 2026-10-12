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
    """node 执行 PAGE 内纯逻辑块：题库过滤/排序、standard 校验、
    M12 两步向导（选领域→选题/自由发挥）纯逻辑。"""

    @classmethod
    def setUpClass(cls):
        m = re.search(r"/\* __POOL_LOGIC_BEGIN__ \*/(.*?)/\* __POOL_LOGIC_END__ \*/",
                      es.PAGE, re.S)
        assert m, "PAGE 内缺少 __POOL_LOGIC__ 标记块"
        cls.logic = m.group(1)

    def test_wizard_questions_for_domain_filters_and_sorts(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        js = "const POOL_MAX = 3;\n" + self.logic + """
const assert = require("assert");
const qs = [
  {id: "a", domain: "llm_arch", track: "track3"},
  {id: "b", domain: "llm_arch", track: "track1"},
  {id: "c", domain: "memory",   track: "track2"},
  {id: "d", domain: "infra",    track: "track1"},
];
// 只取所选领域，并按 理论→实操→自由发挥 排序
assert.deepStrictEqual(
  wizardQuestionsForDomain(qs, "llm_arch").map(q => q.id), ["b", "a"]);
assert.deepStrictEqual(
  wizardQuestionsForDomain(qs, "memory").map(q => q.id), ["c"]);
// 未选领域 / 无题领域都为空
assert.deepStrictEqual(wizardQuestionsForDomain(qs, ""), []);
assert.deepStrictEqual(wizardQuestionsForDomain(qs, "agent"), []);
console.log("WIZARD_FILTER_OK");
"""
        self._run_node(js, "WIZARD_FILTER_OK")

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

    def test_wizard_validate(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        js = "const POOL_MAX = 3;\n" + self.logic + """
const assert = require("assert");
// 两步向导校验：先选领域，再选题或自由发挥
assert.strictEqual(wizardValidate("", ""), "请先选择领域");
assert.strictEqual(wizardValidate("llm_arch", ""), "请选择一道题，或选择「自由发挥」");
assert.strictEqual(wizardValidate("llm_arch", "q-1"), "");
assert.strictEqual(wizardValidate("llm_arch", "__free__"), "");
// 标准模式校验保持原样
assert.strictEqual(poolValidateStandard([]), "请先勾选 1~3 道题");
assert.strictEqual(poolValidateStandard(["llm1"]), "");
console.log("WIZARD_VALIDATE_OK");
"""
        self._run_node(js, "WIZARD_VALIDATE_OK")

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


def _extract_js_functions(page: str, *names: str) -> str:
    """从 PAGE 里按名抽出函数定义源码（朴素花括号配对；所抽函数体内
    字符串/注释不含 {}，失配时 assert 会直接炸出来）。"""
    out = []
    for name in names:
        m = re.search(rf"(?:async\s+)?function\s+{name}\s*\(", page)
        assert m, f"PAGE 中未找到函数 {name}"
        brace = page.index("{", m.end() - 1)
        depth, i = 0, brace
        while True:
            if page[i] == "{":
                depth += 1
            elif page[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(page[m.start():i + 1])
    return "\n".join(out)


class TestPoolGroupingDom(unittest.TestCase):
    """node + 极简 DOM 桩执行真实 renderQuestionPool/onPoolChange/enrollSelfSelected：
    自己领域 checkbox 可勾选、其他领域 disabled 置灰、越界勾选打回、
    自选提交不带 track。"""

    @classmethod
    def setUpClass(cls):
        m = re.search(r"/\* __POOL_LOGIC_BEGIN__ \*/(.*?)/\* __POOL_LOGIC_END__ \*/",
                      es.PAGE, re.S)
        assert m, "PAGE 内缺少 __POOL_LOGIC__ 标记块"
        cls.logic = m.group(1)
        cls.funcs = _extract_js_functions(
            es.PAGE, "renderQuestionPool", "appendPoolDomainGroups",
            "poolQuestionItem", "onPoolChange", "checkedPoolBoxes",
            "updatePoolCount", "enrollSelfSelected")

    def test_grouping_and_enroll_contract(self):
        if not shutil.which("node"):
            self.skipTest("node 不可用")
        js = """
const assert = require("assert");
const POOL_MAX = 3;
const DOMAIN_LABELS = {llm_arch: "语言/LLM（偏架构）", memory: "记忆系统", rl: "RL"};
let state = {domain_keys: ["llm_arch", "memory"]};
let __alerted = "", __errors = "", __enrollArgs = undefined;
function alert(m) { __alerted = m; }
function showErr(m) { __errors = m; }
function enroll(track, ids) { __enrollArgs = [track, ids]; }
function makeEl(tag) {
  const el = {tagName: tag, children: [], style: {}, dataset: {},
              listeners: {}, className: "", innerHTML: "",
              type: "", value: "", checked: false, disabled: false,
              classList: {add(){}, remove(){}, toggle(){}},
              appendChild(c) { el.children.push(c); return c; },
              addEventListener(t, f) { el.listeners[t] = f; }};
  let text = "";
  Object.defineProperty(el, "textContent", {
    get() { return text; },
    set(v) { text = v; if (v === "") el.children = []; },
  });
  return el;
}
const __poolRoot = makeEl("div");
const document = {
  createElement: (t) => makeEl(t),
  getElementById: (id) => id === "question-pool" ? __poolRoot : null,
  querySelectorAll(sel) {
    assert.strictEqual(sel, "#question-pool input[type=checkbox]:checked");
    const out = [];
    (function walk(n) {
      n.children.forEach(c => {
        if (c.type === "checkbox" && c.checked) out.push(c);
        walk(c);
      });
    })(__poolRoot);
    return out;
  },
};
""" + self.logic + self.funcs + """
(async () => {
  const questions = [
    {id: "q1", domain: "llm_arch", track: "track1", title: "LLM题1", body: "B1"},
    {id: "q2", domain: "llm_arch", track: "track2", title: "LLM题2", body: "B2"},
    {id: "q3", domain: "memory",   track: "track1", title: "记忆题", body: "B3"},
    {id: "q4", domain: "rl",       track: "track3", title: "RL题",  body: "B4"},
  ];
  renderQuestionPool(questions);
  // 顺序收集分组头与题目行
  const heads = [], items = [];
  (function walk(n) {
    n.children.forEach(c => {
      if (c.className === "pool-track-head") heads.push(c);
      if (c.className === "pool-item") items.push(c);
      walk(c);
    });
  })(__poolRoot);
  assert.deepStrictEqual(heads.map(h => h.textContent),
    ["你的领域（可作答）", "其他领域（仅浏览，不可作答）"]);
  assert.strictEqual(items.length, 4);
  const cbOf = it => it.children[0].children[0];  // item > label > checkbox
  const byId = {};
  items.forEach(it => { byId[cbOf(it).value] = it; });
  // 自己领域（llm_arch/memory）：checkbox 可用、挂了 change 监听
  for (const id of ["q1", "q2", "q3"]) {
    assert.strictEqual(cbOf(byId[id]).disabled, false, id);
    assert.strictEqual(cbOf(byId[id]).dataset.domain !== "", true);
    assert.strictEqual(typeof cbOf(byId[id]).listeners.change, "function", id);
    assert.notStrictEqual(byId[id].style.opacity, "0.55", id);
  }
  // 其他领域（rl）：disabled + 整行置灰 + 无 change 监听
  assert.strictEqual(cbOf(byId.q4).disabled, true);
  assert.strictEqual(byId.q4.style.opacity, "0.55");
  assert.strictEqual(cbOf(byId.q4).listeners.change, undefined);
  // 领域小节头用中文标签
  const domainHeads = [];
  (function walk2(n) {
    n.children.forEach(c => {
      if (c.className === "pool-domain-head") domainHeads.push(c.textContent);
      walk2(c);
    });
  })(__poolRoot);
  assert(domainHeads.some(t => t.indexOf("语言/LLM（偏架构）") !== -1));
  assert(domainHeads.some(t => t.indexOf("RL") !== -1));
  // 勾选自己领域的题：onPoolChange 放行
  cbOf(byId.q1).checked = true;
  onPoolChange({target: cbOf(byId.q1)});
  assert.strictEqual(cbOf(byId.q1).checked, true);
  // 防御：其他领域被强制勾选 → 打回 + 提示
  const foreign = cbOf(byId.q4);
  foreign.disabled = false; foreign.checked = true;
  onPoolChange({target: foreign});
  assert.strictEqual(foreign.checked, false);
  assert(__alerted.indexOf("只能选问卷里报过的领域") !== -1);
  // POOL_MAX：勾满 3 题后第 4 个被拒（须挂进树里才会被计数，与真实 DOM 一致）
  cbOf(byId.q2).checked = true; cbOf(byId.q3).checked = true;
  const extra = makeEl("input");
  extra.type = "checkbox"; extra.checked = true; extra.dataset.domain = "llm_arch";
  extra.value = "qX";
  __poolRoot.appendChild(extra);
  onPoolChange({target: extra});
  assert.strictEqual(extra.checked, false);
  assert(__alerted.indexOf("最多自选") !== -1);
  // enrollSelfSelected：不带 track，只传 question_ids
  await enrollSelfSelected();
  assert(__enrollArgs !== undefined);
  assert.strictEqual(__enrollArgs[0], null);
  assert.deepStrictEqual(__enrollArgs[1], ["q1", "q2", "q3"]);
  // 越界防御：篡改一题为其他领域 → 拦截不提交
  __enrollArgs = undefined; __errors = "";
  cbOf(byId.q3).dataset.domain = "rl";
  await enrollSelfSelected();
  assert.strictEqual(__enrollArgs, undefined);
  assert(__errors.indexOf("只能选问卷里报过的领域") !== -1);
  console.log("POOL_DOM_OK");
})().catch(e => { console.error(e); process.exit(1); });
"""
        TestPoolLogic._run_node(self, js, "POOL_DOM_OK")

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
    """M12 页面要素钉死：free_creation 两步向导、交卷四步（手动导出→清洗→预览→上传）、
    own_project 直提指引。"""

    def test_wizard_dom_present(self):
        self.assertIn('id="stage-wizard"', es.PAGE)
        self.assertIn('id="wizard-domains"', es.PAGE)
        self.assertIn('id="wizard-step2"', es.PAGE)
        self.assertIn('id="wizard-questions"', es.PAGE)
        self.assertIn("第 1 小步：选择一个领域", es.PAGE)
        self.assertIn("第 2 小步：选一道题，或者直接自由发挥", es.PAGE)
        self.assertIn("← 重选领域", es.PAGE)
        self.assertIn("确认开考", es.PAGE)

    def test_wizard_logic_flow(self):
        # 选领域 → 拉题库过滤该领域 → 单选 radio（题目或自由发挥）→ 确认 enroll
        self.assertIn("wizardQuestionsForDomain(questions, k)", es.PAGE)
        self.assertIn('rb.type = "radio";', es.PAGE)
        self.assertIn('badge.textContent = trackTagLabel(q.track);', es.PAGE)
        self.assertIn('const FREE_PICK = "__free__";', es.PAGE)
        self.assertIn("自由发挥（不选题，在这个领域里自定题目创作）", es.PAGE)
        self.assertIn('enroll("track3", null, wizardDomain);', es.PAGE)
        self.assertIn('enroll("track3", [wizardPick]);', es.PAGE)
        self.assertIn("请先选择领域", es.PAGE)
        self.assertIn("请选择一道题，或选择「自由发挥」", es.PAGE)

    def test_standard_track_view_kept(self):
        # 开考二选一：做题（题库自选）/ 交自有项目；不再渲染 track 按钮与随机抽题入口
        self.assertIn('id="stage-track"', es.PAGE)
        self.assertIn("选择作答方式", es.PAGE)
        self.assertIn("做题：从题库选题作答", es.PAGE)
        self.assertIn("交自有项目：GitHub 仓库 + 可选附件", es.PAGE)
        self.assertIn("enterPoolMode()", es.PAGE)
        self.assertIn("enterProjectMode()", es.PAGE)
        self.assertIn("enrollSelfSelected()", es.PAGE)
        self.assertNotIn("renderTracks", es.PAGE)
        self.assertNotIn("浏览题库自选题", es.PAGE)
        self.assertNotIn("随机抽题", es.PAGE)
        self.assertNotIn("renderPoolTrack3", es.PAGE)

    def test_pool_grouping_copy(self):
        # 题库自选按「你的领域 / 其他领域」分组；其他领域禁用勾选 + 置灰
        self.assertIn("你的领域（可作答）", es.PAGE)
        self.assertIn("其他领域（仅浏览，不可作答）", es.PAGE)
        self.assertIn("poolSplitByDomains", es.PAGE)
        self.assertIn("cb.disabled = true", es.PAGE)
        self.assertIn('item.style.opacity = "0.55"', es.PAGE)
        self.assertIn("cb.dataset.domain = q.domain", es.PAGE)
        self.assertIn("只能选问卷里报过的领域", es.PAGE)
        # 不再按 track 分组/过滤：无轨迹下拉、无 dataset.track
        self.assertNotIn('id="pool-track"', es.PAGE)
        self.assertNotIn("轨迹：<select", es.PAGE)
        self.assertNotIn("dataset.track", es.PAGE)

    def test_exam_mode_only_recommends_default_view(self):
        # exam_mode 只做默认落页推荐（不硬拦）：preferredView 优先，
        # own_project 考生也能从自有项目页「改为做题」进题库自选页
        self.assertIn("preferredView ||", es.PAGE)
        self.assertIn('state.exam_mode === "own_project" ? "project"', es.PAGE)
        self.assertIn("backToQuestionMode", es.PAGE)
        self.assertIn("改为做题（从题库选题作答）", es.PAGE)

    def test_finish_four_steps_copy(self):
        # 手动导出指引卡（不默认扫库，含具体导出步骤）
        self.assertIn("不会自动翻找你电脑里的历史会话", es.PAGE)
        self.assertIn("opencode session list", es.PAGE)
        self.assertIn("opencode export 会话ID", es.PAGE)
        self.assertIn("kimi export", es.PAGE)
        # 逐题提交槽：每题一格独立清洗、独立上传（不合并）
        self.assertIn('id="slot-list"', es.PAGE)
        self.assertIn("第 2 小步：逐题清洗", es.PAGE)
        self.assertIn("第 3 小步：确认没有不该上传的内容后，点上传", es.PAGE)
        self.assertIn('id="preview-frame"', es.PAGE)
        self.assertIn('id="preview-text"', es.PAGE)
        # 乱序清洗不重建 DOM：签名只看题目结构，状态原地更新（保住文件选择）
        self.assertIn("updateSlotStatus", es.PAGE)
        self.assertIn('slot-cleanbtn-', es.PAGE)
        self.assertNotIn("slots).sort().map(", es.PAGE)  # 旧签名含槽位 base 会触发重建
        # 自动发现降级为每题可选按钮，且明示只找考试工作区
        self.assertIn("在本题工作区帮我找", es.PAGE)
        self.assertIn("只找考试工作区内的，不碰其它目录", es.PAGE)
        # /export 陷阱提示：md 导出不能交卷；kimi 的 zip 导出入口
        self.assertIn("/export-debug-zip", es.PAGE)
        self.assertIn("Markdown 文档", es.PAGE)
        # 仓库链接输入框防轮询覆盖（repoDirty 脏标记）
        self.assertIn("repoDirty", es.PAGE)

    def test_finish_endpoints_wired(self):
        self.assertIn('"/api/clean_export"', es.PAGE)
        self.assertIn('"/api/clean_scanned"', es.PAGE)
        self.assertIn('"/api/upload_cleaned"', es.PAGE)
        self.assertIn('"/api/scan_sessions"', es.PAGE)
        self.assertIn('"/api/preview?base="', es.PAGE)

    def test_own_project_direct_submit_copy(self):
        self.assertIn('id="stage-ownproject"', es.PAGE)
        self.assertIn('id="project-repo-url"', es.PAGE)
        self.assertIn('id="project-attachment-file"', es.PAGE)
        self.assertIn('id="project-submit-btn"', es.PAGE)
        self.assertIn("提交自有项目，完成交卷", es.PAGE)
        self.assertIn("submitOwnProject()", es.PAGE)
        self.assertIn('"/api/submit_project"', es.PAGE)
        self.assertIn("无需在本机选题作答", es.PAGE)
        # 账号页兜底入口保留
        self.assertIn("openAccountPage", es.PAGE)
        self.assertIn('"/api/open_account_page"', es.PAGE)

    def test_free_creation_exam_stage_copy(self):
        # 自由发挥开考：引导语 + 工作区卡，无题面正文
        self.assertIn("自由发挥（不选题）· 领域：", es.PAGE)
        self.assertIn("产出将通过本次作答的会话轨迹评估", es.PAGE)
        self.assertIn('id="exam-title"', es.PAGE)

    def test_workspace_guidance_copy(self):
        self.assertIn("在这里作答：", es.PAGE)
        self.assertIn("绝对路径（可直接粘给 opencode）：", es.PAGE)
        self.assertIn("已复制", es.PAGE)
        self.assertIn("打开此领域的 ", es.PAGE)
        self.assertIn("在此工作区打开 ", es.PAGE)
        self.assertIn("本题为 ", es.PAGE)
        self.assertIn("/api/open_agent_workspace", es.PAGE)
        # 工作区卡把涉及领域的每个工作区都列出来（去重）
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

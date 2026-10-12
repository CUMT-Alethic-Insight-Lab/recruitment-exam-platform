"""cleaner 纯函数单测（stdlib unittest，无第三方依赖）。

用代码合成一个 mini 会话 zip，覆盖全部 wire 行类型 + 一个 subagent +
一个 blob 引用，断言清洗规则与输出统计。"""

import json
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import cleaner

T0 = 1786061038587  # epoch 毫秒
BLOB_HASH = "b0326d05ac9a36401d771b5646fc7b55c62ed2553362da7f53ba52bfa9565411"

NOISE_TYPES = [
    "llm.request", "usage.record", "llm.tools_snapshot",
    "mcp.tools_discovered", "profile.bind", "config.update",
    "permission.set_mode", "runtime.set_binding",
    "full_compaction.begin", "full_compaction.complete",
]

LONG_COMMAND = "python -m pytest tests -q " + "x" * 300
READ_OUTPUT = "line1\nline2\nline3"


def _line(**kw):
    return json.dumps(kw, ensure_ascii=False)


def _loop(ev, t=T0 + 100):
    return _line(type="context.append_loop_event", event=ev, time=t)


def _make_main_wire():
    lines = [_line(type="metadata", protocol_version="1.5", created_at=T0)]
    # 全部噪音类型，塞大 payload 撑体积
    for i, t in enumerate(NOISE_TYPES):
        lines.append(_line(type=t, payload="noise" * 200, time=T0 + i))
    lines += [
        _line(type="turn.prompt", time=T0 + 10, origin={"kind": "user"},
              input=[{"type": "text", "text": "请实现 fizzbuzz"}]),
        _line(type="turn.prompt", time=T0 + 11, origin={"kind": "user"},
              input=[{"type": "text",
                      "text": "<system-reminder>系统注入，不算真实输入</system-reminder>"}]),
        _line(type="turn.prompt", time=T0 + 12,
              origin={"kind": "task", "taskId": "bash-1", "status": "completed"},
              input=[{"type": "text", "text": '<notification id="task:bash-1:x">done</notification>'}]),
        _line(type="context.append_message", time=T0 + 13,
              message={"role": "user", "origin": {"kind": "user"},
                       "content": [{"type": "text", "text": "请实现 fizzbuzz"}]}),
        _loop({"type": "step.begin", "step": 1}),
        _loop({"type": "content.part",
               "part": {"type": "think",
                        "think": "我认为应该先写测试，再写实现，思路是…" * 20}}),
        _loop({"type": "content.part", "part": {"type": "text", "text": "我先写个测试用例。"}}),
        _loop({"type": "step.end", "step": 1}),
        # Read：正常字符串输出
        _loop({"type": "tool.call", "toolCallId": "r1", "name": "Read",
               "args": {"path": "src/a.py"}}),
        _loop({"type": "tool.result", "toolCallId": "r1",
               "result": {"output": READ_OUTPUT}}),
        # Bash：超长命令 + 出错
        _loop({"type": "tool.call", "toolCallId": "r2", "name": "Bash",
               "args": {"command": LONG_COMMAND}}),
        _loop({"type": "tool.result", "toolCallId": "r2",
               "result": {"output": "boom", "isError": True}}),
        # Grep：数组输出且含 image_url（blob 引用）
        _loop({"type": "tool.call", "toolCallId": "r3", "name": "Grep",
               "args": {"pattern": "TODO"}}),
        _loop({"type": "tool.result", "toolCallId": "r3",
               "result": {"output": [
                   {"type": "text", "text": "<image path=\"x.png\">"},
                   {"type": "image_url",
                    "imageUrl": {"url": f"blobref:image/png;{BLOB_HASH}"}},
                   {"type": "text", "text": "</image>"}]}}),
        # Agent：关联 subagent 标题
        _loop({"type": "tool.call", "toolCallId": "r4", "name": "Agent",
               "args": {"description": "子代理任务A",
                        "prompt": "完整任务书" * 100}}),
        _loop({"type": "tool.result", "toolCallId": "r4",
               "result": {"output": "agent_id: agent-0\n"
                                    "actual_subagent_type: coder\nstatus: completed"}}),
        # todo 只保留最后一次
        _line(type="tools.update_store", key="todo", time=T0 + 20,
              value=[{"title": "写测试", "status": "in_progress"},
                     {"title": "写实现", "status": "pending"}]),
        _line(type="tools.update_store", key="todo", time=T0 + 21,
              value=[{"title": "写测试", "status": "done"},
                     {"title": "写实现", "status": "pending"}]),
        _line(type="tools.update_store", key="notes", time=T0 + 22,
              value=[{"note": "非 todo，丢弃"}]),
        _line(type="task.terminated", time=T0 + 30,
              info={"taskId": "bash-1", "description": "长构建",
                    "status": "completed", "exitCode": 0}),
        _line(type="context.apply_compaction", time=T0 + 40,
              summary="这是一次压缩摘要"),
        _line(type="turn.ended", turnId=0, reason="completed", time=T0 + 50),
        _line(type="task.started", time=T0 + 51,
              info={"taskId": "bash-1", "description": "长构建", "status": "running"}),
        _line(type="future.unknown", time=T0 + 52),  # 未识别类型 → 丢弃
    ]
    return "\n".join(lines) + "\n"


def _make_agent0_wire():
    lines = [
        _line(type="metadata", protocol_version="1.5", created_at=T0 + 60),
        _line(type="turn.prompt", time=T0 + 61, origin={"kind": "user"},
              input=[{"type": "text", "text": "帮我读文件"}]),
        _loop({"type": "content.part",
               "part": {"type": "think", "think": "子代理思考链，不应出现"}}, t=T0 + 62),
        _loop({"type": "content.part", "part": {"type": "text", "text": "子代理正文"}},
              t=T0 + 63),
        _line(type="llm.request", payload="subagent-noise" * 100, time=T0 + 64),
        _loop({"type": "tool.call", "toolCallId": "s1", "name": "Read",
               "args": {"path": "src/b.py"}}, t=T0 + 65),
        _loop({"type": "tool.result", "toolCallId": "s1",
               "result": {"output": "ok"}}, t=T0 + 66),
    ]
    return "\n".join(lines) + "\n"


def _make_zip(path: Path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps({"id": "mini"}))
        z.writestr("state.json", json.dumps({"id": "mini"}))
        z.writestr("agents/main/wire.jsonl", _make_main_wire())
        z.writestr("agents/agent-0/wire.jsonl", _make_agent0_wire())
        z.writestr(f"agents/main/blobs/{BLOB_HASH}", b"\x89PNG fake blob bytes")


class CleanerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.zip_path = root / "mini_session.zip"
        _make_zip(cls.zip_path)
        cls.out_dir = root / "out"
        cls.result = cleaner.clean_export(cls.zip_path, cls.out_dir)
        cls.records = [json.loads(l)
                       for l in (cls.out_dir / "cleaned.jsonl")
                       .read_text(encoding="utf-8").splitlines()]
        cls.md = (cls.out_dir / "cleaned.md").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _by_agent(self, agent):
        return [r for r in self.records if r.get("agent") == agent]

    def test_return_shape_and_files(self):
        self.assertEqual(set(self.result),
                         {"cleaned_jsonl", "cleaned_md", "cleaned_html", "stats"})
        for key in ("cleaned_jsonl", "cleaned_md", "cleaned_html"):
            self.assertTrue(Path(self.result[key]).exists(), key)
        self.assertEqual(self.result["cleaned_jsonl"], self.out_dir / "cleaned.jsonl")
        self.assertEqual(self.result["cleaned_html"], self.out_dir / "cleaned.html")

    def test_output_has_no_think_content(self):
        raw = (self.out_dir / "cleaned.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("我认为应该先写测试", raw)
        self.assertNotIn("子代理思考链", raw)
        for rec in self.records:
            self.assertNotEqual(rec.get("k"), "think")
            self.assertTrue(rec.get("think") is None or rec["think"] is False
                            or rec.get("k") == "assistant", rec)

    def test_user_counts_only_real_input(self):
        users = [r for r in self.records if r.get("k") == "user"]
        self.assertEqual(
            sorted(u["text"] for u in users),
            ["帮我读文件", "请实现 fizzbuzz"])
        main_users = [u for u in users if u["agent"] == "main"]
        self.assertEqual(len(main_users), 1)
        self.assertEqual(main_users[0]["text"], "请实现 fizzbuzz")
        self.assertEqual(main_users[0]["time"], T0 + 10)
        raw = (self.out_dir / "cleaned.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("<system-reminder>", raw)
        self.assertNotIn("<notification", raw)
        # context.append_message 不重复输出
        self.assertEqual(len(main_users), 1)

    def test_noise_types_dropped(self):
        raw = (self.out_dir / "cleaned.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("noise", raw)
        self.assertNotIn("subagent-noise", raw)
        for rec in self.records:
            self.assertNotIn(rec.get("type", ""), NOISE_TYPES, rec)
        self.assertEqual([r for r in self.records if r.get("k") == "raw"], [])

    def test_tool_call_result_paired(self):
        tools = [r for r in self.records if r.get("k") == "tool"]
        main_tools = [t for t in tools if t["agent"] == "main"]
        self.assertEqual([t["name"] for t in main_tools],
                         ["Read", "Bash", "Grep", "Agent"])
        by_name = {t["name"]: t for t in main_tools}
        self.assertEqual(by_name["Read"]["arg"], "src/a.py")
        self.assertEqual(by_name["Read"]["out_len"], len(READ_OUTPUT))
        self.assertFalse(by_name["Read"]["err"])
        self.assertEqual(by_name["Bash"]["err"], True)
        self.assertEqual(by_name["Bash"]["out_len"], len("boom"))
        self.assertLessEqual(len(by_name["Bash"]["arg"]), 160)
        self.assertTrue(by_name["Bash"]["arg"].endswith("…"))
        self.assertEqual(by_name["Grep"]["arg"], "TODO[图片]")
        self.assertEqual(by_name["Agent"]["arg"], "子代理任务A")
        # subagent 自己的工具
        sub_tools = [t for t in tools if t["agent"] == "agent-0"]
        self.assertEqual(len(sub_tools), 1)
        self.assertEqual(sub_tools[0]["name"], "Read")
        self.assertEqual(sub_tools[0]["out_len"], 2)

    def test_task_todo_compaction_records(self):
        tasks = [r for r in self.records if r.get("k") == "task"]
        self.assertEqual([t["text"] for t in tasks], ["[后台任务] 长构建 exit=0"])
        todos = [r for r in self.records if r.get("k") == "todo"]
        self.assertEqual(len(todos), 1)  # 只保留最后一次
        self.assertEqual(todos[0]["time"], T0 + 21)
        self.assertEqual(todos[0]["text"],
                         "- [x] 写测试\n- [ ] 写实现")
        comp = [r for r in self.records if r.get("k") == "compaction"]
        self.assertEqual([c["text"] for c in comp], ["这是一次压缩摘要"])

    def test_subagent_independent(self):
        sub = self._by_agent("agent-0")
        self.assertTrue(sub)
        for rec in sub:
            self.assertEqual(rec["agent"], "agent-0")
        main = self._by_agent("main")
        self.assertTrue(all(r["agent"] == "main" for r in main))
        # md 里 subagent 独立小节，标题来自 main 中 Agent 调用的 description
        self.assertIn("## 🧩 代理：main", self.md)
        self.assertIn("## 🧩 代理：agent-0 — 子代理任务A", self.md)

    def test_md_header_stats(self):
        stats = self.result["stats"]
        # 合计：user 2（main 1 + agent-0 1）、assistant 2、tool 5
        self.assertEqual(stats["user"], 2)
        self.assertEqual(stats["assistant"], 2)
        self.assertEqual(stats["tool"], 5)
        self.assertEqual(stats["task"], 1)
        self.assertEqual(stats["todo"], 1)
        self.assertEqual(stats["compaction"], 1)
        for line in (f"👤 用户 {stats['user']} 条",
                     f"🤖 助手 {stats['assistant']} 段",
                     f"🔧 工具 {stats['tool']} 次"):
            self.assertIn(line, self.md)
        self.assertIn("- 会话来源：kimi 导出 zip", self.md)
        self.assertIn("- 清洗时间：", self.md)
        self.assertIn("轮次统计", self.md)
        self.assertIn("时间范围：", self.md)
        self.assertIn(cleaner._ms_to_iso(T0 + 10), self.md)  # ISO 时间范围
        self.assertIn(f"- 会话创建：{cleaner._ms_to_iso(T0)}", self.md)

    def test_size_greatly_reduced(self):
        with zipfile.ZipFile(self.zip_path) as z:
            wire_bytes = sum(z.getinfo(n).file_size for n in z.namelist()
                             if n.endswith("wire.jsonl"))
        cleaned = self.result["stats"]["cleaned_jsonl_bytes"] \
            + self.result["stats"]["cleaned_md_bytes"]
        self.assertLess(cleaned * 5, wire_bytes,
                        f"清洗后应 <20%：{cleaned} vs {wire_bytes}")

    def test_filters_override(self):
        with tempfile.TemporaryDirectory() as d:
            filters = [
                {"name": "think", "enabled": False, "kind": "part"},
                {"name": "usage.record", "enabled": False, "kind": "type"},
            ]
            res = cleaner.clean_export(self.zip_path, Path(d), filters=filters)
            recs = [json.loads(l)
                    for l in Path(res["cleaned_jsonl"])
                    .read_text(encoding="utf-8").splitlines()]
            think_recs = [r for r in recs
                          if r.get("k") == "assistant" and r.get("think")]
            self.assertEqual(len(think_recs), 2)  # main + agent-0 各一条
            self.assertIn("我认为应该先写测试", think_recs[0]["text"])
            raw_recs = [r for r in recs if r.get("k") == "raw"
                        and r.get("type") == "usage.record"]
            self.assertEqual(len(raw_recs), 1)
            # 其余规则仍默认丢弃
            self.assertEqual([r for r in recs if r.get("k") == "user"
                              and "<system-reminder>" in r.get("text", "")], [])

    def test_md_turn_sections(self):
        self.assertRegex(self.md, r"## 👤 用户（\d{2}:\d{2}:\d{2}）")
        self.assertRegex(self.md, r"## 🤖 助手（\d{2}:\d{2}:\d{2}）")
        for name in ("Read", "Bash", "Grep", "Agent"):
            self.assertRegex(self.md, rf"## 🔧 工具：{name}（\d{{2}}:\d{{2}}:\d{{2}}）")
        # Bash 出错 → 显式错误标记；输出内容不再渲染，只留字符数
        self.assertIn("**❌ 错误**", self.md)
        self.assertNotIn("boom", self.md)
        self.assertNotIn("> line1", self.md)
        self.assertIn("（输出 4 字符）", self.md)  # bash "boom"
        self.assertIn("（输出 17 字符）", self.md)  # Read 输出长度
        # 关键参数一行
        self.assertIn("**参数**：`src/a.py`", self.md)
        # 正文原样保留
        self.assertIn("我先写个测试用例。", self.md)
        self.assertIn("请实现 fizzbuzz", self.md)

    def test_html_self_contained(self):
        html_path = self.out_dir / "cleaned.html"
        raw = html_path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "不应有 BOM")
        text = raw.decode("utf-8")
        self.assertIn("会话清洗预览", text)
        self.assertIn("👤 用户", text)
        self.assertIn("🤖 助手", text)
        self.assertIn("🔧 Bash", text)
        self.assertIn("❌ 错误", text)
        self.assertIn("（输出 17 字符）", text)  # Read 输出长度
        self.assertNotIn("<details>", text)  # 工具输出不再渲染
        self.assertNotIn("boom", text)  # 出错工具的输出内容也不出现
        self.assertIn('class="turn user"', text)
        self.assertIn('class="turn assistant"', text)
        self.assertIn('class="turn tool error"', text)
        self.assertIn("轮次统计", text)
        assert_html_self_contained(self, text)

    def test_jsonl_structure_unchanged(self):
        expected = {
            "user": {"k", "time", "text", "agent"},
            "assistant": {"k", "time", "text", "agent"},
            "tool": {"k", "time", "name", "arg", "err", "out_len", "agent"},
            "task": {"k", "time", "text", "agent"},
            "todo": {"k", "time", "text", "agent"},
            "compaction": {"k", "time", "text", "agent"},
            "raw": {"k", "time", "type", "agent"},
        }
        for rec in self.records:
            for key in rec:
                self.assertFalse(key.startswith("_"),
                                 f"内部字段泄漏进 jsonl：{rec}")
            want = expected[rec["k"]] | ({"think"} if rec.get("think") else set())
            self.assertEqual(set(rec), want, rec)
        # 抽样比对关键字段与旧版语义一致
        read = next(r for r in self.records
                    if r["k"] == "tool" and r.get("name") == "Read"
                    and r["agent"] == "main")
        self.assertEqual(
            (read["arg"], read["out_len"], read["err"], read["time"]),
            ("src/a.py", len(READ_OUTPUT), False, T0 + 100))

    def test_kimi_model_tracking_and_dividers(self):
        # llm.request 的 model 附到其后的 assistant 记录；html 仅在首现/切换
        # 时插模型分隔行（同模型连续两段不重复）。
        wire = "\n".join([
            _line(type="metadata", created_at=T0),
            _line(type="llm.request", model="k3-256k",
                  modelAlias="kimi-code/k3-256k", time=T0 + 1),
            _loop({"type": "content.part",
                   "part": {"type": "text", "text": "第一句"}}),
            _loop({"type": "content.part",
                   "part": {"type": "text", "text": "同模型第二句"}}, t=T0 + 2),
            _line(type="llm.request", model="k3",
                  modelAlias="kimi-code/k3", time=T0 + 3),
            _loop({"type": "content.part",
                   "part": {"type": "text", "text": "切换后"}}, t=T0 + 4),
        ]) + "\n"
        with tempfile.TemporaryDirectory() as d:
            zp = Path(d) / "s.zip"
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("agents/main/wire.jsonl", wire)
            res = cleaner.clean_export(zp, Path(d) / "out")
            recs = [json.loads(l) for l in
                    Path(res["cleaned_jsonl"]).read_text(encoding="utf-8")
                    .splitlines()]
            assistants = [r for r in recs if r["k"] == "assistant"]
            self.assertEqual([a.get("model") for a in assistants],
                             ["k3-256k", "k3-256k", "k3"])
            page = Path(res["cleaned_html"]).read_text(encoding="utf-8")
            self.assertIn("── 模型：k3-256k ──", page)
            self.assertIn("── 模型：k3 ──", page)
            self.assertEqual(page.count('class="model-div"'), 2)

    def test_kimi_search_keyword(self):
        # kimi 侧搜索工具：关键词完整保留在参数行，输出内容不渲染
        wire = "\n".join([
            _line(type="metadata", created_at=T0),
            _loop({"type": "tool.call", "toolCallId": "w1", "name": "WebSearch",
                   "args": {"query": "kimi code 使用教程 评审"}}),
            _loop({"type": "tool.result", "toolCallId": "w1",
                   "result": {"output": "一堆搜索结果正文不应出现"}}),
        ]) + "\n"
        with tempfile.TemporaryDirectory() as d:
            zp = Path(d) / "s.zip"
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("agents/main/wire.jsonl", wire)
            res = cleaner.clean_export(zp, Path(d) / "out")
            page = Path(res["cleaned_html"]).read_text(encoding="utf-8")
            self.assertIn("🔍 搜索 · WebSearch", page)
            self.assertIn("关键词：<code>kimi code 使用教程 评审</code>", page)
            self.assertNotIn("一堆搜索结果正文", page)
            md = Path(res["cleaned_md"]).read_text(encoding="utf-8")
            self.assertIn("## 🔍 搜索：WebSearch（", md)
            self.assertIn("**关键词**：`kimi code 使用教程 评审`", md)
            self.assertNotIn("一堆搜索结果正文", md)
            # jsonl 契约：_q/_out 均不落盘
            recs = [json.loads(l) for l in Path(res["cleaned_jsonl"])
                    .read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(recs), 1)
            self.assertEqual(set(recs[0]),
                             {"k", "time", "name", "arg", "err", "out_len",
                              "agent"})

    def test_tool_result_not_retained(self):
        # 大工具输出只留派生字段（err/out_len/image），原文不进记录（M19 内存防线）
        big = "x" * 1_000_000
        wire = "\n".join([
            _line(type="metadata", created_at=T0),
            _loop({"type": "tool.call", "toolCallId": "b1", "name": "Read",
                   "args": {"path": "a.txt"}}),
            _loop({"type": "tool.result", "toolCallId": "b1",
                   "result": {"output": big}}),
            _loop({"type": "tool.call", "toolCallId": "b2", "name": "Agent",
                   "args": {"description": "子任务甲"}}),
            _loop({"type": "tool.result", "toolCallId": "b2",
                   "result": {"output": "agent_id: agent-7\n..."}}),
        ]) + "\n"
        records, _ = cleaner.clean_wire(wire.splitlines())
        tools = [r for r in records if r["k"] == "tool"]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0]["out_len"], 1_000_000)
        self.assertFalse(tools[0]["err"])
        for t in tools:
            self.assertNotIn("_out", t)
        # Agent 标题关联走派生的 agent_id
        titles = {}
        cleaner.clean_wire(wire.splitlines(), agent_titles=titles)
        self.assertEqual(titles.get("agent-7"), "子任务甲")


class OpenCodeCleanerTest(unittest.TestCase):
    """opencode 会话 JSON 清洗（fixture 按真实导出样例裁剪）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.json_path = root / "session.json"
        cls.json_path.write_text(
            json.dumps(_make_opencode_doc(), ensure_ascii=False),
            encoding="utf-8")
        cls.out_dir = root / "out"
        cls.result = cleaner.clean_opencode(cls.json_path, cls.out_dir)
        cls.records = [json.loads(l)
                       for l in (cls.out_dir / "cleaned.jsonl")
                       .read_text(encoding="utf-8").splitlines()]
        cls.md = (cls.out_dir / "cleaned.md").read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_detect_source(self):
        self.assertEqual(cleaner.detect_source(self.json_path), "opencode_json")
        self.assertEqual(cleaner.detect_source(self_zip_path()), "kimi_zip")
        other = self.json_path.parent / "not_session.json"
        other.write_text(json.dumps({"hello": 1}), encoding="utf-8")
        self.assertEqual(cleaner.detect_source(other), "unknown")

    def test_user_records(self):
        users = [r for r in self.records if r["k"] == "user"]
        self.assertEqual([u["text"] for u in users],
                         ["请开发 XX 功能", "继续"])
        self.assertEqual(users[0]["time"], T0 + 10)

    def test_reasoning_dropped_and_filterable(self):
        raw = (self.out_dir / "cleaned.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("思考链内容不应出现", raw)
        self.assertNotIn("子代理思考链不应出现", raw)
        with tempfile.TemporaryDirectory() as d:
            filters = [{"name": "think", "enabled": False, "kind": "part"}]
            res = cleaner.clean_opencode(self.json_path, Path(d), filters=filters)
            recs = [json.loads(l)
                    for l in Path(res["cleaned_jsonl"])
                    .read_text(encoding="utf-8").splitlines()]
            thinks = [r for r in recs
                      if r.get("k") == "assistant" and r.get("think")]
            self.assertEqual(len(thinks), 1)
            self.assertIn("思考链内容不应出现", thinks[0]["text"])
            self.assertEqual(thinks[0]["time"], T0 + 21)

    def test_tool_records(self):
        tools = [r for r in self.records if r["k"] == "tool"]
        self.assertEqual([t["name"] for t in tools],
                         ["glob", "bash", "edit", "todowrite"])
        by_name = {t["name"]: t for t in tools}
        self.assertEqual(by_name["glob"]["arg"], "**/*.py")
        self.assertEqual(by_name["glob"]["out_len"], 9)  # "a.py\nb.py"
        self.assertFalse(by_name["glob"]["err"])
        self.assertTrue(by_name["bash"]["err"])
        self.assertTrue(by_name["bash"]["arg"].endswith("…"))  # 超长命令截断
        self.assertEqual(by_name["edit"]["arg"], "src/a.py")

    def test_patch_and_step_parts_dropped(self):
        raw = (self.out_dir / "cleaned.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("step-start", raw)
        self.assertNotIn("step-finish", raw)
        self.assertNotIn("patch", [r["k"] for r in self.records].__str__())
        self.assertEqual([r for r in self.records if r.get("k") == "raw"], [])

    def test_todo_from_last_todowrite(self):
        todos = [r for r in self.records if r["k"] == "todo"]
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["text"], "- [x] 写测试\n- [ ] 写实现")

    def test_stats_and_md(self):
        stats = self.result["stats"]
        self.assertEqual(stats["source"], "session.json")
        self.assertEqual(stats["user"], 2)
        self.assertEqual(stats["assistant"], 2)
        self.assertEqual(stats["tool"], 4)
        self.assertEqual(stats["todo"], 1)
        self.assertEqual(stats["agents"], 1)
        self.assertEqual(stats["created_at"], cleaner._ms_to_iso(T0))
        self.assertIn("## 👤 用户（", self.md)
        self.assertIn("## 🤖 助手（", self.md)
        self.assertIn("## 🔧 工具：", self.md)
        self.assertIn("请开发 XX 功能", self.md)
        self.assertIn("👤 用户 2 条", self.md)
        self.assertIn("- 会话来源：opencode 会话 JSON", self.md)

    def test_clean_session_dispatch(self):
        with tempfile.TemporaryDirectory() as d:
            res = cleaner.clean_session(self.json_path, Path(d))
            self.assertEqual(res["stats"]["user"], 2)
            res2 = cleaner.clean_session(self_zip_path(), Path(d))
            self.assertIn("user", res2["stats"])
        with self.assertRaises(ValueError):
            cleaner.clean_session(
                self.json_path.parent / "not_session.json",
                Path(self._tmp.name) / "nope")

    def test_load_rejects_non_opencode(self):
        with self.assertRaises(ValueError):
            cleaner.load_opencode_session(
                self.json_path.parent / "not_session.json")

    def test_md_tool_error_and_empty_output(self):
        # bash 出错 → 显式错误标记；输出一律不渲染（空输出也没有「（无输出）」）
        self.assertRegex(self.md, r"## 🔧 工具：bash（\d{2}:\d{2}:\d{2}）")
        self.assertIn("**❌ 错误**", self.md)
        self.assertIn("（输出 0 字符）", self.md)
        self.assertNotIn("（无输出）", self.md)
        # todowrite → todo 摘要折成一行展示
        self.assertIn("## 🔧 工具：todowrite（", self.md)

    def test_html_self_contained(self):
        text = (self.out_dir / "cleaned.html").read_text(encoding="utf-8")
        self.assertIn("🔧 bash", text)
        self.assertIn("❌ 错误", text)
        self.assertIn("（输出 9 字符）", text)  # glob 输出 "a.py\nb.py" 的长度
        self.assertNotIn("<details>", text)  # 工具输出不再渲染
        self.assertNotIn("a.py\nb.py", text)
        assert_html_self_contained(self, text)

    def test_tool_output_not_rendered(self):
        # 工具输出内容不进 md/html/复制文本，只留字符数
        doc = _make_opencode_doc()
        long_out = "\n".join(f"line-{i}" for i in range(1, 101))
        doc["messages"][1]["parts"][3]["state"]["output"] = long_out  # glob
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            res = cleaner.clean_opencode(p, Path(d) / "out")
            md = Path(res["cleaned_md"]).read_text(encoding="utf-8")
            self.assertNotIn("line-1", md)
            self.assertNotIn("结果截断", md)
            page = Path(res["cleaned_html"]).read_text(encoding="utf-8")
            self.assertNotIn("line-99", page)
            self.assertNotIn("<details>", page)
            self.assertIn(f"（输出 {len(long_out)} 字符）", page)
            # 复制按钮内嵌的纯文本同样不含输出内容
            m = re.search(r'var text=(".*?");\nvar btn', page, re.S)
            self.assertIsNotNone(m)
            self.assertNotIn("line-1", json.loads(m.group(1)))
            # 但 out_len / jsonl 契约不受影响
            recs = [json.loads(l) for l in Path(res["cleaned_jsonl"])
                    .read_text(encoding="utf-8").splitlines()]
            glob = next(r for r in recs
                        if r["k"] == "tool" and r["name"] == "glob")
            self.assertEqual(glob["out_len"], len(long_out))

    def test_html_escapes_injection(self):
        doc = _make_opencode_doc()
        doc["messages"][0]["parts"][0]["text"] = '<script>alert("x")</script>'
        doc["messages"][1]["parts"][3]["state"]["output"] = \
            '<img src=x onerror=alert(1)>'
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            res = cleaner.clean_opencode(p, Path(d) / "out")
            page = Path(res["cleaned_html"]).read_text(encoding="utf-8")
            self.assertNotIn('<script>alert("x")</script>', page)
            self.assertNotIn("<img src=x", page)
            self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", page)
            # 工具输出整体不再渲染，其中的注入内容也随之消失
            self.assertNotIn("onerror=alert", page)
            # md 是纯文本产物，正文原样保留
            md = Path(res["cleaned_md"]).read_text(encoding="utf-8")
            self.assertIn('<script>alert("x")</script>', md)
            self.assertNotIn("<img src=x", md)

    def test_jsonl_structure_unchanged(self):
        for rec in self.records:
            for key in rec:
                self.assertFalse(key.startswith("_"),
                                 f"内部字段泄漏进 jsonl：{rec}")
        tool = next(r for r in self.records if r["k"] == "tool")
        self.assertEqual(set(tool),
                         {"k", "time", "name", "arg", "err", "out_len", "agent"})
        user = next(r for r in self.records if r["k"] == "user")
        self.assertEqual(set(user), {"k", "time", "text", "agent"})

    def test_jsonl_assistant_model_field(self):
        # modelID → assistant 记录的可选 model 键；其他记录不带
        assistants = [r for r in self.records if r["k"] == "assistant"]
        self.assertEqual([a.get("model") for a in assistants],
                         ["big-pickle", "kimi-k3"])
        for r in self.records:
            if r["k"] != "assistant":
                self.assertNotIn("model", r, r)

    def test_html_model_dividers(self):
        # 首现 + 切换各一次，共两条分隔行（msg_2 / msg_3 模型不同）
        text = (self.out_dir / "cleaned.html").read_text(encoding="utf-8")
        self.assertIn("── 模型：big-pickle ──", text)
        self.assertIn("── 模型：kimi-k3 ──", text)
        self.assertEqual(text.count('class="model-div"'), 2)

    def test_html_copy_button(self):
        text = (self.out_dir / "cleaned.html").read_text(encoding="utf-8")
        self.assertIn('id="copyAll"', text)
        self.assertIn("📋 复制全部内容", text)
        self.assertIn("已复制", text)
        self.assertIn("navigator.clipboard", text)

    def test_html_autowrap_css(self):
        text = (self.out_dir / "cleaned.html").read_text(encoding="utf-8")
        self.assertIn("white-space: pre-wrap", text)
        self.assertIn("overflow-wrap: anywhere", text)
        self.assertIn("word-break: break-word", text)

    def test_html_tool_arg_no_json_braces(self):
        # 未知工具的 JSON 参数在 html 里摊平为纯文本，不外露括号
        rec = {"k": "tool", "time": T0, "name": "mcp_tool",
               "arg": '{"role": "admin", "count": 3}',
               "err": False, "out_len": 0, "_out": ""}
        frag = cleaner._turn_html(rec)
        self.assertNotIn('{"role"', frag)
        self.assertIn("role: admin", frag)
        self.assertIn("count: 3", frag)
        # 被 160 字截断的 JSON（无法 parse）也去括号
        rec2 = dict(rec, arg='{"role": "admin", "detail": "很长…')
        frag2 = cleaner._turn_html(rec2)
        self.assertNotIn("{", frag2)
        self.assertIn("role：admin", frag2)
        # shell 命令里合法的花括号不受影响
        rec3 = dict(rec, name="bash",
                    arg='ForEach-Object { $_ -Raw }')
        frag3 = cleaner._turn_html(rec3)
        self.assertIn("{ $_ -Raw }", frag3)

    def test_html_markdown_rendering(self):
        doc = _make_opencode_doc()
        doc["messages"][0]["parts"][0]["text"] = (
            "# 计划\n\n**重点** 先做 `A`，再看 *B*。\n\n"
            "```py\nprint(1)\n```\n\n"
            "- 甲\n- 乙\n\n1. 一\n2. 二\n\n> 注意点\n\n---\n\n"
            "见[文档](https://example.com/x)。")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            res = cleaner.clean_opencode(p, Path(d) / "out")
            page = Path(res["cleaned_html"]).read_text(encoding="utf-8")
        self.assertIn("<h2>计划</h2>", page)
        self.assertIn("<strong>重点</strong>", page)
        self.assertIn("<code>A</code>", page)
        self.assertIn("<em>B</em>", page)
        self.assertIn('<pre class="code"><code>print(1)</code></pre>', page)
        self.assertIn("<ul><li>甲</li><li>乙</li></ul>", page)
        self.assertIn("<ol><li>一</li><li>二</li></ol>", page)
        self.assertIn("<blockquote>注意点</blockquote>", page)
        self.assertIn("<hr>", page)
        self.assertIn('<a href="https://example.com/x" target="_blank" '
                      'rel="noopener">文档</a>', page)

    def test_html_markdown_table(self):
        doc = _make_opencode_doc()
        doc["messages"][0]["parts"][0]["text"] = (
            "前言段落\n\n"
            "| 名称 | 数值 | 备注 |\n"
            "| :--- | ---: | :---: |\n"
            "| 甲 | 1 | 低 |\n"
            "| 乙<b> | 2 | 高 |\n\n"
            "后言段落")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            res = cleaner.clean_opencode(p, Path(d) / "out")
            page = Path(res["cleaned_html"]).read_text(encoding="utf-8")
        self.assertIn("<table><thead><tr>", page)
        self.assertIn('<th style="text-align:left">名称</th>', page)
        self.assertIn('<th style="text-align:right">数值</th>', page)
        self.assertIn('<th style="text-align:center">备注</th>', page)
        self.assertIn('<td style="text-align:left">甲</td>', page)
        # 单元格内容全量转义
        self.assertIn("乙&lt;b&gt;", page)
        # 表格前后段落不粘连
        self.assertIn("<p>前言段落</p>", page)
        self.assertIn("<p>后言段落</p>", page)

    def test_html_markdown_table_not_breaking_paragraph(self):
        # 与 GFM 一致：表格不能打断段落（前无空行时按普通文本处理）
        text = "前言\n| a | b |\n| --- | --- |\n| 1 | 2 |"
        page = cleaner.render_markdown(text)
        self.assertNotIn("<table>", page)

    def test_search_keyword_rendered(self):
        # 搜索类工具：query/keywords 字段都识别，关键词完整可见，输出不渲染
        doc = _make_opencode_doc()
        doc["messages"][1]["parts"] += [
            {"id": "prt_s1", "sessionID": "ses_x", "messageID": "msg_2",
             "type": "tool", "callID": "c9", "tool": "web_search",
             "state": {"status": "completed",
                       "input": {"query": "Alethic Insight 招新 机试流程"},
                       "output": "搜索结果正文不应出现",
                       "time": {"start": T0 + 25}}},
            {"id": "prt_s2", "sessionID": "ses_x", "messageID": "msg_2",
             "type": "tool", "callID": "c10", "tool": "call_data_source_tool",
             "state": {"status": "completed",
                       "input": {"keywords": "GDP 增速"},
                       "output": "数据接口返回不应出现",
                       "time": {"start": T0 + 26}}},
            {"id": "prt_s3", "sessionID": "ses_x", "messageID": "msg_2",
             "type": "tool", "callID": "c11", "tool": "fetch_url",
             "state": {"status": "completed",
                       "input": {"url": "https://example.com/a"},
                       "output": "网页正文不应出现",
                       "time": {"start": T0 + 27}}},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            res = cleaner.clean_opencode(p, Path(d) / "out")
            page = Path(res["cleaned_html"]).read_text(encoding="utf-8")
            self.assertIn("🔍 搜索 · web_search", page)
            self.assertIn(
                "关键词：<code>Alethic Insight 招新 机试流程</code>", page)
            self.assertIn("🔍 搜索 · call_data_source_tool", page)
            self.assertIn("关键词：<code>GDP 增速</code>", page)
            # 无 query 字段的搜索类工具回退普通参数一行（JSON 摊平）
            self.assertIn("🔍 搜索 · fetch_url", page)
            self.assertIn("参数：<code>url: https://example.com/a</code>", page)
            for leaked in ("搜索结果正文不应出现", "数据接口返回不应出现",
                           "网页正文不应出现"):
                self.assertNotIn(leaked, page)
            md = Path(res["cleaned_md"]).read_text(encoding="utf-8")
            self.assertIn("## 🔍 搜索：web_search（", md)
            self.assertIn("**关键词**：`Alethic Insight 招新 机试流程`", md)
            self.assertIn("**关键词**：`GDP 增速`", md)
            self.assertNotIn("搜索结果正文不应出现", md)
            # jsonl 不落 _q
            raw = Path(res["cleaned_jsonl"]).read_text(encoding="utf-8")
            self.assertNotIn('"_q"', raw)


class SearchDetectTest(unittest.TestCase):
    """搜索类工具识别与关键词抽取的纯函数测试。"""

    def test_is_search_tool(self):
        yes = ("WebSearch", "web_search", "websearch", "google_search",
               "brave_search", "tavily", "tavily_search", "search",
               "search_web", "fetch_url", "FetchURL", "webbridge.navigate",
               "call_data_source_tool", "get_data_source_desc")
        no = ("Bash", "Read", "Write", "Edit", "Grep", "Glob",
              "todowrite", "question", "Agent")
        for name in yes:
            self.assertTrue(cleaner._is_search_tool(name), name)
        for name in no:
            self.assertFalse(cleaner._is_search_tool(name), name)

    def test_search_query_fields(self):
        self.assertEqual(cleaner._search_query({"query": " a "}), "a")
        self.assertEqual(cleaner._search_query({"keyword": "k"}), "k")
        self.assertEqual(cleaner._search_query({"keywords": "ks"}), "ks")
        self.assertEqual(cleaner._search_query({"q": "qq"}), "qq")
        self.assertEqual(cleaner._search_query({"search_terms": "st"}), "st")
        # 字段优先级：query 在 q 之前
        self.assertEqual(cleaner._search_query({"q": "x", "query": "y"}), "y")
        self.assertIsNone(cleaner._search_query({"url": "https://x"}))
        self.assertIsNone(cleaner._search_query({"query": "  "}))
        self.assertIsNone(cleaner._search_query({"query": 42}))
        self.assertIsNone(cleaner._search_query("not-a-dict"))


class MarkdownRendererTest(unittest.TestCase):
    """render_markdown 子集渲染器纯函数测试。"""

    def test_blocks_and_inline(self):
        h = cleaner.render_markdown(
            "## 标题\n\n**粗** *斜* `码`\n\n```\nx<y\n```\n\n"
            "- a\n- b\n\n3. c\n\n> q\n\n***\n\n[t](https://e.com)")
        self.assertIn("<h3>标题</h3>", h)
        self.assertIn("<strong>粗</strong>", h)
        self.assertIn("<em>斜</em>", h)
        self.assertIn("<code>码</code>", h)
        self.assertIn('<pre class="code"><code>x&lt;y</code></pre>', h)
        self.assertIn("<ul><li>a</li><li>b</li></ul>", h)
        self.assertIn("<ol><li>c</li></ol>", h)
        self.assertIn("<blockquote>q</blockquote>", h)
        self.assertIn("<hr>", h)
        self.assertIn('<a href="https://e.com" target="_blank" '
                      'rel="noopener">t</a>', h)

    def test_escape_and_link_safety(self):
        h = cleaner.render_markdown('<b>bold</b> [x](javascript:alert(1))')
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", h)
        self.assertNotIn("<b>bold</b>", h)
        self.assertNotIn('<a href="javascript:', h)  # 非 http(s) 不渲染成链接
        # `__` 不按加粗处理（避免 __init__ 之类技术文本被误渲染）
        self.assertIn("__init__", cleaner.render_markdown("见 __init__.py"))

    def test_soft_linebreaks_become_br(self):
        h = cleaner.render_markdown("第一行\n第二行")
        self.assertIn("第一行<br>第二行", h)


def self_zip_path():
    """复用 kimi mini zip（模块上方 _make_zip）做 detect_source 的另一分支。"""
    import tempfile as _tf
    p = Path(_tf.gettempdir()) / "mini_detect_session.zip"
    if not p.exists():
        _make_zip(p)
    return p


def assert_html_self_contained(case, text):
    """html 产物自包含断言：内联样式/内联脚本（复制按钮），无任何外部资源引用。"""
    case.assertIn("<style>", text)
    case.assertIn("</html>", text)
    for bad in ("<script src", "<link", "<img", "<iframe", "<video",
                "@import", "url(", " src="):
        case.assertNotIn(bad, text, bad)


def _make_opencode_doc():
    """按真实 opencode 导出样例裁剪的代表性片段（两轮对话 + 四种工具）。"""
    def part(pid, mid, ptype, **kw):
        return {"id": f"prt_{pid}", "sessionID": "ses_x", "messageID": mid,
                "type": ptype, **kw}

    return {
        "info": {"id": "ses_x", "directory": "P:\\proj",
                 "time": {"created": T0, "updated": T0 + 90000}},
        "messages": [
            {"info": {"id": "msg_1", "sessionID": "ses_x", "role": "user",
                      "time": {"created": T0 + 10},
                      "summary": {"diffs": [{"file": "a.py", "patch": "Index: a.py"}]}},
             "parts": [part("p1", "msg_1", "text", text="请开发 XX 功能")]},
            {"info": {"id": "msg_2", "sessionID": "ses_x", "role": "assistant",
                      "time": {"created": T0 + 20, "completed": T0 + 30},
                      "modelID": "big-pickle", "providerID": "opencode",
                      "finish": "tool-calls"},
             "parts": [
                 part("p2", "msg_2", "step-start", snapshot="abc"),
                 part("p3", "msg_2", "reasoning",
                      text="思考链内容不应出现" * 10,
                      time={"start": T0 + 21, "end": T0 + 22}),
                 part("p4", "msg_2", "text", text="我先看下目录。"),
                 part("p5", "msg_2", "tool", callID="c1", tool="glob",
                      state={"status": "completed",
                             "input": {"pattern": "**/*.py"},
                             "output": "a.py\nb.py", "title": "",
                             "metadata": {"count": 2},
                             "time": {"start": T0 + 23, "end": T0 + 24}}),
                 part("p6", "msg_2", "step-finish", reason="tool-calls",
                      cost=0.001, tokens={"total": 1}),
             ]},
            {"info": {"id": "msg_3", "sessionID": "ses_x", "role": "assistant",
                      "time": {"created": T0 + 40, "completed": T0 + 60},
                      "modelID": "kimi-k3", "providerID": "moonshot",
                      "finish": "stop"},
             "parts": [
                 part("p7", "msg_3", "tool", callID="c2", tool="bash",
                      state={"status": "error",
                             "input": {"command": LONG_COMMAND},
                             "output": "", "time": {"start": T0 + 41}}),
                 part("p8", "msg_3", "tool", callID="c3", tool="edit",
                      state={"status": "completed",
                             "input": {"filePath": "src/a.py",
                                       "oldString": "x", "newString": "y"},
                             "output": "ok", "time": {"start": T0 + 42}}),
                 part("p9", "msg_3", "tool", callID="c4", tool="todowrite",
                      state={"status": "completed",
                             "input": {"todos": [
                                 {"content": "写测试", "status": "done"},
                                 {"content": "写实现", "status": "pending"}]},
                             "output": "", "time": {"start": T0 + 43}}),
                 part("p10", "msg_3", "patch", hash="abc",
                      files=["src/a.py"]),
                 part("p11", "msg_3", "text", text="完成。",
                      time={"start": T0 + 50}),
             ]},
            {"info": {"id": "msg_4", "sessionID": "ses_x", "role": "user",
                      "time": {"created": T0 + 70}},
             "parts": [part("p12", "msg_4", "text", text="继续"),
                       part("p13", "msg_4", "text", text="",
                            synthetic=True)]},
        ],
    }


if __name__ == "__main__":
    unittest.main()

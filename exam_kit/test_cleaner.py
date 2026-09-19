"""cleaner 纯函数单测（stdlib unittest，无第三方依赖）。

用代码合成一个 mini 会话 zip，覆盖全部 wire 行类型 + 一个 subagent +
一个 blob 引用，断言清洗规则与输出统计。"""

import json
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
        self.assertEqual(set(self.result), {"cleaned_jsonl", "cleaned_md", "stats"})
        for key in ("cleaned_jsonl", "cleaned_md"):
            self.assertTrue(Path(self.result[key]).exists(), key)
        self.assertEqual(self.result["cleaned_jsonl"], self.out_dir / "cleaned.jsonl")

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
        self.assertIn("## main", self.md)
        self.assertIn("## agent-0 — 子代理任务A", self.md)

    def test_md_header_stats(self):
        stats = self.result["stats"]
        # 合计：user 2（main 1 + agent-0 1）、assistant 2、tool 5
        self.assertEqual(stats["user"], 2)
        self.assertEqual(stats["assistant"], 2)
        self.assertEqual(stats["tool"], 5)
        self.assertEqual(stats["task"], 1)
        self.assertEqual(stats["todo"], 1)
        self.assertEqual(stats["compaction"], 1)
        for line in (f"- user 条数：{stats['user']}",
                     f"- assistant 段数：{stats['assistant']}",
                     f"- tool 调用数：{stats['tool']}"):
            self.assertIn(line, self.md)
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
        self.assertIn("## main", self.md)
        self.assertIn("请开发 XX 功能", self.md)
        self.assertIn("- user 条数：2", self.md)

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


def self_zip_path():
    """复用 kimi mini zip（模块上方 _make_zip）做 detect_source 的另一分支。"""
    import tempfile as _tf
    p = Path(_tf.gettempdir()) / "mini_detect_session.zip"
    if not p.exists():
        _make_zip(p)
    return p


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

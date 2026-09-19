"""Kimi Code 会话导出清洗器（Python 3.12 纯标准库）。

把 `kimi export` 出的会话 zip（agents/main/wire.jsonl + agents/agent-N/wire.jsonl
+ blobs/ 等）清洗成评审用的轻量记录：丢弃 LLM 请求/用量/配置噪音与思考链，
保留用户输入、助手正文、工具调用摘要、后台任务、todo 与压缩摘要，供云端评审
「人类率」判读。接口：clean_export(zip_path, out_dir) -> dict，落盘 cleaned.jsonl
与 cleaned.md。命令行：python cleaner.py <zip> <out_dir>。

清洗规则（已定型）：
- 整行丢弃：llm.request / usage.record / llm.tools_snapshot / mcp.tools_discovered /
  profile.bind / config.update / permission.set_mode / runtime.set_binding /
  full_compaction.begin / full_compaction.complete（metadata 的 created_at 记入头部）；
  loop event 的 step.begin / step.end；content.part 的 think（思考链）；
  context.append_message（与 turn.prompt 重复）；其余未列出的类型一并丢弃。
- 保留转换：turn.prompt（origin=user 且非系统注入）→ user；
  content.part(text) → assistant；tool.call/tool.result 按 toolCallId 配对 → tool；
  task.terminated → task；tools.update_store(key=todo) 只留最后一次 → todo；
  context.apply_compaction → compaction。
- subagent wire 各自独立清洗，jsonl 记录加 "agent" 字段；md 中每个 subagent
  独立小节，标题用 main 里 Agent 工具调用结果中的 agent_id 关联的 description。

可通过模块级 FILTERS（每条 {"name","enabled","kind"}）控制各丢弃规则，
clean_export(..., filters=[...]) 可整体覆盖；某条规则 disabled 时对应行以
{"k":"raw"} 原样登记（think 则保留为 assistant 记录），便于按需放宽。

opencode 兼容（M11）：opencode 导出/存储的会话 JSON（顶层 {info, messages}，
messages[i] = {info:{id,role,time,...}, parts:[...]}）同样清洗为 cleaned.jsonl/.md：
- 映射：user 消息 text 分片 → user；assistant text 分片 → assistant；
  reasoning 分片 → 思考链（走 think 过滤规则）；tool 分片 → tool（state.status=error
  记错误）；todowrite 工具的最后一次输入另外落一条 todo；patch / step-start /
  step-finish / summary.diffs 等其余分片丢弃。
- 整个会话为单一 agent "main"（opencode 无 subagent wire）。
入口统一用 clean_session(path, out_dir)：按内容自动识别 kimi zip / opencode JSON。
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# 各工具调用 args 的关键字段；其余工具退化为整个 args 的 JSON 截断。
ARG_FIELD_BY_TOOL = {
    "Bash": "command",
    "Read": "path",
    "Write": "path",
    "Edit": "path",
    "Grep": "pattern",
    "Glob": "pattern",
    "Agent": "description",
}

# opencode 工具名（小写）→ args 关键字段。
OPENCODE_ARG_FIELD_BY_TOOL = {
    "bash": "command",
    "read": "filePath",
    "write": "filePath",
    "edit": "filePath",
    "grep": "pattern",
    "glob": "pattern",
    "list": "path",
    "task": "description",
    "todoread": "todos",
    "todowrite": "todos",
}

ARG_MAX_CHARS = 160

# 丢弃规则清单。kind: type=按顶层 type 丢弃；loop_event=按 event.type 丢弃；
# part=丢弃 think 思考链；metadata=不落记录但取 created_at；prompt_prefix=丢弃
# <system-reminder>/<notification 开头的注入 prompt。enabled=False 时该类行保留。
FILTERS = [
    {"name": "metadata",               "enabled": True, "kind": "metadata"},
    {"name": "llm.request",            "enabled": True, "kind": "type"},
    {"name": "usage.record",           "enabled": True, "kind": "type"},
    {"name": "llm.tools_snapshot",     "enabled": True, "kind": "type"},
    {"name": "mcp.tools_discovered",   "enabled": True, "kind": "type"},
    {"name": "profile.bind",           "enabled": True, "kind": "type"},
    {"name": "config.update",          "enabled": True, "kind": "type"},
    {"name": "permission.set_mode",    "enabled": True, "kind": "type"},
    {"name": "runtime.set_binding",    "enabled": True, "kind": "type"},
    {"name": "full_compaction.begin",  "enabled": True, "kind": "type"},
    {"name": "full_compaction.complete", "enabled": True, "kind": "type"},
    {"name": "step.begin",             "enabled": True, "kind": "loop_event"},
    {"name": "step.end",               "enabled": True, "kind": "loop_event"},
    {"name": "think",                  "enabled": True, "kind": "part"},
    {"name": "context.append_message", "enabled": True, "kind": "type"},
    {"name": "prompt_prefix",          "enabled": True, "kind": "prompt_prefix"},
]

# turn.prompt 文本以这些前缀开头时视为系统注入，不是考生真实输入。
INJECTED_PROMPT_PREFIXES = ("<system-reminder>", "<notification")


def _ms_to_iso(ms):
    """epoch 毫秒 → UTC ISO 字符串；缺失/非法返回 None。"""
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _truncate(text, limit=ARG_MAX_CHARS):
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _prompt_text(rec):
    """拼接 turn.prompt 的 input 文本分片。"""
    parts = rec.get("input") or []
    return "\n".join(
        p.get("text", "") for p in parts
        if isinstance(p, dict) and p.get("type") == "text")


def _output_len(result):
    """tool.result 输出文本长度：字符串取长度；数组取 text 分片长度之和。"""
    if result is None:
        return None
    out = result.get("output")
    if isinstance(out, str):
        return len(out)
    if isinstance(out, list):
        return sum(
            len(p.get("text", "")) for p in out
            if isinstance(p, dict) and p.get("type") == "text")
    return len(str(out)) if out is not None else 0


def _has_image_output(result):
    out = (result or {}).get("output")
    return isinstance(out, list) and any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in out)


def _tool_arg(call, result):
    """提取工具调用关键字段并截断；图片输出追加 [图片] 标记。"""
    name = call.get("name", "")
    args = call.get("args") or {}
    field = ARG_FIELD_BY_TOOL.get(name)
    raw = args.get(field) if field else None
    if not isinstance(raw, str):
        raw = json.dumps(raw if raw is not None else args, ensure_ascii=False)
    arg = _truncate(raw)
    if _has_image_output(result):
        arg += "[图片]"
    return arg


def _todo_text(value):
    """todo 列表 → '- [x] title' 多行文本（done 打勾）。"""
    lines = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        mark = "x" if item.get("status") == "done" else " "
        lines.append(f"- [{mark}] {item.get('title', '')}")
    return "\n".join(lines)


def _agent_id_from_result(result):
    """从 Agent 工具结果文本开头提取 agent_id（如 'agent-0'）。"""
    out = (result or {}).get("output")
    if not isinstance(out, str):
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("agent_id:"):
            return line.split(":", 1)[1].strip() or None
        if line:
            break
    return None


def clean_wire(lines, filters=None):
    """清洗单个 wire 的 JSONL 行列表 → 轻量记录列表（不含 agent 字段）。

    返回 (records, info)：info 含 created_at（metadata）、dropped 计数、
    dropped_other（未识别类型的丢弃数）。
    """
    fl = {f["name"]: bool(f.get("enabled", True)) for f in (filters or FILTERS)}

    def dropped(name):
        return fl.get(name, True)

    # 预扫：toolCallId → result（tool 记录在 call 位置输出，需先拿到配对结果）
    results = {}
    for line in lines:
        rec = json.loads(line)
        ev = rec.get("event") or {}
        if rec.get("type") == "context.append_loop_event" \
                and ev.get("type") == "tool.result":
            results[ev.get("toolCallId")] = ev.get("result")

    records = []
    info = {"created_at": None, "dropped": 0, "dropped_other": 0}

    def drop():
        info["dropped"] += 1

    def emit(rec):
        records.append(rec)

    last_todo = None
    for line in lines:
        rec = json.loads(line)
        rtype = rec.get("type")
        ts = rec.get("time")

        if rtype == "metadata":
            info["created_at"] = rec.get("created_at")
            continue

        fentry = next((f for f in (filters or FILTERS) if f["name"] == rtype), None)
        if fentry is not None and fentry.get("kind") == "type":
            if fentry.get("enabled", True):
                drop()
            else:
                emit({"k": "raw", "time": ts, "type": rtype})
            continue

        if rtype == "context.append_loop_event":
            ev = rec.get("event") or {}
            et = ev.get("type")
            if et in ("step.begin", "step.end"):
                if dropped(et):
                    drop()
                else:
                    emit({"k": "raw", "time": ts, "type": f"loop_event:{et}"})
            elif et == "content.part":
                part = ev.get("part") or {}
                ptype = part.get("type")
                if ptype == "think":
                    if dropped("think"):
                        drop()
                    else:
                        emit({"k": "assistant", "time": ts,
                              "text": part.get("think", ""), "think": True})
                elif ptype == "text":
                    emit({"k": "assistant", "time": ts,
                          "text": part.get("text", "")})
                else:
                    info["dropped_other"] += 1
            elif et in ("tool.call", "tool.result"):
                pass  # 结果配对后统一输出，见下方
            else:
                info["dropped_other"] += 1
            continue

        if rtype == "turn.prompt":
            text = _prompt_text(rec)
            is_user = (rec.get("origin") or {}).get("kind") == "user"
            injected = any(text.startswith(p) for p in INJECTED_PROMPT_PREFIXES)
            if is_user and not (injected and dropped("prompt_prefix")):
                emit({"k": "user", "time": ts, "text": text})
            else:
                drop()
            continue

        if rtype == "task.terminated":
            inf = rec.get("info") or {}
            emit({"k": "task", "time": ts,
                  "text": f"[后台任务] {inf.get('description', '')} "
                          f"exit={inf.get('exitCode')}"})
            continue

        if rtype == "tools.update_store" and rec.get("key") == "todo":
            last_todo = rec
            continue

        if rtype == "context.apply_compaction":
            emit({"k": "compaction", "time": ts, "text": rec.get("summary", "")})
            continue

        info["dropped_other"] += 1

    # tool.call × tool.result 按 toolCallId 配对，按 call 顺序输出
    call_ids = []
    for line in lines:
        rec = json.loads(line)
        ev = rec.get("event") or {}
        if rec.get("type") == "context.append_loop_event" \
                and ev.get("type") == "tool.call":
            call_ids.append((rec.get("time"), ev))
    emitted_results = set()
    for ts, call in call_ids:
        tcid = call.get("toolCallId")
        result = results.get(tcid)
        emit({"k": "tool", "time": ts, "name": call.get("name", ""),
              "arg": _tool_arg(call, result),
              "err": bool((result or {}).get("isError")),
              "out_len": _output_len(result)})
        emitted_results.add(tcid)

    if last_todo is not None:
        emit({"k": "todo", "time": last_todo.get("time"),
              "text": _todo_text(last_todo.get("value"))})

    return records, info


# ---------- opencode 会话 JSON ----------

def looks_like_opencode(obj) -> bool:
    """判断解析出的 JSON 是否为 opencode 会话（导出或存储单文件）。"""
    if not isinstance(obj, dict):
        return False
    info = obj.get("info")
    if not isinstance(info, dict) or not str(info.get("id", "")).startswith("ses_"):
        return False
    return isinstance(obj.get("messages"), list)


def _opencode_part_time(part, fallback):
    """分片时间：优先 part.time.start / part.time.created，回退消息时间。"""
    t = part.get("time")
    if isinstance(t, dict):
        for key in ("start", "created"):
            if isinstance(t.get(key), (int, float)):
                return t[key]
    return fallback


def _opencode_tool_arg(tool_part):
    """opencode tool 分片 → 关键字段截断摘要。"""
    name = str(tool_part.get("tool", ""))
    state = tool_part.get("state") or {}
    args = state.get("input") or {}
    field = OPENCODE_ARG_FIELD_BY_TOOL.get(name)
    raw = args.get(field) if field else None
    if field == "todos" and isinstance(raw, list):
        raw = _todo_text([{"title": it.get("content", ""), "status": it.get("status", "")}
                          if isinstance(it, dict) else it for it in raw])
    if not isinstance(raw, str):
        raw = json.dumps(raw if raw is not None else args, ensure_ascii=False)
    return name, _truncate(raw)


def clean_opencode_records(doc, filters=None):
    """清洗 opencode 会话 dict（{info, messages}）→ (records, info)。

    与 clean_wire 同构：records 为轻量记录列表（agent 归属在外层加），
    info 含 created_at（会话创建毫秒时间戳）、dropped、dropped_other。
    """
    fl = {f["name"]: bool(f.get("enabled", True)) for f in (filters or FILTERS)}
    records = []
    info = {"created_at": None, "dropped": 0, "dropped_other": 0}
    messages = doc.get("messages") or []
    session_info = doc.get("info") or {}
    stime = session_info.get("time") or {}
    if isinstance(stime.get("created"), (int, float)):
        info["created_at"] = stime["created"]

    def drop():
        info["dropped"] += 1

    def emit(rec):
        records.append(rec)

    last_todo = None
    for msg in messages:
        if not isinstance(msg, dict):
            info["dropped_other"] += 1
            continue
        minfo = msg.get("info") or {}
        mtime = (minfo.get("time") or {}).get("created")
        role = minfo.get("role")
        parts = msg.get("parts") or []
        if role == "user":
            texts = [p.get("text", "") for p in parts
                     if isinstance(p, dict) and p.get("type") == "text"
                     and not p.get("synthetic")]
            text = "\n".join(t for t in texts if t)
            if text:
                emit({"k": "user", "time": mtime, "text": text})
            else:
                drop()
        elif role == "assistant":
            for p in parts:
                if not isinstance(p, dict):
                    info["dropped_other"] += 1
                    continue
                ptype = p.get("type")
                ts = _opencode_part_time(p, mtime)
                if ptype == "text":
                    if p.get("text"):
                        emit({"k": "assistant", "time": ts, "text": p["text"]})
                    else:
                        drop()
                elif ptype == "reasoning":
                    if fl.get("think", True):
                        drop()
                    else:
                        emit({"k": "assistant", "time": ts,
                              "text": p.get("text", ""), "think": True})
                elif ptype == "tool":
                    state = p.get("state") or {}
                    name, arg = _opencode_tool_arg(p)
                    out = state.get("output")
                    out_len = len(out) if isinstance(out, str) else \
                        (len(json.dumps(out, ensure_ascii=False))
                         if out is not None else 0)
                    emit({"k": "tool", "time": ts, "name": name, "arg": arg,
                          "err": state.get("status") == "error",
                          "out_len": out_len})
                    if name == "todowrite" and isinstance((state.get("input") or {}).get("todos"), list):
                        last_todo = {"time": ts, "todos": state["input"]["todos"]}
                else:
                    # step-start / step-finish / patch / summary 等一律丢弃
                    info["dropped_other"] += 1
        else:
            info["dropped_other"] += 1

    if last_todo is not None:
        emit({"k": "todo", "time": last_todo["time"],
              "text": _todo_text(
                  [{"title": it.get("content", ""), "status": it.get("status", "")}
                   if isinstance(it, dict) else it for it in last_todo["todos"]])})

    return records, info


def load_opencode_session(path):
    """读 opencode 会话 JSON 文件（导出或存储单文件）。非法/非 opencode 抛 ValueError。"""
    path = Path(path)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"会话文件不是合法 JSON：{exc}") from None
    if not looks_like_opencode(obj):
        raise ValueError("会话文件不是 opencode 会话格式（缺少 info/messages 结构）")
    return obj


def clean_opencode(json_path, out_dir, filters=None) -> dict:
    """清洗 opencode 会话 JSON → out_dir/cleaned.jsonl + cleaned.md。

    返回结构与 clean_export 完全一致。
    """
    json_path = Path(json_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_bytes = json_path.stat().st_size
    doc = load_opencode_session(json_path)
    records, info = clean_opencode_records(doc, filters)

    all_records = [{**rec, "agent": "main"} for rec in records]
    counts = {"user": 0, "assistant": 0, "tool": 0, "task": 0,
              "todo": 0, "compaction": 0, "raw": 0}
    times = []
    for rec in records:
        if rec["k"] in counts:
            counts[rec["k"]] += 1
        if isinstance(rec.get("time"), (int, float)):
            times.append(rec["time"])
    counts["total"] = len(records)
    totals = dict(counts)
    totals["agents"] = 1

    time_range = None
    if times:
        time_range = [_ms_to_iso(min(times)), _ms_to_iso(max(times))]

    stats = {
        "source": json_path.name,
        "src_bytes": src_bytes,
        "created_at": _ms_to_iso(info.get("created_at")),
        "time_range": time_range,
        "per_agent": {"main": counts},
        "agent_titles": {},
        **totals,
    }

    jsonl_path = out_dir / "cleaned.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as f:
        for rec in all_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    md = ["# 会话清洗报告", ""]
    md.append(f"- 会话文件：{json_path.name}")
    if stats["created_at"]:
        md.append(f"- 会话创建：{stats['created_at']}")
    md += [
        f"- user 条数：{totals['user']}",
        f"- assistant 段数：{totals['assistant']}",
        f"- tool 调用数：{totals['tool']}",
        f"- 后台任务数：{totals['task']}",
        f"- 时间范围：{' ~ '.join(time_range) if time_range else '未知'}",
        f"- 原始体积：{src_bytes} 字节 → 清洗后 "
        f"{jsonl_path.stat().st_size} 字节(jsonl)",
        "",
    ]
    md.append(_render_md(records, "main"))

    md_path = out_dir / "cleaned.md"
    md_path.write_text("\n".join(md), encoding="utf-8", newline="\n")

    stats["cleaned_jsonl_bytes"] = jsonl_path.stat().st_size
    stats["cleaned_md_bytes"] = md_path.stat().st_size
    return {
        "cleaned_jsonl": jsonl_path,
        "cleaned_md": md_path,
        "stats": stats,
    }


def detect_source(path) -> str:
    """按内容识别会话来源："kimi_zip" / "opencode_json" / "unknown"。"""
    path = Path(path)
    if not path.exists():
        return "unknown"
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            if any(n.startswith("agents/") and n.endswith("/wire.jsonl")
                   for n in names):
                return "kimi_zip"
        except (OSError, zipfile.BadZipFile):
            pass
        return "unknown"
    try:
        with path.open("rb") as f:
            head = f.read(64).lstrip()
    except OSError:
        return "unknown"
    if head.startswith(b"{"):
        try:
            looks_opencode = looks_like_opencode(json.loads(
                path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return "unknown"
        return "opencode_json" if looks_opencode else "unknown"
    return "unknown"


def clean_session(path, out_dir, filters=None) -> dict:
    """统一入口：自动识别 kimi zip / opencode JSON 并清洗。未知格式抛 ValueError。"""
    source = detect_source(path)
    if source == "kimi_zip":
        return clean_export(path, out_dir, filters)
    if source == "opencode_json":
        return clean_opencode(path, out_dir, filters)
    raise ValueError("无法识别的会话文件格式（既不是 kimi 导出 zip，"
                     "也不是 opencode 会话 JSON）")


def _render_md(records, agent, title=None):
    """单个 agent 的记录 → md 小节文本。"""
    out = [f"## {agent}" + (f" — {title}" if title else ""), ""]
    for rec in recs_with_iso(records):
        k = rec["k"]
        when = rec.get("_iso") or "?"
        if k == "user":
            out += [f"### 用户 · {when}", "", rec.get("text", ""), ""]
        elif k == "assistant":
            tag = "助手(思考)" if rec.get("think") else "助手"
            out += [f"**{tag} · {when}**", "", rec.get("text", ""), ""]
        elif k == "tool":
            flags = []
            if rec.get("err"):
                flags.append("错误")
            if rec.get("out_len") is not None:
                flags.append(f"输出{rec['out_len']}字符")
            suffix = f"（{'，'.join(flags)}）" if flags else ""
            out.append(f"- `{rec.get('name', '')}` · {when}：{rec.get('arg', '')}{suffix}")
        elif k == "task":
            out += [f"**后台任务 · {when}**", "", rec.get("text", ""), ""]
        elif k == "todo":
            out += [f"**Todo · {when}**", "", rec.get("text", ""), ""]
        elif k == "compaction":
            out += [f"**压缩摘要 · {when}**", "", rec.get("text", ""), ""]
        elif k == "raw":
            out.append(f"- (raw) `{rec.get('type', '')}` · {when}")
    out.append("")
    return "\n".join(out)


def recs_with_iso(records):
    """给记录临时附加 _iso（UTC ISO 时间）便于 md 渲染。"""
    out = []
    for rec in records:
        rec = dict(rec)
        rec["_iso"] = _ms_to_iso(rec.get("time"))
        out.append(rec)
    return out


def _wire_members(z):
    """zip 内各 agent 的 wire.jsonl 成员名，main 在前、其余按名排序。"""
    names = [n for n in z.namelist()
             if n.startswith("agents/") and n.endswith("/wire.jsonl")]
    agents = {n.split("/")[1] for n in names}
    ordered = ["main"] + sorted(a for a in agents if a != "main")
    return [(a, f"agents/{a}/wire.jsonl") for a in ordered
            if f"agents/{a}/wire.jsonl" in names]


def clean_export(zip_path: Path, out_dir: Path, filters=None) -> dict:
    """清洗 kimi export 会话 zip → out_dir/cleaned.jsonl + cleaned.md。

    返回 {"cleaned_jsonl", "cleaned_md", "stats"}；stats 含分 agent 与合计的
    user/assistant/tool 等计数、时间范围（ISO）、清洗前后体积。
    """
    zip_path = Path(zip_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_bytes = zip_path.stat().st_size

    per_agent = {}   # agent -> records
    created = None   # main 的 metadata.created_at
    agent_titles = {}  # agent_id -> main 中 Agent 调用的 description

    with zipfile.ZipFile(zip_path) as z:
        for agent, member in _wire_members(z):
            lines = [l for l in z.read(member).decode("utf-8").splitlines() if l.strip()]
            records, info = clean_wire(lines, filters)
            per_agent[agent] = records
            if agent == "main":
                created = info.get("created_at") or created
                # Agent 调用结果首行 agent_id 关联 subagent 标题
                desc_by_call = {}
                for line in lines:
                    rec = json.loads(line)
                    ev = rec.get("event") or {}
                    if rec.get("type") != "context.append_loop_event":
                        continue
                    if ev.get("type") == "tool.call" and ev.get("name") == "Agent":
                        desc = (ev.get("args") or {}).get("description", "")
                        if ev.get("toolCallId") is not None:
                            desc_by_call[ev["toolCallId"]] = desc
                    elif ev.get("type") == "tool.result":
                        aid = _agent_id_from_result(ev.get("result"))
                        if aid:
                            agent_titles[aid] = desc_by_call.get(
                                ev.get("toolCallId"), agent_titles.get(aid, ""))

    # 汇总记录（加 agent 字段）与统计
    all_records = []
    counts = {}
    times = []
    for agent, records in per_agent.items():
        c = {"user": 0, "assistant": 0, "tool": 0, "task": 0,
             "todo": 0, "compaction": 0, "raw": 0}
        for rec in records:
            all_records.append({**rec, "agent": agent})
            if rec["k"] in c:
                c[rec["k"]] += 1
            if isinstance(rec.get("time"), (int, float)):
                times.append(rec["time"])
        c["total"] = len(records)
        counts[agent] = c

    totals = {}
    for key in ("user", "assistant", "tool", "task", "todo", "compaction", "raw"):
        totals[key] = sum(c[key] for c in counts.values())
    totals["total"] = sum(c["total"] for c in counts.values())
    totals["agents"] = len(per_agent)

    time_range = None
    if times:
        time_range = [_ms_to_iso(min(times)), _ms_to_iso(max(times))]

    stats = {
        "source": zip_path.name,
        "src_bytes": src_bytes,
        "created_at": _ms_to_iso(created),
        "time_range": time_range,
        "per_agent": counts,
        "agent_titles": agent_titles,
        **totals,
    }

    # cleaned.jsonl
    jsonl_path = out_dir / "cleaned.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as f:
        for rec in all_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # cleaned.md：头部统计 + main 主体 + subagent 小节
    md = ["# 会话清洗报告", ""]
    md.append(f"- 会话文件：{zip_path.name}")
    if stats["created_at"]:
        md.append(f"- 会话创建：{stats['created_at']}")
    md += [
        f"- user 条数：{totals['user']}",
        f"- assistant 段数：{totals['assistant']}",
        f"- tool 调用数：{totals['tool']}",
        f"- 后台任务数：{totals['task']}",
        f"- 时间范围：{' ~ '.join(time_range) if time_range else '未知'}",
        f"- 原始体积：{src_bytes} 字节 → 清洗后 "
        f"{jsonl_path.stat().st_size} 字节(jsonl)",
        "",
    ]
    for agent, records in per_agent.items():
        title = agent_titles.get(agent) if agent != "main" else None
        md.append(_render_md(records, agent, title))

    md_path = out_dir / "cleaned.md"
    md_path.write_text("\n".join(md), encoding="utf-8", newline="\n")

    stats["cleaned_jsonl_bytes"] = jsonl_path.stat().st_size
    stats["cleaned_md_bytes"] = md_path.stat().st_size
    return {
        "cleaned_jsonl": jsonl_path,
        "cleaned_md": md_path,
        "stats": stats,
    }


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="清洗 kimi 导出 zip / opencode 会话 JSON（自动识别格式）")
    parser.add_argument("source", help="会话文件（kimi 导出 zip 或 opencode JSON）")
    parser.add_argument("out_dir", help="清洗输出目录")
    args = parser.parse_args()
    result = clean_session(Path(args.source), Path(args.out_dir))
    print(json.dumps(result["stats"], ensure_ascii=False, indent=2))
    print(f"jsonl: {result['cleaned_jsonl']}")
    print(f"md:    {result['cleaned_md']}")
    sys.exit(0)

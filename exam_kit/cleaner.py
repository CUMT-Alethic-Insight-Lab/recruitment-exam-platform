"""Kimi Code 会话导出清洗器（Python 3.12 纯标准库）。

把 `kimi export` 出的会话 zip（agents/main/wire.jsonl + agents/agent-N/wire.jsonl
+ blobs/ 等）清洗成评审用的轻量记录：丢弃 LLM 请求/用量/配置噪音与思考链，
保留用户输入、助手正文、工具调用摘要、后台任务、todo 与压缩摘要，供云端评审
「人类率」判读。接口：clean_export(zip_path, out_dir) -> dict，落盘 cleaned.jsonl、
cleaned.md 与 cleaned.html。命令行：python cleaner.py <zip> <out_dir>。

产物分工（M12）：cleaned.jsonl 是云端评审契约，字段语义不变（assistant 记录
尽量附带 "model" 可选键：kimi wire 取最近 llm.request / profile.bind 的
model/modelAlias，opencode 取消息 info.modelID，拿不到则省略该键）；cleaned.md
面向人读（分轮小节：👤 用户 / 🤖 助手 / 🔧 工具名）；cleaned.html 是自包含
可读预览页（内联样式 + 内联脚本，零外部依赖：正文经内置 markdown 子集渲染器
转成真正 HTML，模型仅在首现/切换时插分隔行，顶部「复制全部内容」按钮复制
从记录重建的纯文本版，全量转义防注入）。工具卡只保留名称+时间+参数一行
（JSON 摊平不带括号）+❌ 错误标记+「（输出 N 字符）」计数，**不渲染任何工具
输出内容**；搜索/检索/联网类工具（名字含 search/fetch/browse/web/datasource）
显式带出搜索关键词（args 的 query/keyword/keywords/q/search_terms，完整不
截断）。工具输出原文不进入任何产物：清洗时只派生错误标记/输出长度/图片标记，
原始输出随抛随弃；落盘 jsonl 前一律剥掉下划线前缀字段（_q 不上传）。

大文件（M19）：kimi zip 的 wire 成员流式逐行读取、tool.result 两遍流式预扫，
内存占用与单个 agent 的会话规模成正比，GB 级导出不会再把清洗进程拖垮；
opencode 单文件 JSON 受解析器限制仍需整读，超过 512MB 直接给出明确报错。

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
messages[i] = {info:{id,role,time,...}, parts:[...]}）同样清洗为
cleaned.jsonl/.md/.html：
- 映射：user 消息 text 分片 → user；assistant text 分片 → assistant；
  reasoning 分片 → 思考链（走 think 过滤规则）；tool 分片 → tool（state.status=error
  记错误）；todowrite 工具的最后一次输入另外落一条 todo；patch / step-start /
  step-finish / summary.diffs 等其余分片丢弃。
- 整个会话为单一 agent "main"（opencode 无 subagent wire）。
入口统一用 clean_session(path, out_dir)：按内容自动识别 kimi zip / opencode JSON。
"""

from __future__ import annotations

import html
import io
import json
import re
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

# 搜索/检索/联网获取类工具识别（渲染层把它们的参数行升级为「关键词」展示）：
# 工具名规范化（去 -_. 转小写）后命中任一子串即视为搜索类。
SEARCH_NAME_HINTS = ("search", "fetch", "browse", "web", "datasource")
# 名字本身不含上述子串的已知搜索工具，精确匹配兜底。
SEARCH_EXACT_NAMES = {"tavily"}
# 搜索类工具的 args 里按序探测的关键词字段。
QUERY_ARG_FIELDS = ("query", "keyword", "keywords", "q", "search_terms")

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


def _fmt_hms(ms):
    """epoch 毫秒 → 本地 HH:MM:SS；缺失/非法返回 '?'。"""
    if not isinstance(ms, (int, float)):
        return "?"
    return datetime.fromtimestamp(ms / 1000).astimezone().strftime("%H:%M:%S")


def _now_local_iso():
    """当前本地时间（含时区偏移）的秒级 ISO 字符串。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _one_line(text):
    """多行文本折成一行（工具参数摘要展示用）。"""
    return " / ".join(l.strip() for l in str(text).splitlines() if l.strip())


def _is_search_tool(name):
    """工具名是否属搜索/检索/联网获取类（去 -_. 后子串匹配 + 精确名兜底）。"""
    n = re.sub(r"[-_.]", "", str(name)).lower()
    if n in SEARCH_EXACT_NAMES:
        return True
    return any(h in n for h in SEARCH_NAME_HINTS)


def _search_query(args):
    """从搜索类工具的 args 提取关键词（query/keyword/keywords/q/search_terms）。"""
    if not isinstance(args, dict):
        return None
    for field in QUERY_ARG_FIELDS:
        v = args.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _jsonl_rec(rec):
    """落盘 jsonl 前剥掉下划线前缀的内部字段（如 _out），保持云端契约不变。"""
    return {k: v for k, v in rec.items() if not k.startswith("_")}


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


def _tool_arg(call, image=False):
    """提取工具调用关键字段并截断；图片输出追加 [图片] 标记。"""
    name = call.get("name", "")
    args = call.get("args") or {}
    field = ARG_FIELD_BY_TOOL.get(name)
    raw = args.get(field) if field else None
    if not isinstance(raw, str):
        raw = json.dumps(raw if raw is not None else args, ensure_ascii=False)
    arg = _truncate(raw)
    if image:
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


def _derive_tool_info(result):
    """tool.result → 只保留清洗需要的派生字段（err/out_len/image/agent_id）。

    原始 result（含完整工具输出）随抛随弃，不在内存里驻留——大会话导出里
    工具输出占绝大部分体积，全留 raw result 会把机器拖垮。"""
    if result is None:
        return {"err": False, "out_len": None, "image": False, "agent_id": None}
    return {"err": bool(result.get("isError")),
            "out_len": _output_len(result),
            "image": _has_image_output(result),
            "agent_id": _agent_id_from_result(result)}


def _derive_tool_info_from_lines(lines):
    """预扫 JSONL 行：toolCallId → 派生工具信息（tool 记录在 call 位置输出，
    需先拿到配对结果）。子串预过滤，只 parse 可能是 tool.result 的行。"""
    results = {}
    for line in lines:
        if '"tool.result"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        ev = rec.get("event") or {}
        if rec.get("type") == "context.append_loop_event" \
                and ev.get("type") == "tool.result":
            results[ev.get("toolCallId")] = _derive_tool_info(ev.get("result"))
    return results


def clean_wire(lines, filters=None, tool_info=None, agent_titles=None):
    """清洗单个 wire 的 JSONL 行（列表或可迭代流）→ 轻量记录列表。

    tool_info：toolCallId → 派生工具信息（_derive_tool_info_from_lines），
    不传则内部自行预扫。agent_titles：传入 dict 时顺带提取 main 的 Agent
    调用标题（agent_id → description）。
    返回 (records, info)：info 含 created_at（metadata）、dropped 计数、
    dropped_other（未识别类型的丢弃数）。
    """
    fl = {f["name"]: bool(f.get("enabled", True)) for f in (filters or FILTERS)}

    def dropped(name):
        return fl.get(name, True)

    if tool_info is None:
        tool_info = _derive_tool_info_from_lines(lines)

    records = []
    info = {"created_at": None, "dropped": 0, "dropped_other": 0}

    def drop():
        info["dropped"] += 1

    def emit(rec):
        records.append(rec)

    last_todo = None
    current_model = None  # 最近 llm.request / profile.bind 的模型，附到 assistant 记录
    call_ids = []         # (time, tool.call event)，配对结果后统一在末尾输出
    desc_by_call = {}     # toolCallId -> Agent 调用 description（提取子代理标题用）
    for line in lines:
        rec = json.loads(line)
        rtype = rec.get("type")
        ts = rec.get("time")

        if rtype == "metadata":
            info["created_at"] = rec.get("created_at")
            continue

        if rtype in ("llm.request", "profile.bind"):
            m = rec.get("model") or rec.get("modelAlias")
            if isinstance(m, str) and m.strip():
                current_model = m.strip()

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
                        out = {"k": "assistant", "time": ts,
                               "text": part.get("think", ""), "think": True}
                        if current_model:
                            out["model"] = current_model
                        emit(out)
                elif ptype == "text":
                    out = {"k": "assistant", "time": ts,
                           "text": part.get("text", "")}
                    if current_model:
                        out["model"] = current_model
                    emit(out)
                else:
                    info["dropped_other"] += 1
            elif et == "tool.call":
                call_ids.append((ts, ev))
                if agent_titles is not None and ev.get("name") == "Agent":
                    desc_by_call[ev.get("toolCallId")] = \
                        (ev.get("args") or {}).get("description", "")
            elif et == "tool.result":
                pass  # 结果已预扫配对，见下方统一输出
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
    for ts, call in call_ids:
        tcid = call.get("toolCallId")
        tinfo = tool_info.get(tcid) or {}
        rec = {"k": "tool", "time": ts, "name": call.get("name", ""),
               "arg": _tool_arg(call, image=bool(tinfo.get("image"))),
               "err": bool(tinfo.get("err")),
               "out_len": tinfo.get("out_len")}
        if _is_search_tool(call.get("name")):
            q = _search_query(call.get("args"))
            if q:
                rec["_q"] = q
                rec["arg"] = q  # 搜索关键词入公开 arg：jsonl 落盘保留，云端评审可见
        emit(rec)
        if agent_titles is not None:
            aid = tinfo.get("agent_id")
            if aid:
                agent_titles[aid] = desc_by_call.get(
                    tcid, agent_titles.get(aid, ""))

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
            model = minfo.get("modelID")
            model = model.strip() if isinstance(model, str) and model.strip() \
                else None
            for p in parts:
                if not isinstance(p, dict):
                    info["dropped_other"] += 1
                    continue
                ptype = p.get("type")
                ts = _opencode_part_time(p, mtime)
                if ptype == "text":
                    if p.get("text"):
                        out = {"k": "assistant", "time": ts, "text": p["text"]}
                        if model:
                            out["model"] = model
                        emit(out)
                    else:
                        drop()
                elif ptype == "reasoning":
                    if fl.get("think", True):
                        drop()
                    else:
                        out = {"k": "assistant", "time": ts,
                               "text": p.get("text", ""), "think": True}
                        if model:
                            out["model"] = model
                        emit(out)
                elif ptype == "tool":
                    state = p.get("state") or {}
                    name, arg = _opencode_tool_arg(p)
                    out = state.get("output")
                    out_text = out if isinstance(out, str) else \
                        (json.dumps(out, ensure_ascii=False)
                         if out is not None else "")
                    rec = {"k": "tool", "time": ts, "name": name, "arg": arg,
                           "err": state.get("status") == "error",
                           "out_len": len(out_text)}
                    if _is_search_tool(name):
                        q = _search_query(state.get("input"))
                        if q:
                            rec["_q"] = q
                            rec["arg"] = q  # 搜索关键词入公开 arg：jsonl 落盘保留，云端评审可见
                    emit(rec)
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


OPENCODE_MAX_BYTES = 512 * 1024 * 1024  # opencode 单文件 JSON 只能整读解析，超限给明确报错


def load_opencode_session(path):
    """读 opencode 会话 JSON 文件（导出或存储单文件）。非法/非 opencode 抛 ValueError。"""
    path = Path(path)
    if path.stat().st_size > OPENCODE_MAX_BYTES:
        raise ValueError(
            "会话 JSON 超过 512MB，单文件 JSON 解析会把内存撑爆——"
            "请改用 Kimi Code 的 zip 导出（/export-debug-zip 或 kimi export）")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"会话文件不是合法 JSON：{exc}") from None
    if not looks_like_opencode(obj):
        raise ValueError("会话文件不是 opencode 会话格式（缺少 info/messages 结构）")
    return obj


def clean_opencode(json_path, out_dir, filters=None) -> dict:
    """清洗 opencode 会话 JSON → out_dir/cleaned.jsonl + cleaned.md + cleaned.html。

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

    jsonl_path, md_path, html_path = _write_outputs(
        out_dir, "opencode 会话 JSON", stats, {"main": records}, {},
        all_records)
    return {
        "cleaned_jsonl": jsonl_path,
        "cleaned_md": md_path,
        "cleaned_html": html_path,
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
        # 大文件不整读解析（会把内存撑爆）：只嗅探前 1MB 的结构特征，
        # 判断为 opencode 后由 load_opencode_session 的体积护栏给明确报错
        if path.stat().st_size > 64 * 1024 * 1024:
            with path.open("rb") as f:
                sniff = f.read(1024 * 1024)
            return "opencode_json" if b'"messages"' in sniff and b'"ses_"' in sniff \
                else "unknown"
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


# ---------- 清洗产物渲染（cleaned.md / cleaned.html） ----------

def _md_code(text):
    """md 行内代码：含反引号时改用双反引号包裹。"""
    text = str(text)
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _render_turns_md(records):
    """一个 agent 的记录列表 → 分轮 md 小节（👤 用户 / 🤖 助手 / 🔧 工具…）。

    工具卡只保留名称+参数一行+❌ 标记+输出字符数，不渲染任何输出内容；
    搜索类工具显式带出关键词。
    """
    out = []
    for rec in records:
        k = rec["k"]
        when = _fmt_hms(rec.get("time"))
        if k == "user":
            out += [f"## 👤 用户（{when}）", "", rec.get("text", ""), ""]
        elif k == "assistant":
            label = "🤖 助手·思考" if rec.get("think") else "🤖 助手"
            out += [f"## {label}（{when}）", "", rec.get("text", ""), ""]
        elif k == "tool":
            name = str(rec.get("name", ""))
            if _is_search_tool(name):
                out += [f"## 🔍 搜索：{name}（{when}）", ""]
            else:
                out += [f"## 🔧 工具：{name}（{when}）", ""]
            q = rec.get("_q")
            if q:
                out += [f"**关键词**：{_md_code(_one_line(q))}", ""]
            else:
                arg = _one_line(_arg_display(rec.get("arg", "")))
                if arg:
                    out += [f"**参数**：{_md_code(arg)}", ""]
            if rec.get("err"):
                out += ["**❌ 错误**", ""]
            out_len = rec.get("out_len")
            if isinstance(out_len, (int, float)):
                out += [f"（输出 {out_len} 字符）", ""]
        elif k == "task":
            out += [f"## 🧵 后台任务（{when}）", "", rec.get("text", ""), ""]
        elif k == "todo":
            out += [f"## ✅ Todo（{when}）", "", rec.get("text", ""), ""]
        elif k == "compaction":
            out += [f"## 🗜️ 压缩摘要（{when}）", "", rec.get("text", ""), ""]
        elif k == "raw":
            out += [f"## 📄 未过滤记录：{rec.get('type', '')}（{when}）", ""]
    return out


def _header_lines(source_label, stats, jsonl_bytes):
    """产物开头标题块：会话来源 / 清洗时间 / 轮次统计等（md 与 html 共用数据）。"""
    tr = stats.get("time_range")
    lines = [
        ("会话来源", source_label),
        ("会话文件", str(stats.get("source", ""))),
        ("清洗时间", f"{_now_local_iso()}（本地）"),
    ]
    if stats.get("created_at"):
        lines.append(("会话创建", f"{stats['created_at']}（UTC）"))
    lines += [
        ("轮次统计", f"👤 用户 {stats['user']} 条 · "
                    f"🤖 助手 {stats['assistant']} 段 · "
                    f"🔧 工具 {stats['tool']} 次"),
        ("其他记录", f"后台任务 {stats['task']} · Todo {stats['todo']} · "
                    f"压缩摘要 {stats['compaction']}"),
        ("时间范围", f"{' ~ '.join(tr) if tr else '未知'}（UTC）"),
        ("原始体积", f"{stats['src_bytes']} 字节 → 清洗后 {jsonl_bytes} 字节(jsonl)"),
    ]
    return lines


def _render_md_document(source_label, stats, per_agent, agent_titles,
                        jsonl_bytes):
    """拼装完整 cleaned.md 文本；多 agent 时插 🧩 代理分节。"""
    md = ["# 会话清洗报告", ""]
    md += [f"- {k}：{v}" for k, v in
           _header_lines(source_label, stats, jsonl_bytes)]
    md.append("")
    multi = len(per_agent) > 1
    for agent, records in per_agent.items():
        if multi:
            title = agent_titles.get(agent) if agent != "main" else None
            md += [f"## 🧩 代理：{agent}" + (f" — {title}" if title else ""), ""]
        md += _render_turns_md(records)
    return "\n".join(md).rstrip() + "\n"


# ---------- markdown 子集渲染（纯 Python：先整体转义，再渲染，防注入） ----------

_MD_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_MD_ITALIC_RE = re.compile(r"\*([^*\n]+)\*")
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.*?)\s*#*\s*$")
_MD_HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_MD_UL_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_MD_OL_RE = re.compile(r"^\s{0,3}\d+[.)]\s+(.*)$")
_MD_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")
_MD_TABLE_SEP_CELL_RE = re.compile(r":?-+:?$")


def _md_table_cells(line):
    """pipe 表格行 → 单元格列表（去首尾 |，按 | 切分，各格 strip）。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _is_md_table_sep(line):
    """是否表格分隔行（每格形如 --- / :--- / ---: / :---:）。"""
    cells = _md_table_cells(line)
    return bool(cells) and all(
        _MD_TABLE_SEP_CELL_RE.fullmatch(c) for c in cells)


def _md_table_align(cell):
    """分隔行单元格 → 列对齐（left/center/right/''）。"""
    if cell.startswith(":") and cell.endswith(":"):
        return "center"
    if cell.endswith(":"):
        return "right"
    if cell.startswith(":"):
        return "left"
    return ""


def _md_inline(text):
    """行内渲染：**加粗** / *斜体* / `行内代码` / [文字](http链接)。"""
    esc = html.escape(text)
    codes = []

    def _stash_code(m):
        codes.append(m.group(1))
        return "\x00%d\x00" % (len(codes) - 1)

    esc = _MD_CODE_SPAN_RE.sub(_stash_code, esc)
    esc = _MD_LINK_RE.sub(
        lambda m: '<a href="%s" target="_blank" rel="noopener">%s</a>'
                  % (m.group(2), m.group(1)), esc)
    esc = _MD_BOLD_RE.sub(r"<strong>\1</strong>", esc)
    esc = _MD_ITALIC_RE.sub(r"<em>\1</em>", esc)
    return _MD_PLACEHOLDER_RE.sub(
        lambda m: "<code>%s</code>" % codes[int(m.group(1))], esc)


def render_markdown(text):
    """markdown 子集 → HTML 字符串（块间无多余空白，配合 .body 的 pre-wrap）。

    支持：#~#### 标题（映射 h2~h5）、``` 围栏代码块（横向滚动）、-/*/+ 无序
    列表、1. 有序列表、> 引用块、---/*** 分割线、GFM pipe 表格（含 :--- 对齐；
    与 GFM 一致不打断段落）、链接与行内样式；段落内单个换行渲染为 <br>。
    输入先全量 HTML 转义，链接仅放行 http(s)。
    """
    lines = str(text).splitlines()
    out = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.strip().startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            if i < n:
                i += 1  # 跳过收尾围栏（缺失则到文末）
            out.append('<pre class="code"><code>%s</code></pre>'
                       % html.escape("\n".join(buf)))
            continue
        m = _MD_HEADING_RE.match(line)
        if m:
            level = len(m.group(1)) + 1
            out.append("<h%d>%s</h%d>" % (level, _md_inline(m.group(2)), level))
            i += 1
            continue
        if _MD_HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue
        if line.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                q = lines[i].lstrip()[1:]
                buf.append(q[1:] if q.startswith(" ") else q)
                i += 1
            inner = "<br>".join(_md_inline(b) for b in buf if b.strip())
            out.append("<blockquote>%s</blockquote>" % inner)
            continue
        m = _MD_UL_RE.match(line)
        if m:
            items = []
            while i < n:
                mm = _MD_UL_RE.match(lines[i])
                if not mm:
                    break
                items.append(mm.group(1))
                i += 1
            out.append("<ul>%s</ul>" % "".join(
                "<li>%s</li>" % _md_inline(it) for it in items))
            continue
        m = _MD_OL_RE.match(line)
        if m:
            items = []
            while i < n:
                mm = _MD_OL_RE.match(lines[i])
                if not mm:
                    break
                items.append(mm.group(1))
                i += 1
            out.append("<ol>%s</ol>" % "".join(
                "<li>%s</li>" % _md_inline(it) for it in items))
            continue
        if "|" in line and i + 1 < n and _is_md_table_sep(lines[i + 1]):
            headers = _md_table_cells(line)
            aligns = [_md_table_align(c) for c in _md_table_cells(lines[i + 1])]
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_md_table_cells(lines[i]))
                i += 1
            th = "".join(
                "<th%s>%s</th>" % (
                    ' style="text-align:%s"' % aligns[j] if aligns[j] else "",
                    _md_inline(h))
                for j, h in enumerate(headers))
            trs = "".join(
                "<tr>%s</tr>" % "".join(
                    "<td%s>%s</td>" % (
                        ' style="text-align:%s"' % aligns[j] if aligns[j] else "",
                        _md_inline(r[j]) if j < len(r) else "")
                    for j in range(len(headers)))
                for r in rows)
            out.append("<table><thead><tr>%s</tr></thead>"
                       "<tbody>%s</tbody></table>" % (th, trs))
            continue
        buf = []
        while i < n:
            l = lines[i]
            if not l.strip():
                break
            if buf and (l.strip().startswith("```")
                        or _MD_HEADING_RE.match(l) or _MD_HR_RE.match(l)
                        or l.lstrip().startswith(">")
                        or _MD_UL_RE.match(l) or _MD_OL_RE.match(l)):
                break
            buf.append(l)
            i += 1
        out.append("<p>%s</p>" % "<br>".join(_md_inline(b) for b in buf))
    return "".join(out)


def _arg_display(arg):
    """工具参数展示：JSON 对象/数组摊平为「键: 值」纯文本，避免括号糊脸。

    参数在记录层已按 160 字截断，截断后的 JSON 无法 parse，此时按疑似 JSON
    （以 {" 或 [" 开头）做宽松清理：去 {}[]" 结构符号、冒号逗号中文化，
    保留可读文本；shell 命令等合法含括号内容不动。
    """
    s = str(arg)
    t = s.strip()
    if t[:1] in ("{", "[") and t[-1:] in ("}", "]"):
        try:
            obj = json.loads(t)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            flat = []
            for k, v in obj.items():
                if not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False)
                flat.append(f"{k}: {v}")
            return "；".join(flat)
        if isinstance(obj, list):
            return "、".join(
                v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                for v in obj)
        if obj is not None:
            return s
    if re.match(r'^[\{\[]\s*"', t):
        cleaned = re.sub(r'[\{\}\[\]"]', "", t)
        cleaned = re.sub(r"\s*:\s*", "：", cleaned)
        cleaned = re.sub(r"\s*,\s*", "；", cleaned)
        return cleaned
    return s


_HTML_CSS = """\
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #f4f5f7; color: #24292f;
       font-family: "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
                    "Segoe UI", sans-serif; }
header { background: #1f2937; color: #f9fafb; padding: 20px 28px; }
header h1 { margin: 0 0 10px; font-size: 20px; }
header ul { margin: 0; padding-left: 1.2em; font-size: 13.5px; line-height: 1.8; }
#copyAll { margin-top: 12px; background: #2563eb; color: #fff; border: none;
           border-radius: 6px; padding: 9px 18px; font-size: 14px;
           cursor: pointer; }
#copyAll:hover { background: #1d4ed8; }
main { max-width: 960px; margin: 20px auto 40px; padding: 0 16px; }
.turn { background: #fff; border-radius: 8px; border-left: 5px solid #9ca3af;
        padding: 12px 16px; margin: 12px 0; box-shadow: 0 1px 2px rgba(0,0,0,.06); }
.turn.user { border-left-color: #2563eb; }
.turn.assistant { border-left-color: #9ca3af; }
.turn.tool { border-left-color: #d97706; }
.turn.tool.error { border-left-color: #dc2626; background: #fef2f2; }
.meta { font-size: 13px; font-weight: 600; color: #4b5563; margin-bottom: 6px; }
.body { font-size: 14.5px; line-height: 1.7;
        white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; }
.body p { margin: .45em 0; }
.body h2, .body h3, .body h4, .body h5 { margin: .7em 0 .35em; line-height: 1.4; }
.body h2 { font-size: 17px; }
.body h3 { font-size: 16px; }
.body h4, .body h5 { font-size: 15px; }
.body code { background: #f3f4f6; border-radius: 4px; padding: 1px 5px;
             font-family: Consolas, "Courier New", monospace; font-size: 13px; }
.body pre.code { background: #f8fafc; border: 1px solid #e5e7eb;
                 border-radius: 6px; padding: 10px; overflow-x: auto;
                 white-space: pre; overflow-wrap: normal; word-break: normal;
                 font-size: 12.5px; line-height: 1.55; }
.body pre.code code { background: transparent; padding: 0; font-size: inherit; }
.body blockquote { margin: .4em 0; padding: 2px 12px; color: #4b5563;
                   border-left: 3px solid #d1d5db; background: #f9fafb; }
.body ul, .body ol { margin: .4em 0; padding-left: 1.7em; }
.body li { margin: .15em 0; }
.body hr { border: none; border-top: 1px solid #e5e7eb; margin: .8em 0; }
.body table { border-collapse: collapse; margin: .5em 0; font-size: 13.5px; }
.body th, .body td { border: 1px solid #d1d5db; padding: 4px 10px; }
.body th { background: #f3f4f6; }
.body a { color: #2563eb; }
.model-div { margin: 16px 0 2px; text-align: center; color: #6b7280;
             font-size: 12.5px; letter-spacing: .5px; }
.arg { font-size: 13.5px; margin: 2px 0; }
.arg code { background: #f3f4f6; border-radius: 4px; padding: 2px 6px;
            overflow-wrap: anywhere; word-break: break-word; }
.err { color: #dc2626; font-weight: 700; margin: 4px 0; }
.outlen { font-size: 12px; color: #9ca3af; margin-top: 4px; }
.agent { margin: 26px 0 6px; padding: 8px 12px; background: #e5e7eb;
         border-radius: 6px; font-size: 14px; font-weight: 700; }
footer { text-align: center; color: #9ca3af; font-size: 12px; padding: 18px; }
"""

# 复制按钮脚本：TEXT 为渲染前记录重建的纯文本（json.dumps 嵌入，不依赖 DOM）；
# navigator.clipboard 不可用（如 file:// 旧内核）时退化为 textarea+execCommand。
_COPY_JS = """\
(function(){
var text=%s;
var btn=document.getElementById("copyAll");
function ok(){btn.textContent="✅ 已复制";setTimeout(function(){btn.textContent="📋 复制全部内容";},2000);}
function fb(){var ta=document.createElement("textarea");ta.value=text;ta.style.position="fixed";ta.style.opacity="0";document.body.appendChild(ta);ta.select();try{document.execCommand("copy");ok();}catch(e){btn.textContent="复制失败，请手动复制";}document.body.removeChild(ta);}
btn.addEventListener("click",function(){if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(ok,fb);}else{fb();}});
})();
"""


def _turn_html(rec):
    """单条记录 → 轮次卡片 html（正文经 markdown 子集渲染，输出全量转义）。"""
    esc = html.escape
    k = rec["k"]
    when = _fmt_hms(rec.get("time"))
    if k in ("user", "assistant"):
        if k == "user":
            cls, label = "turn user", "👤 用户"
        else:
            cls = "turn assistant"
            label = "🤖 助手·思考" if rec.get("think") else "🤖 助手"
        return (f'<section class="{cls}"><div class="meta">{label} · {when}</div>'
                f'<div class="body">{render_markdown(rec.get("text", ""))}</div>'
                f'</section>')
    if k == "tool":
        name = str(rec.get("name", ""))
        search = _is_search_tool(name)
        cls = "turn tool error" if rec.get("err") else "turn tool"
        title = f"🔍 搜索 · {esc(name)}" if search else f"🔧 {esc(name)}"
        parts = [f'<section class="{cls}"><div class="meta">{title} · '
                 f'{when}</div>']
        q = rec.get("_q")
        if q:
            parts.append(f'<div class="arg">关键词：<code>'
                         f'{esc(_one_line(q))}</code></div>')
        else:
            arg = _one_line(_arg_display(rec.get("arg", "")))
            if arg:
                parts.append(f'<div class="arg">参数：<code>{esc(arg)}</code></div>')
        if rec.get("err"):
            parts.append('<div class="err">❌ 错误</div>')
        out_len = rec.get("out_len")
        if isinstance(out_len, (int, float)):
            parts.append(f'<div class="outlen">（输出 {out_len} 字符）</div>')
        parts.append("</section>")
        return "".join(parts)
    label_by_k = {"task": "🧵 后台任务", "todo": "✅ Todo",
                  "compaction": "🗜️ 压缩摘要"}
    if k in label_by_k:
        return (f'<section class="turn {k}"><div class="meta">'
                f'{label_by_k[k]} · {when}</div>'
                f'<div class="body">{render_markdown(rec.get("text", ""))}</div>'
                f'</section>')
    # raw：过滤器放宽时登记的原始类型
    return (f'<section class="turn raw"><div class="meta">📄 未过滤记录：'
            f'{esc(str(rec.get("type", "")))} · {when}</div></section>')


def _render_plain_document(source_label, stats, per_agent, agent_titles,
                           jsonl_bytes):
    """复制按钮用的纯文本版：从记录重建（标题块 + 轮次分隔 + 模型分隔行）。"""
    sep = "─" * 32
    lines = ["会话清洗预览", ""]
    lines += [f"{k}：{v}" for k, v in
              _header_lines(source_label, stats, jsonl_bytes)]
    lines.append("")
    multi = len(per_agent) > 1
    for agent, records in per_agent.items():
        if multi:
            title = agent_titles.get(agent) if agent != "main" else None
            lines += [f"🧩 代理：{agent}" + (f" — {title}" if title else ""), ""]
        last_model = None
        for rec in records:
            k = rec["k"]
            when = _fmt_hms(rec.get("time"))
            body = []
            if k == "user":
                head = f"👤 用户（{when}）"
                body = [rec.get("text", "")]
            elif k == "assistant":
                label = "🤖 助手·思考" if rec.get("think") else "🤖 助手"
                head = f"{label}（{when}）"
                model = rec.get("model")
                if model and model != last_model:
                    last_model = model
                    lines += [f"── 模型：{model} ──", ""]
                body = [rec.get("text", "")]
            elif k == "tool":
                name = str(rec.get("name", ""))
                if _is_search_tool(name):
                    head = f"🔍 搜索：{name}（{when}）"
                else:
                    head = f"🔧 {name}（{when}）"
                q = rec.get("_q")
                if q:
                    body.append(f"关键词：{_one_line(q)}")
                else:
                    arg = _one_line(_arg_display(rec.get("arg", "")))
                    if arg:
                        body.append(f"参数：{arg}")
                if rec.get("err"):
                    body.append("❌ 错误")
                out_len = rec.get("out_len")
                if isinstance(out_len, (int, float)):
                    body.append(f"（输出 {out_len} 字符）")
            elif k in ("task", "todo", "compaction"):
                label = {"task": "🧵 后台任务", "todo": "✅ Todo",
                         "compaction": "🗜️ 压缩摘要"}[k]
                head = f"{label}（{when}）"
                body = [rec.get("text", "")]
            else:
                head = f"📄 未过滤记录：{rec.get('type', '')}（{when}）"
            lines += [sep, head, ""] + body + [""]
    return "\n".join(lines).rstrip() + "\n"


def _render_html_document(source_label, stats, per_agent, agent_titles,
                          jsonl_bytes):
    """拼装自包含 cleaned.html（内联样式 + 内联脚本，零外部依赖，断网可开）。"""
    esc = html.escape
    plain = _render_plain_document(source_label, stats, per_agent,
                                   agent_titles, jsonl_bytes)
    js_text = json.dumps(plain, ensure_ascii=False).replace("<", "\\u003c")
    parts = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>会话清洗预览 - {esc(str(stats.get('source', '')))}</title>",
        "<style>",
        _HTML_CSS,
        "</style>",
        "</head>",
        "<body>",
        "<header>",
        "<h1>会话清洗预览</h1>",
        "<ul>",
    ]
    parts += [f"<li>{k}：{esc(v)}</li>"
              for k, v in _header_lines(source_label, stats, jsonl_bytes)]
    parts += ["</ul>",
              '<button id="copyAll" type="button">📋 复制全部内容</button>',
              "</header>", "<main>"]
    multi = len(per_agent) > 1
    for agent, records in per_agent.items():
        if multi:
            title = agent_titles.get(agent) if agent != "main" else None
            head = f"🧩 代理：{agent}" + (f" — {title}" if title else "")
            parts.append(f'<div class="agent">{esc(head)}</div>')
        last_model = None
        for rec in records:
            if rec["k"] == "assistant":
                model = rec.get("model")
                if model and model != last_model:
                    last_model = model
                    parts.append(f'<div class="model-div">── 模型：'
                                 f'{esc(str(model))} ──</div>')
            parts.append(_turn_html(rec))
    parts += [
        "</main>",
        "<footer>本文件由本地清洗器生成，仅用于上传前预览；"
        "正式评审以上传的 cleaned.jsonl 为准。</footer>",
        "<script>",
        _COPY_JS % js_text,
        "</script>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(parts)


def _write_outputs(out_dir, source_label, stats, per_agent, agent_titles,
                   all_records):
    """统一落盘 cleaned.jsonl/.md/.html，返回三个路径。"""
    out_dir = Path(out_dir)
    jsonl_path = out_dir / "cleaned.jsonl"
    with jsonl_path.open("w", encoding="utf-8", newline="\n") as f:
        for rec in all_records:
            f.write(json.dumps(_jsonl_rec(rec), ensure_ascii=False) + "\n")
    jsonl_bytes = jsonl_path.stat().st_size

    md_path = out_dir / "cleaned.md"
    md_path.write_text(
        _render_md_document(source_label, stats, per_agent, agent_titles,
                            jsonl_bytes),
        encoding="utf-8", newline="\n")

    html_path = out_dir / "cleaned.html"
    html_path.write_text(
        _render_html_document(source_label, stats, per_agent, agent_titles,
                              jsonl_bytes),
        encoding="utf-8", newline="\n")

    stats["cleaned_jsonl_bytes"] = jsonl_bytes
    stats["cleaned_md_bytes"] = md_path.stat().st_size
    stats["cleaned_html_bytes"] = html_path.stat().st_size
    return jsonl_path, md_path, html_path


def _wire_members(z):
    """zip 内各 agent 的 wire.jsonl 成员名，main 在前、其余按名排序。"""
    names = [n for n in z.namelist()
             if n.startswith("agents/") and n.endswith("/wire.jsonl")]
    agents = {n.split("/")[1] for n in names}
    ordered = ["main"] + sorted(a for a in agents if a != "main")
    return [(a, f"agents/{a}/wire.jsonl") for a in ordered
            if f"agents/{a}/wire.jsonl" in names]


def _iter_member_lines(z, member):
    """流式逐行读 zip 成员（不整读进内存），跳过空行。"""
    with z.open(member) as f:
        for line in io.TextIOWrapper(f, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line:
                yield line


def clean_export(zip_path: Path, out_dir: Path, filters=None) -> dict:
    """清洗 kimi export 会话 zip → out_dir/cleaned.jsonl + cleaned.md + cleaned.html。

    返回 {"cleaned_jsonl", "cleaned_md", "cleaned_html", "stats"}；stats 含分 agent
    与合计的 user/assistant/tool 等计数、时间范围（ISO）、清洗前后体积。

    大文件友好：wire 成员流式逐行读（不整读）；tool.result 只留派生字段，
    原始输出随抛随弃；每个 wire 两遍流（预扫工具结果 + 主清洗），
    内存占用与单个 agent 的会话规模成正比，而不是整个 zip。"""
    zip_path = Path(zip_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    src_bytes = zip_path.stat().st_size

    per_agent = {}   # agent -> records
    created = None   # main 的 metadata.created_at
    agent_titles = {}  # agent_id -> main 中 Agent 调用的 description

    with zipfile.ZipFile(zip_path) as z:
        for agent, member in _wire_members(z):
            tool_info = _derive_tool_info_from_lines(
                _iter_member_lines(z, member))
            records, info = clean_wire(
                _iter_member_lines(z, member), filters,
                tool_info=tool_info,
                agent_titles=agent_titles if agent == "main" else None)
            per_agent[agent] = records
            if agent == "main":
                created = info.get("created_at") or created

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

    # cleaned.jsonl / cleaned.md / cleaned.html
    jsonl_path, md_path, html_path = _write_outputs(
        out_dir, "kimi 导出 zip", stats, per_agent, agent_titles, all_records)
    return {
        "cleaned_jsonl": jsonl_path,
        "cleaned_md": md_path,
        "cleaned_html": html_path,
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
    print(f"html:  {result['cleaned_html']}")
    sys.exit(0)

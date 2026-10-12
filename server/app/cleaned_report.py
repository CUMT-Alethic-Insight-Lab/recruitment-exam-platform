"""cleaned.jsonl → Markdown 报告转换（纯标准库）。

管理端「下载 MD」的服务端实现：把存储在云端的 cleaned.jsonl（NDJSON，
每行 {k, time, ...}）重建为人读的 Markdown，与 exam_kit/cleaner.py 生成的
cleaned.md 同构（👤 用户 / 🤖 助手 / 🔧 工具分轮小节 + 🧩 代理分节）。
上传的 jsonl 已剥掉下划线内部字段（无 _q），搜索类工具的关键词从 arg 读取。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

# 搜索/检索/联网获取类工具识别（与 exam_kit/cleaner.py 规则一致）：
# 工具名规范化（去 -_. 转小写）后命中任一子串即视为搜索类。
SEARCH_NAME_HINTS = ("search", "fetch", "browse", "web", "datasource")
SEARCH_EXACT_NAMES = {"tavily"}


def load_records(path) -> list[dict]:
    """逐行解析 cleaned.jsonl，跳过空行与坏行。"""
    records = []
    with Path(path).open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("k"):
                records.append(rec)
    return records


def _ms_to_iso(ms):
    """epoch 毫秒 → UTC ISO 字符串；缺失/非法返回 None。"""
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _fmt_hms(ms):
    """epoch 毫秒 → 本地 HH:MM:SS；缺失/非法返回 '?'。"""
    if not isinstance(ms, (int, float)):
        return "?"
    return datetime.fromtimestamp(ms / 1000).astimezone().strftime("%H:%M:%S")


def _one_line(text):
    """多行文本折成一行（工具参数摘要展示用）。"""
    return " / ".join(l.strip() for l in str(text).splitlines() if l.strip())


def _md_code(text):
    """md 行内代码：含反引号时改用双反引号包裹。"""
    text = str(text)
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _is_search_tool(name):
    """工具名是否属搜索/检索/联网获取类（去 -_. 后子串匹配 + 精确名兜底）。"""
    n = re.sub(r"[-_.]", "", str(name)).lower()
    if n in SEARCH_EXACT_NAMES:
        return True
    return any(h in n for h in SEARCH_NAME_HINTS)


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


def _render_turns_md(records):
    """一个 agent 的记录列表 → 分轮 md 小节（👤 用户 / 🤖 助手 / 🔧 工具…）。"""
    out = []
    for rec in records:
        k = rec["k"]
        when = _fmt_hms(rec.get("time"))
        if k == "user":
            out += [f"## 👤 用户（{when}）", "", str(rec.get("text", "")), ""]
        elif k == "assistant":
            out += [f"## 🤖 助手（{when}）", "", str(rec.get("text", "")), ""]
        elif k == "tool":
            name = str(rec.get("name", ""))
            if _is_search_tool(name):
                out += [f"## 🔍 搜索：{name}（{when}）", ""]
            else:
                out += [f"## 🔧 工具：{name}（{when}）", ""]
            arg = _one_line(_arg_display(rec.get("arg", "")))
            if arg:
                label = "关键词" if _is_search_tool(name) else "参数"
                out += [f"**{label}**：{_md_code(arg)}", ""]
            if rec.get("err"):
                out += ["**❌ 错误**", ""]
            out_len = rec.get("out_len")
            if isinstance(out_len, (int, float)):
                out += [f"（输出 {out_len} 字符）", ""]
        elif k == "task":
            out += [f"## 🧵 后台任务（{when}）", "", str(rec.get("text", "")), ""]
        elif k == "todo":
            out += [f"## ✅ Todo（{when}）", "", str(rec.get("text", "")), ""]
        elif k == "compaction":
            out += [f"## 🗜️ 压缩摘要（{when}）", "", str(rec.get("text", "")), ""]
    return out


def records_to_markdown(records, header=None) -> str:
    """cleaned.jsonl 记录列表 → 完整 Markdown 报告。

    header：可选的 [(标签, 值), ...] 列表（学号/姓名/交卷时间等，由调用方
    从 db 组装）；记录统计与时间范围由本函数自行计算附加。
    """
    counts = {}
    times = []
    for rec in records:
        counts[rec["k"]] = counts.get(rec["k"], 0) + 1
        if isinstance(rec.get("time"), (int, float)):
            times.append(rec["time"])

    md = ["# 会话清洗报告", ""]
    for label, value in header or []:
        md.append(f"- {label}：{value}")
    md.append("- 轮次统计：" + " · ".join(f"{k} {c}" for k, c in counts.items()))
    if times:
        md.append(f"- 时间范围：{_ms_to_iso(min(times))} ~ "
                  f"{_ms_to_iso(max(times))}（UTC）")
    md.append("")

    per_agent = {}
    for rec in records:
        per_agent.setdefault(str(rec.get("agent") or "main"), []).append(rec)
    multi = len(per_agent) > 1
    for agent, agent_records in per_agent.items():
        if multi:
            md += [f"## 🧩 代理：{agent}", ""]
        md += _render_turns_md(agent_records)
    return "\n".join(md).rstrip() + "\n"

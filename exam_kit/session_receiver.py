"""考场局域网会话接收服务（单文件、纯标准库，无第三方依赖）。

与 exam_server.py 的交卷分流（config.json 的 upload_mode=lan）配套：
登录/抽题等考试流程仍走云端公网入口；交卷时清洗后的轨迹文档
（cleaned.jsonl / cleaned.md / 附件 zip）上传到本服务，不占公网流量；
随后 exam_server 再向云端发送轻量的「已交卷」标记（不带文件）。
评审/面试时用浏览器直接打开本服务首页（http://<本机IP>:<port>/）查看与下载。

启动：python session_receiver.py [--host 0.0.0.0] [--port 8897] [--root DIR] [--token T]
默认存储：脚本同目录 received_sessions/<届次ID>/<会话ID>/
仅限可信考场内网使用（明文 HTTP）；--token 可给上传接口加一道共享口令。
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8897
DEFAULT_ROOT = BASE_DIR / "received_sessions"
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILENAME_RE = re.compile(r"^[\w\-. ]{1,120}$")

FILE_KINDS = {
    "cleaned_jsonl": "cleaned.jsonl",
    "cleaned_md": "cleaned.md",
    "attachment": "attachment.zip",
}
# 逐题提交的槽位件：cleaned_jsonl_q{N} / cleaned_md_q{N} → cleaned_q{N}.jsonl/.md
KIND_RE = re.compile(r"^(cleaned_jsonl|cleaned_md|attachment)(?:_q(\d+))?$")


def kind_to_filename(kind: str) -> str | None:
    """接口 kind → 落盘文件名；不认识的 kind 返回 None。attachment 不带槽位后缀。"""
    m = KIND_RE.fullmatch(kind)
    if not m:
        return None
    base, q = m.group(1), m.group(2)
    if base == "attachment":
        return "attachment.zip" if q is None else None
    stem = FILE_KINDS[base]
    if q is None:
        return stem
    name, dot, ext = stem.partition(".")
    return f"{name}_q{q}{dot}{ext}"
CONTENT_TYPES = {
    "cleaned.jsonl": "text/plain; charset=utf-8",
    "cleaned.md": "text/markdown; charset=utf-8",
    "attachment.zip": "application/zip",
}
TRACK_LABELS = {
    "track1": "轨迹一（题库自选）",
    "track2": "轨迹二（随机抽题）",
    "track3": "轨迹三（自由创作）",
    "selected": "题库自选",
    "project": "自有项目",
}

ROOT = DEFAULT_ROOT
TOKEN = ""
_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _session_dir(cid: str, sid: str) -> Path | None:
    if not KEY_RE.fullmatch(cid) or not KEY_RE.fullmatch(sid):
        return None
    return ROOT / cid / sid


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _save_info(sdir: Path, info: dict) -> None:
    _atomic_write(sdir / "info.json",
                  json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8"))


def _iter_sessions() -> list[dict]:
    items = []
    if not ROOT.is_dir():
        return items
    for cdir in sorted(ROOT.iterdir()):
        if not cdir.is_dir() or not KEY_RE.fullmatch(cdir.name):
            continue
        for sdir in sorted(cdir.iterdir()):
            if not sdir.is_dir() or not KEY_RE.fullmatch(sdir.name):
                continue
            manifest = _load_json(sdir / "manifest.json")
            info = manifest or _load_json(sdir / "info.json") or {}
            files = {k: v for k, v in (info.get("files") or {}).items() if v}
            items.append({
                "cid": cdir.name,
                "sid": sdir.name,
                "complete": manifest is not None,
                "info": info,
                "files": files,
            })
    items.sort(key=lambda x: str(x["info"].get("received_at") or ""), reverse=True)
    return items


def _file_link_label(kind: str) -> str:
    """首页文件链接文字：逐题槽位件带题号（第 N 题轨迹 / 第 N 题清洗报告）。"""
    m = KIND_RE.fullmatch(kind)
    if not m:
        return kind
    base, q = m.group(1), m.group(2)
    prefix = f"第 {int(q) + 1} 题" if q is not None else ""
    if base == "cleaned_jsonl":
        return prefix + "轨迹 jsonl"
    if base == "cleaned_md":
        return prefix + "清洗报告"
    return "附件 zip"


def _sorted_file_kinds(files: dict) -> list[str]:
    def key(k: str):
        m = KIND_RE.fullmatch(k)
        q = int(m.group(2)) if m and m.group(2) is not None else -1
        order = {"cleaned_jsonl": 0, "cleaned_md": 1, "attachment": 2}
        return (q, order.get(m.group(1), 9) if m else 9)
    return sorted(files, key=key)


class Handler(BaseHTTPRequestHandler):
    server_version = "AlethicSessionReceiver/1.0"

    def log_message(self, fmt, *args):
        print(f"[session_receiver] {self.address_string()} {fmt % args}")

    # ---------- 响应助手 ----------

    def _send(self, status: int, body: bytes, content_type: str,
              extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str):
        self._send_json({"detail": message}, status)

    def _read_body(self, cap: int) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > cap:
            return None
        return self.rfile.read(length) if length else b""

    def _check_token(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("X-Receiver-Token", "") == TOKEN

    # ---------- 路由 ----------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._send_json({"ok": True, "service": "session-receiver"})
        if path == "/":
            return self._send_index()
        parts = path.strip("/").split("/")
        # /api/sessions/{cid}/{sid}/files/{kind}
        if (len(parts) == 6 and parts[0] == "api" and parts[1] == "sessions"
                and parts[4] == "files"
                and kind_to_filename(parts[5]) is not None):
            return self._send_file(parts[2], parts[3], parts[5])
        return self._error(404, "接口不存在")

    def do_POST(self):
        if not self._check_token():
            return self._error(401, "接收服务口令不正确")
        parts = self.path.strip("/").split("/")
        if len(parts) < 5 or parts[:2] != ["api", "sessions"]:
            return self._error(404, "接口不存在")
        sdir = _session_dir(parts[2], parts[3])
        if sdir is None:
            return self._error(400, "届次或会话 ID 格式不合法")
        tail = parts[4:]
        if tail == ["begin"]:
            return self._begin(sdir)
        if len(tail) == 2 and tail[0] == "file" \
                and kind_to_filename(tail[1]) is not None:
            return self._receive_file(sdir, tail[1])
        if tail == ["done"]:
            return self._done(sdir)
        return self._error(404, "接口不存在")

    # ---------- 上传接口 ----------

    def _begin(self, sdir: Path):
        body = self._read_body(MAX_JSON_BYTES)
        if body is None:
            return self._error(400, "请求体过大或缺 Content-Length")
        try:
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "begin 请求体须为 JSON 对象")
        info = {
            "student_id": str(payload.get("student_id", "")),
            "session_token": str(payload.get("session_token", "")),
            "track": str(payload.get("track", "")),
            "agent": str(payload.get("agent", "")),
            "repo_url": str(payload.get("repo_url") or ""),
            "attachment_name": str(payload.get("attachment_name") or ""),
            "received_at": now_iso(),
            "files": {},
        }
        with _LOCK:
            sdir.mkdir(parents=True, exist_ok=True)
            # 逐题上传每题都会 begin 一次：保留已收到的文件记录，只刷新元信息
            old = _load_json(sdir / "info.json") or {}
            info["files"] = old.get("files") or {}
            # 重传场景：重新进入接收态
            (sdir / "manifest.json").unlink(missing_ok=True)
            _save_info(sdir, info)
        return self._send_json({"ok": True})

    def _receive_file(self, sdir: Path, kind: str):
        stored_name = kind_to_filename(kind)
        if stored_name is None:
            return self._error(400, "无效文件类型")
        info = _load_json(sdir / "info.json")
        if info is None:
            return self._error(404, "请先调用 begin 注册会话")
        data = self._read_body(MAX_FILE_BYTES)
        if data is None:
            return self._error(413, f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 上限"
                                    "或缺 Content-Length")
        original = self.headers.get("X-Filename", stored_name)
        if not FILENAME_RE.fullmatch(original):
            original = stored_name
        with _LOCK:
            _atomic_write(sdir / stored_name, data)
            info.setdefault("files", {})[kind] = {
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "original_name": original,
            }
            _save_info(sdir, info)
        return self._send_json({"ok": True, "bytes": len(data)})

    def _done(self, sdir: Path):
        body = self._read_body(MAX_JSON_BYTES)
        if body is None:
            return self._error(400, "请求体过大或缺 Content-Length")
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "done 请求体须为 JSON 对象")
        info = _load_json(sdir / "info.json")
        if info is None:
            return self._error(404, "请先调用 begin 注册会话")
        files = info.get("files") or {}
        if not any(k.startswith("cleaned_jsonl") for k in files):
            return self._error(400, "缺少 cleaned_jsonl，不能标记完成")
        if payload.get("repo_url"):
            info["repo_url"] = str(payload["repo_url"])
        info["finalized_at"] = now_iso()
        manifest = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
        with _LOCK:
            _atomic_write(sdir / "manifest.json", manifest)
        return self._send_json({"ok": True})

    # ---------- 评审浏览 ----------

    def _send_file(self, cid: str, sid: str, kind: str):
        sdir = _session_dir(cid, sid)
        stored = kind_to_filename(kind)
        if sdir is None or stored is None:
            return self._error(400, "届次或会话 ID 格式不合法")
        path = sdir / stored
        if not path.is_file():
            return self._error(404, "文件不存在")
        data = path.read_bytes()
        extra = {}
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if "dl=1" in query:
            extra["Content-Disposition"] = f'attachment; filename="{stored}"'
        ctype = CONTENT_TYPES.get(stored) or (
            "text/plain; charset=utf-8" if stored.endswith(".jsonl")
            else "text/markdown; charset=utf-8" if stored.endswith(".md")
            else "application/zip")
        self._send(200, data, ctype, extra)

    def _send_index(self):
        sessions = _iter_sessions()
        rows = []
        for s in sessions:
            info = s["info"]
            files = s["files"]
            links = []
            base = f"/api/sessions/{s['cid']}/{s['sid']}/files"
            for kind in _sorted_file_kinds(files):
                dl = "?dl=1" if kind == "attachment" else ""
                links.append(f'<a href="{base}/{kind}{dl}">'
                             f'{html.escape(_file_link_label(kind))}</a>')
            status = ('<span style="color:#15803d">已收齐</span>' if s["complete"]
                      else '<span style="color:#b45309">接收中</span>')
            repo = (f'<a href="{html.escape(str(info.get("repo_url")))}" target="_blank"'
                    f' rel="noopener">仓库 ↗</a>' if info.get("repo_url") else "—")
            rows.append(f"""<tr>
<td>{html.escape(str(info.get("received_at") or ""))}</td>
<td>{html.escape(s["cid"])}</td>
<td>{html.escape(str(info.get("student_id") or ""))}</td>
<td>{html.escape(TRACK_LABELS.get(str(info.get("track", "")), str(info.get("track") or "")))}</td>
<td title="{html.escape(s["sid"])}">{html.escape(s["sid"][:8])}…</td>
<td>{status}</td>
<td>{repo}</td>
<td>{(" · ".join(links)) or "—"}</td>
</tr>""")
        page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>机试会话接收 · Alethic Insight</title>
<style>
body{{font-family:system-ui,sans-serif;margin:24px;background:#f6f7f9;color:#1f2937}}
h1{{font-size:20px}} p{{color:#6b7280;font-size:13px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
th,td{{border:1px solid #e5e7eb;padding:6px 10px;text-align:left;vertical-align:top}}
th{{background:#f3f4f6}} a{{color:#2563eb}}
</style></head><body>
<h1>机试会话接收服务</h1>
<p>共 {len(sessions)} 个会话。轨迹 jsonl / 清洗报告可在线查看，附件 zip 直接下载。</p>
<table><tr><th>接收时间</th><th>届次</th><th>学号</th><th>轨迹</th><th>会话</th>
<th>状态</th><th>仓库</th><th>文件</th></tr>
{''.join(rows) or '<tr><td colspan="8">暂无会话</td></tr>'}</table>
</body></html>"""
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")


def main() -> int:
    global ROOT, TOKEN
    parser = argparse.ArgumentParser(description="考场局域网机试会话接收服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--root", default=str(DEFAULT_ROOT),
                        help=f"存储目录（默认 {DEFAULT_ROOT}）")
    parser.add_argument("--token", default="", help="上传接口共享口令（可选）")
    args = parser.parse_args()
    ROOT = Path(args.root).resolve()
    TOKEN = args.token
    ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[session_receiver] 存储目录：{ROOT}")
    print(f"[session_receiver] 监听 http://{args.host}:{args.port}/ "
          f"（考试机 config.json 的 lan_receiver_base 填本机局域网 IP 与该端口）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

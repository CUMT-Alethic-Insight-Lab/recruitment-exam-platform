"""Alethic Insight 招新机试 — 考生本地服务（Python 3.12 纯标准库）。

流程：学号+密码登录（云端账号体系，PBKDF2 客户端散列）→ 按 auth/me 的 exam_mode 分流 →
轨迹选择（standard 支持题库自选 1~3 题 / 随机抽题）→ 题目展示/换题 →
完成交卷（定位并导出作答会话到 pending_upload/ → 本地清洗 → 立即上传云端；
断网时本地留存，重启后自动补传）。
作答工具双支持（M11）：Kimi Code（wire.jsonl 导出 zip）与 opencode（会话 JSON），
由 config.json 的 agent 选择，清洗自动识别格式；opencode 找不到会话时页面提供
手动选择会话文件兜底。
opencode 会话发现三层通道：① opencode.db（v1.18.29 起会话存 SQLite，首选——
只读打开，被 WAL 锁住则拷 db+wal+shm 副本再读）；② opencode export CLI 导出
单文件 JSON；③ 旧版 storage/*.json 目录扫描。取会话内容优先走 opencode export，
失败再从 db 副本拼装出与导出格式同构的 JSON。
云端 API 见 server/app/routes/exam.py 与 server/app/routes/auth.py。
"""

import hashlib
import json
import os
import secrets
import socket
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "state.json"
PENDING_DIR = BASE_DIR / "pending_upload"

try:
    import cleaner  # 同目录的清洗模块（并行交付）；缺失时交卷时报可读错误
except ImportError:
    cleaner = None

ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024  # 补充材料 zip 上限 50MB（与云端一致）

DEFAULT_CONFIG = {
    "cloud_base": "https://alethicinsight.com/recruitment",
    "campaign_id": "2026autumn",
    "exam_port": 8899,
    "kimi_home": "",          # 为空则读 KIMI_CODE_HOME 环境变量，再否则 ~/.kimi-code
    "kimi_bin": "kimi",
    "http_timeout": 10,
    "agent": "kimi",          # 作答工具："kimi" | "opencode"（安装时选择）
    "kimi_web_cmd": "kimi web",       # 拉起 kimi Web 界面的指令（研究线可修正）
    # opencode web 默认随机端口，必须显式 --port 固定（8898 避开考试页 8899 与 kimi web 58627）
    "opencode_web_cmd": "opencode web --port 8898",
    "opencode_home": "",      # 为空则 %USERPROFILE%\.local\share\opencode
    "agent_workdir": "",      # 考试工作目录；为空则用机试包目录
    # 交卷上传目标："cloud"=清洗件+附件直传云端；"lan"=传考场局域网接收机
    # （session_receiver.py），云端只收轻量「已交卷」标记，不占公网流量
    "upload_mode": "cloud",
    "lan_receiver_base": "",  # upload_mode=lan 时必填，如 http://192.168.1.10:8897
}

TRACKS = [
    {"key": "track1", "name": "研发型（理论/概念导向）"},
    {"key": "track2", "name": "实操型（落地实践）"},
    {"key": "track3", "name": "自由创作型（自选领域现场创作/已有项目增量）"},
]

EXAM_MODES = {"standard", "own_project", "free_creation"}
OWN_PROJECT_NOTICE = "个人项目轨：请到报名网站账号页提交 GitHub 仓库链接，无需使用本机考试"
FREE_CREATION_DEFAULT_TRACK = "track3"
QUESTION_IDS_MAX = 3

AGENTS = ("kimi", "opencode")
AGENT_LABELS = {"kimi": "Kimi Code", "opencode": "opencode"}

# 领域 key -> 中文名（与 server/app/constants.py 的 DOMAINS 对齐；用于答题文件夹命名）
# 机试领域为 7 个（aix 已裁撤、ai_safety 为面试素材不进机试，均不在此列）
DOMAIN_LABELS = {
    "llm_arch": "语言/LLM（偏架构）",
    "cv_arch": "CV（偏架构）",
    "vlm_align": "VLM/MLLM（偏对齐）",
    "rl": "RL（大模型与传统决策）",
    "agent": "Agent 编排与框架",
    "memory": "记忆系统",
    "infra": "AI Infra",
}


def _sanitize_dir(name: str) -> str:
    """把领域名变成可作文件夹名的安全字符串：去非法字符、取括号前主名。"""
    name = (name.split("（")[0] or name).split("(")[0] or name
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "-")
    return name.strip(" .") or "领域"


def human_home_label(workdir: Path) -> str:
    """把考试工作目录换算成人话路径前缀：桌面\\<安装根目录名>\\<工作目录名>。

    只硬编码「桌面」字样，其余从 workdir 实际路径的父目录名/目录名推导
    （安装器布局：桌面\\招新考试系统-删除文件夹即完成卸载\\考试工作目录）。"""
    return f"桌面\\{workdir.parent.name}\\{workdir.name}"


def ensure_domain_workspaces(cfg: dict, domain_keys: list[str]) -> tuple[list[str], list[str]]:
    """按考生问卷所选领域，在考试工作目录下建立领域工作区并返回 (中文名, 文件夹名) 列表。

    每个工作区包含：opencode.json（最小合法配置，已存在不覆盖）、作答指引.txt
    （人话路径 + 绝对路径 + 作答提示）；并尽力向 opencode.db 预注册项目行
    （见 preregister_opencode_workspaces，任何失败只记日志不阻断）。"""
    labels, dirs = [], []
    workdir = agent_workdir(cfg)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return [], []
    for k in domain_keys:
        label = DOMAIN_LABELS.get(k, str(k))
        fname = _sanitize_dir(label)
        d = workdir / fname
        try:
            d.mkdir(parents=True, exist_ok=True)
            _write_workspace_files(cfg, d, fname, label, workdir)
        except OSError:
            continue
        labels.append(label)
        dirs.append(fname)
    if dirs:
        preregister_opencode_workspaces(cfg, workdir, dirs)
    return labels, dirs


def _write_workspace_files(cfg: dict, d: Path, fname: str, label: str,
                           workdir: Path) -> None:
    """写 opencode.json（已存在不覆盖）与 作答指引.txt（新文件夹首次生成）。"""
    conf = d / "opencode.json"
    if not conf.exists():
        conf.write_text('{"$schema": "https://opencode.ai/config.json"}\n',
                        encoding="utf-8")
    guide = d / "作答指引.txt"
    if guide.exists():
        return
    agent_disp = AGENT_LABELS.get(str(cfg.get("agent", "kimi")),
                                  str(cfg.get("agent", "kimi")))
    guide.write_text(
        "【Alethic Insight 招新机试 · 本领域作答文件夹】\n"
        f"领域：{label}（文件夹名：{fname}）\n\n"
        f"在这里作答（人话路径）：{human_home_label(workdir)}\\{fname}\n"
        f"绝对路径（可直接粘贴给 opencode）：{(workdir / fname).as_posix()}\n\n"
        f"请在此文件夹内用 {agent_disp} 作答，交卷时会自动找到你的会话。\n"
        "作答时请让 AI 工具的工作目录停留在这个文件夹里。\n",
        encoding="utf-8")


def ensure_domain_folders(cfg: dict, domain_keys: list[str]) -> tuple[list[str], list[str]]:
    """旧版兼容入口：只建目录、不写工作区文件、不预注册 opencode。

    新逻辑（工作区文件 + opencode 预注册）见 ensure_domain_workspaces。
    """
    labels, dirs = [], []
    workdir = agent_workdir(cfg)
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        for k in domain_keys:
            label = DOMAIN_LABELS.get(k, str(k))
            fname = _sanitize_dir(label)
            (workdir / fname).mkdir(parents=True, exist_ok=True)
            labels.append(label)
            dirs.append(fname)
    except OSError:
        return [], []
    return labels, dirs


# ---------- 配置与本地状态 ----------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            print(f"[WARN] config.json 读取失败，使用默认值: {exc}")
    cfg["cloud_base"] = cfg["cloud_base"].rstrip("/")
    if cfg.get("agent") not in AGENTS:
        cfg["agent"] = "kimi"
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"stage": ""}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def iso_to_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------- 云端 API ----------

PBKDF2_ITERATIONS = 100_000
PBKDF2_DKLEN = 32


def compute_client_hash(password: str, salt: str) -> str:
    """与浏览器 WebCrypto 端互通的 PBKDF2 客户端散列（契约见 server/app/auth.py）。"""
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt),
        PBKDF2_ITERATIONS, PBKDF2_DKLEN).hex()


class CloudError(Exception):
    def __init__(self, message: str, status: int = 0, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class SessionNotFound(Exception):
    """交卷时没找到考生的作答会话（message 为考生可读的白话文案）。"""


def _friendly_upload_error(exc: CloudError) -> str:
    """把上传失败翻译成考生能懂的白话（页面不出现术语/堆栈）。"""
    if exc.status in (0, None) or "无法连接" in str(exc):
        return ("网络不通，没能上传到云端。你的作答已安全保存在本机的"
                " pending_upload 文件夹里——恢复网络后点「重试上传」即可；"
                "保持本页开着也会自动补传。")
    return ("云端暂时没能接收你的交卷。你的作答已安全保存在本机的"
            " pending_upload 文件夹里，请稍后点「重试上传」；"
            "多次失败请联系组织者。")


# ---------- HTTP 传输层 ----------
# Cloudflare 按 TLS 指纹拦 python-urllib（实测握手即断：SSL: UNEXPECTED_EOF_WHILE_READING），
# 但透过 curl.exe（Windows 自带，Schannel 栈）正常。因此云端请求默认走 curl，urllib 兜底，
# 可用环境变量 ALE_THIC_HTTP_TRANSPORT=urllib|curl 强制指定（测试用）。

def _curl_exe() -> str | None:
    cands = []
    cands.append(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "curl.exe"))
    which = shutil.which("curl")
    if which:
        cands.append(which)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _http_transport() -> str:
    force = os.environ.get("ALE_THIC_HTTP_TRANSPORT", "").strip().lower()
    if force in ("curl", "urllib"):
        return force
    return "curl" if _curl_exe() else "urllib"


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _curl_exec(argv: list[str], input_data: bytes | None = None,
               timeout: float = 60) -> tuple[int, bytes]:
    """执行 curl，返回 (HTTP 状态码, 响应体)。网络失败抛 CloudError。"""
    import tempfile
    tmp = tempfile.mktemp(prefix="ale_thic_http_")
    try:
        proc = subprocess.run(
            [argv[0], "-sS", "--max-time", str(int(timeout)),
             "-o", tmp, "-w", "%{http_code}"] + argv[1:],
            input=input_data, capture_output=True, timeout=int(timeout) + 5,
            creationflags=CREATE_NO_WINDOW)
        code = int(proc.stdout.decode().strip() or 0) if proc.stdout.strip() else 0
        body = b""
        if os.path.isfile(tmp):
            body = open(tmp, "rb").read()
        if proc.returncode != 0 or code == 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()
            raise CloudError(f"无法连接云端（{argv[-1]}）：{err or 'curl 执行失败'}")
        return code, body
    except subprocess.TimeoutExpired:
        raise CloudError(f"无法连接云端（{argv[-1]}）：连接超时") from None
    except OSError as exc:
        raise CloudError(f"无法连接云端（{argv[-1]}）：{exc}") from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse_cloud_error(status: int, raw: bytes) -> CloudError:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        payload = None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = detail.get("message", str(detail))
    elif detail:
        message = str(detail)
    else:
        message = f"云端返回错误（HTTP {status}）"
    return CloudError(message, status=status, detail=detail)


def cloud_request(cfg: dict, method: str, path: str, body: dict | None = None,
                  session_token: str | None = None,
                  bearer: str | None = None):
    url = f"{cfg['cloud_base']}{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    if _http_transport() == "curl":
        argv = [ _curl_exe() ]
        if session_token:
            argv += ["-H", f"X-Session-Token: {session_token}"]
        if bearer:
            argv += ["-H", f"Authorization: Bearer {bearer}"]
        argv += ["-H", "Accept: application/json"]
        if data is not None:
            argv += ["-H", "Content-Type: application/json", "-X", method,
                     "--data-binary", "@-", url]
        else:
            argv += ["-X", method, url]
        code, raw = _curl_exec(argv, input_data=data, timeout=cfg["http_timeout"])
        if code >= 200 and code < 300:
            return code, json.loads(raw.decode("utf-8")) if raw else (code, {})
        raise _parse_cloud_error(code, raw)
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if session_token:
        headers["X-Session-Token"] = session_token
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=cfg["http_timeout"]) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        exc.close()
        raise _parse_cloud_error(exc.code, raw.encode("utf-8")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"无法连接云端（{cfg['cloud_base']}）：{exc}")


# ---------- multipart 上传 ----------

def load_cleaner():
    """取清洗模块：优先 sys.modules（便于单测注入假 cleaner），否则常规 import。"""
    mod = sys.modules.get("cleaner")
    if mod is not None and (hasattr(mod, "clean_session")
                            or hasattr(mod, "clean_export")):
        return mod
    if cleaner is not None and (hasattr(cleaner, "clean_session")
                                or hasattr(cleaner, "clean_export")):
        return cleaner
    return None


def build_multipart(fields: dict, files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    """手卷 multipart/form-data。files 为 (字段名, 文件路径)。
    返回 (请求体字节, Content-Type 头)。"""
    boundary = f"----AlethicExam{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8"))
    for name, path in files:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8"))
        parts.append(path.read_bytes())
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def upload_session(cfg: dict, meta: dict) -> None:
    """上传一个清洗后的交卷。upload_mode=lan 时分流到局域网接收机（文件不走公网，
    云端只收轻量标记）；默认直传云端。成功（含 409 already_uploaded 幂等）直接返回；
    401/4xx/5xx/网络错误抛 CloudError。"""
    if cfg.get("upload_mode") == "lan":
        return upload_session_lan(cfg, meta)
    cleaned = PENDING_DIR / meta["cleaned_jsonl"]
    if not cleaned.exists():
        raise CloudError(f"清洗文件缺失：{meta['cleaned_jsonl']}")
    fields = {"session_id": str(meta["session_id"])}
    repo_url = str(meta.get("repo_url") or "").strip()
    if repo_url:
        fields["repo_url"] = repo_url
    files = [("cleaned", cleaned)]
    if meta.get("attachment_name"):
        att = PENDING_DIR / f"attachment_{meta['student_id']}.zip"
        if att.exists():
            files.append(("attachment", att))
    body, content_type = build_multipart(fields, files)
    url = f"{cfg['cloud_base']}/api/campaigns/{cfg['campaign_id']}/sessions"
    if _http_transport() == "curl":
        argv = [_curl_exe(), "-H", f"X-Session-Token: {meta['session_token']}",
                "-H", "Accept: application/json"]
        for name, value in fields.items():
            argv += ["-F", f"{name}={value}"]
        for name, path in files:
            argv += ["-F", f"{name}=@{path};type=application/octet-stream"]
        argv.append(url)
        code, raw = _curl_exec(argv, timeout=cfg["http_timeout"])
        if 200 <= code < 300:
            return
        err = _parse_cloud_error(code, raw)
        if code == 409 and isinstance(err.detail, dict) \
                and err.detail.get("code") == "already_uploaded":
            return  # 幂等：云端已有该会话，视为成功
        raise err from None
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": content_type,
        "Accept": "application/json",
        "X-Session-Token": str(meta["session_token"]),
    })
    try:
        with urllib.request.urlopen(req, timeout=cfg["http_timeout"]) as resp:
            resp.read()
            return
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        exc.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if exc.status == 409 and isinstance(detail, dict) \
                and detail.get("code") == "already_uploaded":
            return  # 幂等：云端已有该会话，视为成功
        if isinstance(detail, dict):
            message = detail.get("message", str(detail))
        elif detail:
            message = str(detail)
        else:
            message = f"云端返回错误（HTTP {exc.code}）"
        raise CloudError(message, status=exc.code, detail=detail) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"无法连接云端（{cfg['cloud_base']}）：{exc}") from None


# ---------- 局域网接收机分流（upload_mode=lan） ----------

def lan_http(base: str, method: str, path: str, data: bytes | None = None,
             headers: dict | None = None, timeout: float = 60) -> dict:
    """请求考场局域网接收机（session_receiver.py）。成功返回 JSON；
    失败抛 CloudError（连接类错误文案含「无法连接」，供 _friendly_upload_error 识别）。"""
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        exc.close()
        try:
            detail = json.loads(raw).get("detail")
        except ValueError:
            detail = None
        raise CloudError(str(detail) if detail else f"接收机返回错误（HTTP {exc.code}）",
                         status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CloudError(f"无法连接会话接收服务（{base}）：{exc}") from None


def upload_session_lan(cfg: dict, meta: dict) -> None:
    """交卷分流：文件传局域网接收机，云端只收轻量「已交卷」标记（不带文件）。
    顺序：先收齐文件再标云端——中途失败留在 pending_upload 补传，重传幂等
    （接收机覆盖写，云端标记 409 already_uploaded 视为成功）。"""
    base = str(cfg.get("lan_receiver_base") or "").strip()
    if not base:
        raise CloudError("config.json 的 upload_mode=lan 但未配置 lan_receiver_base，"
                         "请联系组织者检查考试机配置")
    cid = str(meta.get("campaign_id") or cfg["campaign_id"])
    sid = str(meta["session_id"])
    api = f"/api/sessions/{cid}/{sid}"
    repo_url = str(meta.get("repo_url") or "").strip()
    timeout = max(float(cfg.get("http_timeout") or 10), 30)
    lan_http(base, "POST", f"{api}/begin",
             data=json.dumps({
                 "student_id": meta.get("student_id", ""),
                 "session_token": str(meta.get("session_token", "")),
                 "track": meta.get("track", ""),
                 "agent": meta.get("agent", ""),
                 "repo_url": repo_url,
                 "attachment_name": meta.get("attachment_name", ""),
             }).encode("utf-8"),
             headers={"Content-Type": "application/json"}, timeout=timeout)
    uploads: list[tuple[str, Path]] = [
        ("cleaned_jsonl", PENDING_DIR / meta["cleaned_jsonl"]),
        ("cleaned_md", PENDING_DIR / meta["cleaned_md"]),
    ]
    if meta.get("attachment_name"):
        uploads.append(("attachment",
                        PENDING_DIR / f"attachment_{meta['student_id']}.zip"))
    for kind, path in uploads:
        if not path.exists():
            if kind == "cleaned_jsonl":
                raise CloudError(f"清洗文件缺失：{path.name}")
            continue  # md / 附件缺失不算错（与云端上传口径一致）
        lan_http(base, "POST", f"{api}/file/{kind}", data=path.read_bytes(),
                 headers={"X-Filename": path.name}, timeout=300)
    lan_http(base, "POST", f"{api}/done",
             data=json.dumps({"repo_url": repo_url}).encode("utf-8"),
             headers={"Content-Type": "application/json"}, timeout=timeout)
    # 云端轻量标记：文件不公网传输，只让后台知道该会话已交卷（状态/评分流程用）
    try:
        cloud_request(cfg, "POST", f"/api/campaigns/{cid}/sessions/{sid}/lan-uploaded",
                      body={"repo_url": repo_url} if repo_url else {},
                      session_token=str(meta["session_token"]))
    except CloudError as exc:
        if exc.status == 409 and isinstance(exc.detail, dict) \
                and exc.detail.get("code") == "already_uploaded":
            return  # 幂等：云端已标记过
        raise


def meta_path_for(base: str) -> Path:
    return PENDING_DIR / f"{base}.meta.json"


def _rel_to_pending(path: Path) -> str:
    """pending_upload 内文件的相对路径（posix 风格，存入 meta）。"""
    return Path(os.path.relpath(path, PENDING_DIR)).as_posix()


def save_meta(base: str, meta: dict) -> None:
    meta_path_for(base).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def load_meta(base: str) -> dict | None:
    path = meta_path_for(base)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def retry_pending_uploads(cfg: dict | None = None) -> None:
    """扫描 pending_upload 里 uploaded=false 的项逐项补传（每项间隔 1s，静默失败）。"""
    cfg = cfg or load_config()
    for meta_path in sorted(PENDING_DIR.glob("**/*.meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if meta.get("uploaded"):
            continue
        base = meta_path.name[:-len(".meta.json")]
        try:
            upload_session(cfg, meta)
        except CloudError as exc:
            print(f"[exam_server] 自动补传失败 {base}: {exc}")
        else:
            meta["uploaded"] = True
            try:
                save_meta(base, meta)
            except OSError:
                pass
            print(f"[exam_server] 自动补传成功 {base}")
            state = load_state()
            if state.get("pending_base") == base and not state.get("uploaded"):
                state.update({"stage": "uploaded", "uploaded": True})
                state.pop("upload_pending", None)
                state.pop("upload_error", None)
                save_state(state)
        time.sleep(1)


def start_upload_worker() -> None:
    threading.Thread(target=retry_pending_uploads, name="upload-retry", daemon=True).start()


# ---------- Kimi Code 会话定位与导出 ----------

def kimi_home(cfg: dict) -> Path:
    if cfg.get("kimi_home"):
        return Path(cfg["kimi_home"])
    env = os.environ.get("KIMI_CODE_HOME")
    if env:
        return Path(env)
    return Path.home() / ".kimi-code"


def iter_kimi_sessions(home: Path) -> list[dict]:
    """读 session_index.jsonl，逐条补 state.json 的 createdAt。跳过损坏条目。"""
    index = home / "session_index.jsonl"
    sessions = []
    if not index.exists():
        return sessions
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        sdir = Path(entry.get("sessionDir", ""))
        created_at = 0
        state_file = sdir / "state.json"
        if state_file.exists():
            try:
                st = json.loads(state_file.read_text(encoding="utf-8", errors="replace"))
                created_at = int(st.get("createdAt") or 0)
            except (ValueError, OSError):
                created_at = 0
        sessions.append({
            "sessionId": entry.get("sessionId", ""),
            "sessionDir": str(sdir),
            "workDir": entry.get("workDir", ""),
            "createdAt": created_at,
        })
    return sessions


def pick_candidates(sessions: list[dict], since_ms: int) -> list[dict]:
    """开考时间之后创建的会话，新的在前。"""
    cands = [s for s in sessions if s["sessionId"] and s["createdAt"] >= since_ms]
    cands.sort(key=lambda s: s["createdAt"], reverse=True)
    return cands


def export_via_cli(cfg: dict, session_id: str, out_path: Path) -> bool:
    """kimi export <id>（显式 id 非交互）。kimi 不可用或失败返回 False。"""
    kimi_bin = shutil.which(cfg["kimi_bin"]) or (cfg["kimi_bin"] if Path(cfg["kimi_bin"]).exists() else None)
    if not kimi_bin:
        return False
    try:
        result = subprocess.run(
            [kimi_bin, "export", session_id, "-o", str(out_path),
             "--no-include-global-log"],
            capture_output=True, timeout=120,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and out_path.exists()


def export_via_zip(session_dir: Path, out_path: Path) -> None:
    """回退：直接打包会话目录（state.json + agents/**/wire.jsonl + plans 等）。"""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(session_dir.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(session_dir).as_posix())


def export_session(cfg: dict, session: dict, out_path: Path) -> str:
    """返回实际使用的导出方式。"""
    if export_via_cli(cfg, session["sessionId"], out_path):
        return "kimi export"
    export_via_zip(Path(session["sessionDir"]), out_path)
    return "zip 回退"


# ---------- opencode 会话定位 ----------

def opencode_home(cfg: dict) -> Path:
    if cfg.get("opencode_home"):
        return Path(cfg["opencode_home"])
    return Path.home() / ".local" / "share" / "opencode"


def agent_workdir(cfg: dict) -> Path:
    """考试工作目录（考生应在其中使用 AI 工具作答）。"""
    if cfg.get("agent_workdir"):
        return Path(cfg["agent_workdir"])
    return BASE_DIR


def apply_url(cfg: dict) -> str:
    """云端临时报名问卷页（考生报名入口）。"""
    return f"{cfg['cloud_base']}/apply/{cfg['campaign_id']}"


def agent_web_cmd(cfg: dict) -> str:
    """按 config 的 agent 取 Web 启动指令（指令本身可在 config 里改）。"""
    key = f"{cfg['agent']}_web_cmd"
    default = {"kimi": "kimi web", "opencode": "opencode web --port 8898"}
    return str(cfg.get(key) or default[cfg["agent"]])


def agent_web_port(cfg: dict) -> int | None:
    """从 Web 启动指令解析固定端口（--port N）；kimi 无 --port 回退默认 58627。"""
    cmd = agent_web_cmd(cfg)
    idx = cmd.find("--port")
    if idx != -1:
        rest = cmd[idx:].split()
        if len(rest) >= 2 and rest[1].isdigit():
            return int(rest[1])
    if cfg.get("agent") == "kimi":
        return 58627   # kimi web 默认端口
    return None


def agent_web_url(cfg: dict) -> str | None:
    """估算 AI 作答工具的 Web 地址（用于直接拉起浏览器打开）；未知返回 None。
    系统无默认应用时 agent 自己打不开浏览器，由考试系统用同样直接拉起逻辑补开。"""
    port = agent_web_port(cfg)
    return "http://127.0.0.1:%d" % port if port else None


def _port_in_use(port: int) -> bool:
    """探测本地端口是否已被占用（TCP connect_ex，1 秒超时）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _opencode_under(path_str: str, workdir: Path) -> bool:
    try:
        Path(path_str).resolve().relative_to(workdir.resolve())
        return True
    except (ValueError, OSError):
        return False


def _scan_opencode_json_files(home: Path, limit: int = 5000):
    """遍历 opencode 数据目录下的 json 文件（跳过超大文件），逐个解析。"""
    count = 0
    for f in home.rglob("*.json"):
        count += 1
        if count > limit:
            break
        try:
            if f.stat().st_size > 64 * 1024 * 1024:
                continue
            yield f, json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue


def _is_opencode_message(obj) -> bool:
    return (isinstance(obj, dict)
            and str(obj.get("id", "")).startswith("msg_")
            and "sessionID" in obj)


def _is_opencode_part(obj) -> bool:
    return (isinstance(obj, dict)
            and str(obj.get("id", "")).startswith("prt_")
            and "messageID" in obj)


def _is_opencode_session_info(obj) -> bool:
    return (isinstance(obj, dict)
            and str(obj.get("id", "")).startswith("ses_")
            and isinstance(obj.get("time"), dict))


def _opencode_doc_from_parts(session_info: dict, messages: list[dict],
                             parts_by_mid: dict) -> dict:
    """把分立的存储文件拼成与导出格式一致的 {info, messages} 结构。"""
    out_msgs = []
    for msg in sorted(messages,
                      key=lambda m: (m.get("time") or {}).get("created") or 0):
        mid = msg.get("id", "")
        parts = parts_by_mid.get(mid, [])

        def part_key(p):
            t = p.get("time") if isinstance(p.get("time"), dict) else {}
            start = t.get("start")
            end = t.get("end")
            return (start if isinstance(start, (int, float)) else 0,
                    end if isinstance(end, (int, float)) else 0,
                    str(p.get("id", "")))

        parts.sort(key=part_key)
        out_msgs.append({"info": msg, "parts": parts})
    return {"info": session_info, "messages": out_msgs}


def iter_opencode_sessions(home: Path) -> list[dict]:
    """在 opencode 数据目录定位会话，返回新的在前：

    [{session_id, created, updated, directory, source, doc}]。
    source 为 "export"（单个导出 JSON，即真实样例格式）或 "storage"
    （存储目录里分立的 session/message/part 文件拼装）。
    兼容保守策略：拼不出来的会话跳过，交卷页面另有手动选择文件兜底。
    """
    if not home.exists():
        return []
    exports, infos, msgs, parts = [], [], [], []
    for f, obj in _scan_opencode_json_files(home):
        if cleaner_mod_looks_like_opencode(obj):
            st = (obj.get("info") or {}).get("time") or {}
            exports.append({
                "session_id": str(obj["info"].get("id", "")),
                "created": int(st.get("created") or 0),
                "updated": int(st.get("updated") or st.get("created") or 0),
                "directory": str((obj.get("info") or {}).get("directory", "")),
                "source": str(f),
                "doc": obj,
            })
        elif _is_opencode_session_info(obj):
            infos.append((f, obj))
        elif _is_opencode_message(obj):
            msgs.append(obj)
        elif _is_opencode_part(obj):
            parts.append(obj)

    if infos:
        parts_by_mid = {}
        for p in parts:
            parts_by_mid.setdefault(p.get("messageID"), []).append(p)
        msgs_by_sid = {}
        for m in msgs:
            msgs_by_sid.setdefault(m.get("sessionID"), []).append(m)
        for f, sinfo in infos:
            sid = sinfo.get("id", "")
            mlist = msgs_by_sid.get(sid, [])
            if not mlist:
                continue  # 光有会话壳、没有消息，拼不出可清洗的会话
            st = sinfo.get("time") or {}
            exports.append({
                "session_id": sid,
                "created": int(st.get("created") or 0),
                "updated": int(st.get("updated") or st.get("created") or 0),
                "directory": str(sinfo.get("directory", "")),
                "source": "storage",
                "doc": _opencode_doc_from_parts(sinfo, mlist, parts_by_mid),
            })

    exports.sort(key=lambda s: s["updated"], reverse=True)
    return exports


def cleaner_mod_looks_like_opencode(obj) -> bool:
    """避免硬依赖 cleaner 模块的结构判断（与 cleaner.looks_like_opencode 一致）。"""
    if not isinstance(obj, dict):
        return False
    info = obj.get("info")
    if not isinstance(info, dict) or not str(info.get("id", "")).startswith("ses_"):
        return False
    return isinstance(obj.get("messages"), list)


def pick_opencode_candidates(sessions: list[dict], since_ms: int,
                             workdir: Path | None = None) -> list[dict]:
    """开考时间（按会话 updated）之后更新的会话，新的在前；优先考试工作目录内的。"""
    cands = [s for s in sessions if s["session_id"] and s["updated"] >= since_ms]
    cands.sort(key=lambda s: s["updated"], reverse=True)
    if workdir is not None:
        matched = [s for s in cands
                   if s.get("directory")
                   and _opencode_under(s["directory"], workdir)]
        if matched:
            return matched
    return cands


# ---------- opencode.db（v1.18.29+ 会话 SQLite 存储）----------

# 表结构勘察结论见 exam_kit/docs_opencode_db.md（时间一律 epoch 毫秒 int）。
OPENCODE_DB_NAME = "opencode.db"
OPENCODE_EXPORT_TIMEOUT = 120  # 与 kimi export 一致的 CLI 超时


# ---------- opencode 工作区预注册（尽力而为） ----------

def _table_columns(con: sqlite3.Connection, table: str) -> list[str] | None:
    """从 sqlite_master 解析一张表的列名（版本演进不敏感）；无此表返回 None。"""
    try:
        row = con.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    sql = row[0].replace("`", " ").replace('"', " ").replace("[", " ").replace("]", " ")
    paren = sql.find("(")
    if paren == -1:
        return None
    skip = {"PRIMARY", "CONSTRAINT", "FOREIGN", "UNIQUE", "CHECK", "KEY"}
    cols = []
    for part in sql[paren + 1:].split(","):
        tokens = part.split()
        if not tokens:
            continue
        first = tokens[0]
        if first in skip or not first.replace("_", "").isalnum():
            continue
        cols.append(first)
    return list(dict.fromkeys(cols)) or None


def _opencode_project_id(con: sqlite3.Connection) -> str:
    """按现有行风格生成新的 project.id（实测现库为 40 位 hex；有 proj_ 前缀则沿用）。"""
    try:
        ids = [str(r[0]) for r in con.execute("SELECT id FROM project LIMIT 200")]
    except sqlite3.Error:
        ids = []
    if any(i.startswith("proj_") for i in ids):
        return "proj_" + secrets.token_hex(12)
    return secrets.token_hex(20)


def _insert_or_ignore(con: sqlite3.Connection, table: str, cols: list[str],
                      row: dict, required: set[str]) -> None:
    """INSERT OR IGNORE，只写入表里实际存在的列；必需列缺失则整体放弃。"""
    avail = [c for c in cols if c in row]
    if not required.issubset(avail):
        return
    sql = ("INSERT OR IGNORE INTO %s (%s) VALUES (%s)"
           % (table, ", ".join(avail), ", ".join("?" * len(avail))))
    con.execute(sql, [row.get(c) for c in avail])


def preregister_opencode_workspaces(cfg: dict, workdir: Path, dirs: list[str]) -> None:
    """尽力而为：向 opencode.db 预注册领域工作区（project / project_directory 行），
    让考生第一次打开 opencode Web 就能看到这些工作区。

    目录一律用正斜杠形式（与现有行一致）；已注册过的目录跳过（幂等）。
    db 不存在 / 不可打开 / 表结构不符 / 写库失败 — 一律只记日志跳过，不阻断登录。
    """
    if not dirs:
        return
    home = opencode_home(cfg)
    db = home / OPENCODE_DB_NAME
    if not db.exists():
        return
    # 防御：真实用户库（默认 opencode 数据目录）+ 系统临时目录里的工作区 = 测试污染
    # 向量，跳过不写；显式配置 opencode_home 时视为测试/自定义环境，照常预注册。
    if not cfg.get("opencode_home"):
        try:
            workdir.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
            _log("opencode 工作区预注册跳过（工作目录在临时目录内，疑似非考试环境）")
            return
        except (ValueError, OSError):
            pass
    try:
        con = sqlite3.connect(_sqlite_ro_uri(db), uri=True, timeout=2)
        try:
            proj_cols = _table_columns(con, "project")
            pdir_cols = _table_columns(con, "project_directory")
        finally:
            con.close()
    except sqlite3.Error as exc:
        _log(f"opencode 工作区预注册跳过（opencode.db 不可勘察）: {exc}")
        return
    if (proj_cols is None or "id" not in proj_cols
            or not ({"worktree", "directory"} & set(proj_cols))):
        _log("opencode 工作区预注册跳过（project 表结构不符合预期）")
        return
    now_ms = int(time.time() * 1000)
    try:
        wcon = sqlite3.connect(str(db), timeout=5)
        try:
            main_col = "worktree" if "worktree" in proj_cols else "directory"
            for d in dirs:
                try:
                    _preregister_one_workspace(
                        wcon, proj_cols, pdir_cols, workdir, d, main_col, now_ms)
                except sqlite3.Error as exc:
                    _log(f"opencode 工作区预注册跳过（{d}）: {exc}")
            wcon.commit()
        finally:
            wcon.close()
    except sqlite3.Error as exc:
        _log(f"opencode 工作区预注册跳过（写库失败）: {exc}")


def _preregister_one_workspace(wcon: sqlite3.Connection, proj_cols: list[str],
                               pdir_cols: list[str] | None, workdir: Path,
                               d: str, main_col: str, now_ms: int) -> None:
    fwd = (workdir / d).resolve().as_posix()
    bwd = str(workdir / d)
    existing = wcon.execute(
        "SELECT id FROM project WHERE worktree = ? OR worktree = ? LIMIT 1",
        (fwd, bwd)).fetchone()
    if existing is not None:
        return  # 已注册过，幂等跳过
    pid = _opencode_project_id(wcon)
    _insert_or_ignore(wcon, "project", proj_cols, {
        "id": pid,
        "worktree": fwd,
        "directory": fwd,
        "name": d,
        "vcs": None,
        "time_created": now_ms,
        "time_updated": now_ms,
        "time_initialized": now_ms,
        "sandboxes": "[]",
    }, required={"id", main_col})
    if pdir_cols and {"project_id", "directory"} <= set(pdir_cols):
        _insert_or_ignore(wcon, "project_directory", pdir_cols, {
            "project_id": pid,
            "directory": fwd,
            "time_created": now_ms,
        }, required={"project_id", "directory"})


def _sqlite_ro_uri(db: Path) -> str:
    return "file:" + urllib.parse.quote(str(db).replace("\\", "/")) + "?mode=ro"


def _copy_opencode_db_files(home: Path) -> tuple[Path, Path] | None:
    """拷贝 db + wal + shm 到临时目录（opencode 可能正开着，直接只读打开会被
    WAL 锁住）。返回 (副本 db 路径, 临时目录)；源 db 不存在返回 None。"""
    src = home / OPENCODE_DB_NAME
    if not src.exists():
        return None
    tmpdir = Path(tempfile.mkdtemp(prefix="opencode_db_"))
    for suffix in ("", "-wal", "-shm"):
        p = home / (OPENCODE_DB_NAME + suffix)
        if p.exists():
            shutil.copyfile(p, tmpdir / (OPENCODE_DB_NAME + suffix))
    return tmpdir / OPENCODE_DB_NAME, tmpdir


def _probe(con) -> bool:
    try:
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        return True
    except sqlite3.Error:
        return False


def open_opencode_db(home: Path):
    """只读打开 opencode.db。优先 file:...?mode=ro；失败（WAL 锁/占用）则拷
    db+wal+shm 副本再读副本。返回 (连接, 临时目录或 None)；打不开返回 (None, None)。
    调用方负责 close 连接并清理临时目录（如非 None）。"""
    db = home / OPENCODE_DB_NAME
    if not db.exists():
        return None, None
    try:
        con = sqlite3.connect(_sqlite_ro_uri(db), uri=True, timeout=2)
        if _probe(con):
            return con, None
        con.close()
    except sqlite3.Error:
        pass
    copied = _copy_opencode_db_files(home)
    if copied is None:
        return None, None
    db_copy, tmpdir = copied
    try:
        con = sqlite3.connect(_sqlite_ro_uri(db_copy), uri=True, timeout=2)
        if _probe(con):
            return con, tmpdir
        con.close()
    except sqlite3.Error:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)
    return None, None


def opencode_db_sessions(home: Path) -> list[dict]:
    """列出 opencode.db 里有消息的会话（新的在前）。

    返回 [{session_id, created, updated, directory, source: "db", doc: None}]；
    db 不存在/打不开/无会话表都返回空列表（上层降级到 JSON 目录扫描）。
    """
    con, tmpdir = open_opencode_db(home)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT s.id, s.directory, s.time_created, s.time_updated"
            " FROM session s"
            " WHERE EXISTS (SELECT 1 FROM message m WHERE m.session_id = s.id)"
            " ORDER BY s.time_updated DESC").fetchall()
    except sqlite3.Error as exc:
        print(f"[exam_server] 读取 opencode.db 会话列表失败: {exc}")
        rows = []
    finally:
        con.close()
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return [{
        "session_id": str(r[0]),
        "created": int(r[2] or 0),
        "updated": int(r[3] or 0),
        "directory": str(r[1] or ""),
        "source": "db",
        "doc": None,  # 内容按需取（export 通道 / db 副本拼装），列表阶段不读消息
    } for r in rows]


def opencode_db_session_doc(home: Path, session_id: str) -> dict | None:
    """从 opencode.db（必要时读临时副本）拼装一个会话，结构与 opencode export
    的单文件 JSON 同构：{info, messages:[{info, parts}]}。拼不出来返回 None。
    """
    con, tmpdir = open_opencode_db(home)
    if con is None:
        return None
    try:
        con.row_factory = sqlite3.Row
        srow = con.execute(
            "SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        if srow is None:
            return None

        def col(name, default=None):
            # 列随 opencode 版本演进（老库没有 cost/tokens 等），缺列用默认值
            return srow[name] if name in srow.keys() else default

        info = {
            "id": srow["id"], "projectID": col("project_id"),
            "directory": col("directory"), "slug": col("slug"),
            "title": col("title"), "version": col("version"),
            "time": {"created": srow["time_created"],
                     "updated": srow["time_updated"]},
            "cost": col("cost"),
            "tokens": {"input": col("tokens_input"),
                       "output": col("tokens_output"),
                       "reasoning": col("tokens_reasoning"),
                       "cache": {"read": col("tokens_cache_read"),
                                 "write": col("tokens_cache_write")}},
        }
        doc = {"info": info, "messages": []}
        parts_by_mid: dict[str, list[dict]] = {}
        for pid, mid, sid, pdata, ptime in con.execute(
                "SELECT p.id, p.message_id, p.session_id, p.data, p.time_created"
                " FROM part p JOIN message m ON m.id = p.message_id"
                " WHERE m.session_id = ? ORDER BY p.time_created, p.id",
                (session_id,)):
            part = json.loads(pdata)
            part.setdefault("id", pid)
            part.setdefault("messageID", mid)
            part.setdefault("sessionID", sid)
            parts_by_mid.setdefault(mid, []).append(part)
        for mid, mdata, msid, mtime in con.execute(
                "SELECT id, data, session_id, time_created FROM message"
                " WHERE session_id = ? ORDER BY time_created, id",
                (session_id,)):
            minfo = json.loads(mdata)
            minfo.setdefault("id", mid)
            minfo.setdefault("sessionID", msid)
            t = minfo.get("time")
            if not (isinstance(t, dict) and isinstance(t.get("created"), (int, float))):
                minfo["time"] = {"created": mtime}
            doc["messages"].append(
                {"info": minfo, "parts": parts_by_mid.get(mid, [])})
        return doc
    except (sqlite3.Error, ValueError) as exc:
        print(f"[exam_server] 从 opencode.db 拼装会话 {session_id} 失败: {exc}")
        return None
    finally:
        con.close()
        if tmpdir is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)


def find_opencode_bin() -> str | None:
    """定位 opencode 可执行文件：先 PATH，再安装器默认目录 AlethicTools\\opencode。"""
    exe = shutil.which("opencode")
    if exe:
        return exe
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        cand = Path(local) / "Programs" / "AlethicTools" / "opencode" / "opencode.exe"
        if cand.exists():
            return str(cand)
    return None


def opencode_export_session(cfg: dict, session_id: str, out_path: Path) -> bool:
    """通道①：`opencode export <sessionID>` 导出单文件 JSON 到 out_path。

    注意：export 子命令把 JSON 打到 stdout（实测 1.18.18 起 help 里没有 --output
    参数），所以直接把 stdout 重定向到文件，不依赖 shell。opencode.exe 所在目录
    临时附加到 PATH（安装器装在 AlethicTools\\opencode，可能不在当前 PATH）。
    失败（无命令/超时/产物不是该会话的导出 JSON）返回 False。
    """
    exe = find_opencode_bin()
    if not exe:
        print("[exam_server] opencode export 通道跳过：没有找到 opencode 命令")
        return False
    env = dict(os.environ)
    env["PATH"] = str(Path(exe).resolve().parent) + os.pathsep + env.get("PATH", "")
    try:
        with open(out_path, "wb") as fh:
            result = subprocess.run(
                [exe, "export", session_id], stdout=fh,
                stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                timeout=cfg.get("opencode_export_timeout", OPENCODE_EXPORT_TIMEOUT),
                env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"[exam_server] opencode export {session_id} 失败: {exc!r}")
        return False
    if result.returncode != 0 or not out_path.exists():
        print(f"[exam_server] opencode export {session_id} 退出码 {result.returncode}")
        return False
    try:
        doc = json.loads(out_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        print(f"[exam_server] opencode export 产物不是合法 JSON: {exc}")
        return False
    if not cleaner_mod_looks_like_opencode(doc) \
            or str((doc.get("info") or {}).get("id", "")) != session_id:
        print(f"[exam_server] opencode export 产物不是会话 {session_id} 的导出")
        return False
    return True


def _opencode_document(cfg: dict, home: Path, session_id: str, out_path: Path):
    """取会话内容双通道：优先 opencode export（产物即 E 线清洗管线吃的格式），
    失败再从 opencode.db 副本拼装同构 JSON。成功返回 (doc, 方式名)，
    失败返回 (None, None)。"""
    if opencode_export_session(cfg, session_id, out_path):
        try:
            doc = json.loads(out_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None, None
        return doc, "opencode export"
    doc = opencode_db_session_doc(home, session_id)
    if doc is not None:
        out_path.write_text(
            json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return doc, "opencode 数据库副本"
    return None, None


# ---------- 页面 ----------

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alethic Insight 招新机试</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: "Microsoft YaHei", sans-serif; margin: 0 auto; padding: 0 16px 40px;
         max-width: 880px; color: #222; overflow-x: hidden; }
  #banner { font-size: 12px; color: #666; background: #f5f5f0; border-bottom: 1px solid #e5e5e0;
            margin: 0 -16px 12px; padding: 6px 16px; line-height: 1.6; }
  h1 { font-size: 24px; text-align: center; } h2 { font-size: 19px; margin-top: 24px; }
  .card { border: 1px solid #ddd; border-radius: 10px; padding: 18px 22px; margin: 14px 0; }
  button { cursor: pointer; border: 0; border-radius: 8px; background: #2456c8; color: #fff; }
  button:disabled { background: #aaa; cursor: not-allowed; }
  button.big { display: block; width: 100%; font-size: 21px; padding: 16px 24px; margin: 10px 0; }
  button.huge { display: block; width: 100%; font-size: 26px; padding: 24px 24px; margin: 12px 0; }
  button.secondary { background: #eee; color: #333; font-size: 15px; padding: 9px 18px; }
  input, select { padding: 11px; font-size: 18px; width: 100%; max-width: 430px; display: block;
                  margin: 6px 0; border: 1px solid #bbb; border-radius: 6px; }
  .err { color: #c0392b; margin: 10px 0; white-space: pre-wrap; font-size: 16px; }
  .ok { color: #1e8449; }
  .pending { color: #b7950b; }
  .track { display: block; width: 100%; margin: 10px 0; font-size: 19px; padding: 15px 20px;
           text-align: left; }
  .qbody { white-space: pre-wrap; color: #222; margin-top: 8px; font-size: 18px; line-height: 1.7; }
  .pool-track-head { font-size: 19px; font-weight: 700; margin: 18px 0 6px; color: #1a1a2e; }
  .pool-domain-head { font-size: 17px; font-weight: 700; color: #1d4ed8; background: #eef2ff;
      border-left: 4px solid #1d4ed8; padding: 8px 12px; margin: 12px 0 4px; border-radius: 6px; }
  .pool-item { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px 14px;
      margin: 8px 0; background: #fff; }
  .pool-row { display: flex; align-items: baseline; gap: 10px; cursor: pointer; }
  .pool-row input[type=checkbox] { width: 20px; height: 20px; flex: none; }
  .qnum { font-weight: 700; color: #1d4ed8; font-size: 18px; white-space: nowrap; flex: none; }
  .qtitle { font-weight: 700; font-size: 17px; color: #111; }
  .qid { margin-left: auto; color: #9ca3af; font-size: 12px; white-space: nowrap; }
  .pool-body summary { cursor: pointer; color: #2563eb; font-size: 14px; margin-top: 6px; }
  .pool-body .qbody { font-size: 16px; color: #444; }
  .pool-badge { font-size: 12px; padding: 1px 9px; border-radius: 10px; color: #fff;
      flex: none; white-space: nowrap; }
  .pool-badge.t1 { background: #2456c8; } .pool-badge.t2 { background: #1e8449; }
  .pool-badge.t3 { background: #b8860b; }
  .sel-tag { color: #1e8449; font-weight: 700; font-size: 13px; }
  .ws-card { border: 2px solid #1e8449; background: #f4faf4; border-radius: 10px;
      padding: 14px 18px; margin: 12px 0; font-size: 16px; }
  .ws-line { margin: 6px 0; font-size: 16px; word-break: break-all; }
  .ws-abs { font-family: Consolas, monospace; color: #2456c8; font-size: 14px; }
  .copy-btn { background: #eee; color: #333; font-size: 13px; padding: 3px 12px;
      border-radius: 6px; margin-left: 8px; }
  .muted { color: #888; font-size: 14px; }
  .hidden { display: none; }
  #finish-result-success { text-align: center; padding: 24px 12px; }
  #finish-result-success .big-ok { font-size: 34px; color: #1e8449; font-weight: bold; margin: 8px 0; }
  .failbox { border: 2px solid #c0392b; border-radius: 10px; padding: 14px 18px; margin: 12px 0; }
  .failbox .msg { color: #c0392b; font-size: 17px; white-space: pre-wrap; line-height: 1.6; }
  .spinner { display: inline-block; width: 26px; height: 26px; border: 4px solid #ccc;
             border-top-color: #2456c8; border-radius: 50%; animation: spin 0.9s linear infinite;
             vertical-align: middle; margin-right: 10px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .step-tag { display: inline-block; background: #2456c8; color: #fff; border-radius: 12px;
              font-size: 13px; padding: 2px 12px; margin-right: 8px; }
</style>
</head>
<body>
<div id="banner">
  声明：本考试系统只上传<b>清洗后的会话记录</b>（已去除思考过程与工具输出正文），不上传你电脑里的其他文件；
  Kimi Code / opencode 为第三方软件，与本社团无关；opencode 的免费模型是 OpenCode Zen
  的限时免费型号，首次使用需联网注册 Zen 账号并用 opencode auth login 登录后方可使用；
  断网时清洗后的记录会自动保存在 pending_upload 文件夹，联网后自动补传。
</div>
<h1>Alethic Insight 社团招新 · 机试</h1>
<div id="err" class="err"></div>
<div id="note" class="pending"></div>

<div id="stage-login" class="card hidden">
  <h2><span class="step-tag">第 1 步</span>学号登录</h2>
  <input id="student-id" placeholder="学号（8 位数字）" maxlength="8" inputmode="numeric">
  <input id="password" type="password" placeholder="密码（初始密码 = 学号）">
  <button class="big" onclick="login()">登 录</button>
  <p class="muted" style="margin-top:10px">还没报名？点这里<a
     href="__APPLY_URL__" target="_blank" rel="noopener">前往临时报名问卷</a>
     （先用学号报名，再回来登录机试）。</p>
</div>

<div id="domain-guide" class="card hidden"></div>

<div id="stage-ownproject" class="card hidden">
  <h2>个人项目轨</h2>
  <div id="ownproject-who" class="muted"></div>
  <p id="ownproject-text" style="font-size:17px"></p>
</div>

<div id="stage-track" class="card hidden">
  <h2><span class="step-tag">第 2 步</span>选择答题轨迹</h2>
  <div id="who" class="muted"></div>
  <div id="pw-note" class="pending hidden">你仍在使用初始密码，建议到招新平台网页端修改密码。</div>
  <div id="mode-note" class="muted"></div>
  <div id="tracks"></div>
  <div id="selfselect-block" class="hidden">
    <p id="pool-note" class="muted">也可以从题库中自己挑 1~3 道题（点上面轨迹按钮则为随机抽题）。</p>
    <button id="pool-open-btn" class="secondary" onclick="loadQuestionPool()">浏览题库自选题</button>
    <div id="question-pool" class="hidden"></div>
  </div>
</div>

<div id="stage-exam" class="hidden">
  <div class="card">
    <h2><span class="step-tag">第 3 步</span>题目</h2>
    <div id="exam-meta" class="muted"></div>
    <div id="workspace-card" class="ws-card hidden"></div>
    <div id="guide" class="card"></div>
    <div id="questions"></div>
    <button id="reroll-btn" class="secondary" onclick="reroll()">换题</button>
    <span id="rerolls-left" class="muted"></span>
    <button id="ack-btn" class="big" onclick="acknowledge()">我已理解，开始在 AI 工具里作答</button>
  </div>
  <div id="after-ack" class="hidden">
    <div class="card">
      <p style="font-size:17px">现在请<b>切到你的 AI 工具窗口</b>（Kimi Code 或 opencode）完成作答。
        答完以后<b>回到本页面</b>，点下面的绿色大按钮交卷。</p>
    </div>
    <div class="card">
      <h2>补充材料（可选，可跳过）</h2>
      <p class="muted" id="repo-hint">可填写 GitHub 仓库链接，或上传一个 zip 附件（≤50MB）；两项都可留空。</p>
      <input id="repo-url" placeholder="GitHub 仓库链接（可选）">
      <div style="margin-top:8px">
        <input type="file" id="attachment-file" accept=".zip" style="max-width:430px;font-size:15px;padding:8px">
        <span id="attachment-info" class="muted"></span>
        <button id="attachment-del" class="secondary hidden" onclick="delAttachment()">删除附件</button>
      </div>
    </div>
    <div class="card">
      <h2><span class="step-tag">第 4 步</span>完成交卷</h2>
      <button id="finish-btn" class="huge" style="background:#1e8449"
              onclick="finish()">清洗并上传我的作答</button>
      <div id="finish-busy" class="pending hidden" style="font-size:18px;text-align:center">
        <span class="spinner"></span>正在清洗并上传，请勿关闭页面…
      </div>
      <div id="finish-result-success" class="hidden">
        <div class="big-ok">✔ 交卷成功！</div>
        <p style="font-size:17px">你的作答记录已上传到云端，考试完成。</p>
        <p class="muted" id="success-detail"></p>
      </div>
      <div id="finish-result-fail" class="failbox hidden">
        <div class="msg" id="fail-msg"></div>
        <button id="retry-btn" class="big" onclick="retryUpload()">重试上传</button>
        <p class="muted" style="margin-top:14px">兜底方案：手动选择会话文件
          （opencode 考生：选择导出的会话 JSON 文件）</p>
        <input type="file" id="manual-session-file" accept=".json"
               style="max-width:430px;font-size:15px;padding:8px">
        <div id="manual-status" class="muted"></div>
      </div>
    </div>
  </div>
</div>

<div id="stage-done" class="card hidden">
  <div id="done-success" class="hidden" style="text-align:center;padding:20px 8px">
    <div class="big-ok" style="font-size:34px;color:#1e8449;font-weight:bold">✔ 交卷成功！</div>
    <p style="font-size:17px">你的作答记录已上传到云端，考试完成。</p>
  </div>
  <div id="done-pending" class="hidden">
    <p style="font-size:18px">作答记录已保存到本机，但还没传到云端。</p>
    <div id="upload-err" class="failbox"><div class="msg" id="done-fail-msg"></div></div>
    <button id="retry-btn-done" class="big" onclick="retryUpload()">重试上传</button>
    <p class="muted">保持本页开着也会自动补传。</p>
  </div>
  <p class="muted" id="done-detail"></p>
  <p class="muted">如需重考请联系组织者作废会话后重新登录。</p>
</div>

<script>
let state = {};
let allTracks = [];
const OWN_PROJECT_NOTICE = "个人项目轨：请到报名网站账号页提交 GitHub 仓库链接，无需使用本机考试";
const POOL_MAX = 3;
const DOMAIN_LABELS = {
  "llm_arch": "语言/LLM（偏架构）",
  "cv_arch": "CV（偏架构）",
  "vlm_align": "VLM/MLLM（偏对齐）",
  "rl": "RL（大模型与传统决策）",
  "agent": "Agent 编排与框架",
  "memory": "记忆系统",
  "infra": "AI Infra"
};

/* __POOL_LOGIC_BEGIN__ */
function poolToggleIds(ids, id, domain, domainsById) {
  if (ids.indexOf(id) !== -1) return ids.filter(x => x !== id);
  if (domain && domainsById) {
    // 同域互斥：取消该领域其它勾选，跨域不受影响
    const sameDomain = ids.filter(x => domainsById[x] === domain);
    if (sameDomain.length) {
      return ids.filter(x => domainsById[x] !== domain).concat([id]);
    }
  }
  if (ids.length >= POOL_MAX) return ids;
  return ids.concat([id]);
}
function poolFilterByDomains(questions, keys) {
  if (!keys || !keys.length) return questions;
  return questions.filter(q => keys.indexOf(q.domain || "") !== -1);
}
function poolSortByTrack(questions) {
  const order = {track1: 0, track2: 1, track3: 2};
  return questions.slice().sort((a, b) => {
    const ka = order[a.track] !== undefined ? order[a.track] : 99;
    const kb = order[b.track] !== undefined ? order[b.track] : 99;
    return ka - kb;
  });
}
function poolValidateStandard(ids) {
  if (!ids.length) return "请先勾选 1~" + POOL_MAX + " 道题";
  return "";
}
function poolValidateTrack3(ids, domainsById) {
  if (!ids.length) return "请至少选择 1 道题";
  if (domainsById) {
    const seen = new Set();
    for (const x of ids) {
      if (seen.has(domainsById[x])) return "同一领域只能选择一道题作答";
      seen.add(domainsById[x]);
    }
  }
  return "";
}
/* __POOL_LOGIC_END__ */

function humanPath(dir) {
  return (state.workdir_human || "") + "\\\\" + dir;
}

function absPath(dir) {
  return state.workdir_abs ? state.workdir_abs + "/" + dir : "";
}

function legacyCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch (e) {}
  ta.remove();
  done();
}

function copyPath(text, btn) {
  const done = () => {
    if (!btn) return;
    const orig = btn.textContent;
    btn.textContent = "已复制";
    setTimeout(() => { btn.textContent = orig; }, 1800);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => legacyCopy(text, done));
  } else {
    legacyCopy(text, done);
  }
}

function showNote(msg) { document.getElementById("note").textContent = msg || ""; }

function renderDomainGuide() {
  const dg = document.getElementById("domain-guide");
  if (!dg) return;
  const labels = state.domains || [];
  if (!labels.length) { dg.classList.add("hidden"); dg.textContent = ""; return; }
  dg.classList.remove("hidden");
  dg.textContent = "";
  const title = document.createElement("b");
  title.textContent = "你的答题位置指引";
  dg.appendChild(title);
  const sub = document.createElement("div");
  sub.className = "muted";
  sub.textContent = "你在问卷里选择的领域，已为你建好对应的工作区文件夹。请在对应文件夹里使用 AI 工具作答：";
  dg.appendChild(sub);
  const ul = document.createElement("ul");
  labels.forEach((label, i) => {
    const dir = (state.domain_dirs && state.domain_dirs[i]) || "";
    const li = document.createElement("li");
    li.style.marginBottom = "10px";
    const b = document.createElement("b");
    b.textContent = label + "（「" + dir + "」文件夹）";
    li.appendChild(b);
    const l1 = document.createElement("div");
    l1.className = "ws-line";
    l1.textContent = "在这里作答：" + humanPath(dir);
    li.appendChild(l1);
    const l2 = document.createElement("div");
    l2.className = "ws-line";
    l2.appendChild(document.createTextNode("绝对路径（可直接粘给 opencode）："));
    const code = document.createElement("span");
    code.className = "ws-abs";
    code.textContent = absPath(dir);
    l2.appendChild(code);
    const cp = document.createElement("button");
    cp.className = "copy-btn";
    cp.textContent = "复制";
    cp.onclick = () => copyPath(absPath(dir), cp);
    l2.appendChild(cp);
    li.appendChild(l2);
    const op = document.createElement("button");
    op.className = "secondary";
    op.textContent = "打开此领域的 " + (state.agent_label || "AI 工具");
    op.onclick = () => openWorkspace(dir);
    li.appendChild(op);
    ul.appendChild(li);
  });
  dg.appendChild(ul);
  const tip = document.createElement("div");
  tip.className = "muted";
  tip.textContent = "提示：作答工具窗口已在「考试工作目录」根目录打开，你可以在里面找到这些文件夹；"
    + "也可以点上面的按钮直接打开对应工作区（不要离开该文件夹作答）。";
  dg.appendChild(tip);
}

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("请求失败 HTTP " + res.status));
  return data;
}

function showErr(msg) {
  document.getElementById("err").textContent = msg || "";
  showNote("");
}

function hide(id) { document.getElementById(id).classList.add("hidden"); }
function unhide(id) { document.getElementById(id).classList.remove("hidden"); }

function render() {
  for (const el of document.querySelectorAll("body > div[id^=stage]")) el.classList.add("hidden");
  hide("finish-busy"); hide("finish-result-success"); hide("finish-result-fail");
  renderDomainGuide();
  const stage = state.stage || "";
  if (stage === "uploaded" || stage === "finished") {
    unhide("stage-done");
    const sid = state.agent_session_id || "";
    document.getElementById("done-detail").textContent =
      sid ? ("作答会话：" + sid) : "";
    if (state.stage === "uploaded" || state.uploaded) {
      unhide("done-success"); hide("done-pending");
    } else {
      hide("done-success"); unhide("done-pending");
      document.getElementById("done-fail-msg").textContent =
        state.upload_error || "暂时没能上传到云端，请点下面的按钮重试。";
    }
  } else if (stage === "enrolled") {
    unhide("stage-exam");
    renderExam();
  } else if (stage === "logged_in") {
    if (state.exam_mode === "own_project") {
      unhide("stage-ownproject");
      document.getElementById("ownproject-who").textContent =
        state.name + "（" + state.student_id + "）";
      document.getElementById("ownproject-text").textContent = OWN_PROJECT_NOTICE;
    } else {
      unhide("stage-track");
      document.getElementById("who").textContent =
        state.name + "（" + state.student_id + "），请选择轨迹：";
      document.getElementById("pw-note").classList.toggle("hidden", !!state.pw_changed);
      renderTracks();
    }
  } else {
    unhide("stage-login");
  }
}

function renderExam() {
  document.getElementById("exam-meta").textContent =
    state.name + "（" + state.student_id + "）· 轨迹：" + (state.track_name || state.track);
  const guide = document.getElementById("guide");
  if (state.track3_guide_text) { guide.textContent = state.track3_guide_text; guide.classList.remove("hidden"); }
  else guide.classList.add("hidden");
  const box = document.getElementById("questions");
  box.innerHTML = "";
  (state.questions || []).forEach((q, i) => {
    const div = document.createElement("div");
    div.innerHTML = "<b style=\\"font-size:19px\\">Q" + (i + 1) + " [" + q.domain + "] " + q.title + "</b>" +
      '<div class="qbody"></div><hr>';
    div.querySelector(".qbody").textContent = q.body;
    box.appendChild(div);
  });
  const selfSel = !!state.self_selected;
  const rerollBtn = document.getElementById("reroll-btn");
  rerollBtn.classList.toggle("hidden", selfSel);
  rerollBtn.disabled = !(state.rerolls_left > 0) || state.track === "track3";
  document.getElementById("rerolls-left").textContent = selfSel
    ? "自选题模式（发题后如需换题请联系组织者）"
    : "剩余换题次数：" + (state.rerolls_left ?? 0);
  const repoInput = document.getElementById("repo-url");
  if (document.activeElement !== repoInput) repoInput.value = state.repo_url || "";
  if (state.exam_mode === "free_creation") {
    document.getElementById("repo-hint").textContent =
      "自由创作轨：请填写你的 GitHub 仓库链接，交卷时会随会话记录一并提交。";
  }
  renderAttachment();
  renderWorkspaceCard();
}

function renderWorkspaceCard() {
  const el = document.getElementById("workspace-card");
  const qs = state.questions || [];
  if (state.track !== "track3" || !qs.length) {
    el.classList.add("hidden");
    return;
  }
  // 每个领域一道题：涉及的领域可能不止一个，逐个列出工作区
  const seen = {};
  const blocks = [];
  qs.forEach(q => {
    const domain = q.domain || "";
    if (seen[domain]) return;
    seen[domain] = true;
    const dir = (state.domain_dirs || [])[(state.domain_keys || []).indexOf(domain)];
    if (dir) blocks.push({domain: domain, dir: dir,
                          label: DOMAIN_LABELS[domain] || domain});
  });
  if (!blocks.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.textContent = "";
  const multi = blocks.length > 1;
  blocks.forEach(b => {
    const head = document.createElement("div");
    head.style.fontSize = "18px";
    head.style.fontWeight = "bold";
    head.textContent = "本题为 " + b.label + " 领域，请在对应工作区作答";
    el.appendChild(head);
    const l1 = document.createElement("div");
    l1.className = "ws-line";
    l1.textContent = "在这里作答：" + humanPath(b.dir);
    el.appendChild(l1);
    const l2 = document.createElement("div");
    l2.className = "ws-line";
    l2.appendChild(document.createTextNode("绝对路径（可直接粘给 opencode）："));
    const code = document.createElement("span");
    code.className = "ws-abs";
    code.textContent = absPath(b.dir);
    l2.appendChild(code);
    const cp = document.createElement("button");
    cp.className = "copy-btn";
    cp.textContent = "复制";
    cp.onclick = () => copyPath(absPath(b.dir), cp);
    l2.appendChild(cp);
    el.appendChild(l2);
    const op = document.createElement("button");
    op.className = "big";
    op.textContent = multi ? "打开「" + b.label + "」工作区"
      : "在此工作区打开 " + (state.agent_label || "AI 工具");
    op.onclick = () => openWorkspace(b.dir);
    el.appendChild(op);
    const gap = document.createElement("div");
    gap.style.height = "12px";
    el.appendChild(gap);
  });
}

function acknowledge() {
  hide("ack-btn");
  unhide("after-ack");
  document.getElementById("after-ack").scrollIntoView({behavior: "smooth"});
}

function renderAttachment() {
  const info = document.getElementById("attachment-info");
  const del = document.getElementById("attachment-del");
  if (state.attachment_name) {
    info.textContent = "已选附件：" + state.attachment_name;
    del.classList.remove("hidden");
  } else {
    info.textContent = "";
    del.classList.add("hidden");
  }
}

async function uploadAttachment() {
  const input = document.getElementById("attachment-file");
  const f = input.files[0];
  if (!f) return;
  showErr("");
  try {
    const res = await fetch("/api/attachment",
      {method: "PUT", headers: {"X-Filename": f.name}, body: f});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("上传失败 HTTP " + res.status));
    state.attachment_name = data.attachment_name;
    input.value = "";
    renderAttachment();
  } catch (e) { showErr(e.message); }
}

async function delAttachment() {
  showErr("");
  try {
    const res = await fetch("/api/attachment", {method: "DELETE"});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("请求失败 HTTP " + res.status));
    state.attachment_name = "";
    renderAttachment();
  } catch (e) { showErr(e.message); }
}

async function refresh() {
  try { state = await api("/api/state"); render(); }
  catch (e) { showErr(e.message); }
}

async function refreshSilently() {
  try { state = await api("/api/state"); render(); } catch (e) {}
}

async function login() {
  showErr("");
  try {
    state = await api("/api/login", {
      student_id: document.getElementById("student-id").value.trim(),
      password: document.getElementById("password").value,
    });
    document.getElementById("password").value = "";
    render();
  } catch (e) { showErr(e.message); }
}

async function enroll(track, questionIds) {
  showErr("");
  const body = {track};
  if (questionIds && questionIds.length) body.question_ids = questionIds;
  try { state = await api("/api/enroll", body); render(); }
  catch (e) { showErr(e.message); refreshSilently(); }
}

function trackName(key) {
  const t = allTracks.find(t => t.key === key);
  return t ? t.name : key;
}

function renderTracks() {
  const box = document.getElementById("tracks");
  const freeCreation = state.exam_mode === "free_creation";
  document.getElementById("mode-note").textContent = freeCreation
    ? "自由创作轨：默认使用「自由创作型」轨迹，请在题库中自选 1 道题作答；交卷时请在补充材料里填 GitHub 仓库链接。"
    : "";
  document.getElementById("selfselect-block").classList.remove("hidden");
  document.getElementById("pool-note").textContent = freeCreation
    ? "自由创作：每个领域下三种题型（理论 / 实操 / 自由发挥）任选其一，同一领域最多勾 1 道题，不同领域可各勾 1 道（最多 3 道）；点上面轨迹按钮则直接开始现场创作（不发具体题）。"
    : "也可以从题库中自己挑 1~3 道题（点上面轨迹按钮则为随机抽题）。";
  const list = freeCreation
    ? allTracks.filter(t => t.key === "track3")
    : allTracks;
  box.innerHTML = "";
  list.forEach(t => {
    const b = document.createElement("button");
    b.className = "track";
    b.textContent = freeCreation ? t.name + "（默认轨迹）" : t.name;
    b.onclick = () => enroll(t.key);
    box.appendChild(b);
  });
}

function checkedPoolBoxes() {
  return [...document.querySelectorAll("#question-pool input[type=checkbox]:checked")];
}

let poolDomById = {};   // 题目 id -> 领域 key（track3 池渲染时重建）

function updatePoolCount() {
  const el = document.getElementById("pool-count");
  if (!el) return;
  el.textContent = "已选 " + checkedPoolBoxes().length + "/" + POOL_MAX + " 题";
}

function trackTagLabel(track) {
  if (track === "track1") return "理论";
  if (track === "track2") return "实操";
  if (track === "track3") return "自由发挥";
  return track || "";
}

function onPoolCheckChange(ev) {
  const box = document.getElementById("question-pool");
  const ids = checkedPoolBoxes().map(c => c.value);
  const next = poolToggleIds(ids, ev.target.value,
                             ev.target.dataset.domain, poolDomById);
  box.querySelectorAll("input[type=checkbox]").forEach(c => {
    c.checked = next.indexOf(c.value) !== -1;
  });
  paintSelected();
  updatePoolCount();
}

function paintSelected() {
  document.querySelectorAll("#question-pool .pool-item").forEach(item => {
    const cb = item.querySelector("input[type=checkbox]");
    let tag = item.querySelector(".sel-tag");
    if (cb.checked) {
      if (!tag) {
        tag = document.createElement("span");
        tag.className = "sel-tag";
        tag.textContent = "已选";
        item.querySelector(".pool-row").appendChild(tag);
      }
    } else if (tag) {
      tag.remove();
    }
  });
}

function onPoolChange(ev) {
  const checked = checkedPoolBoxes();
  if (checked.length > POOL_MAX) {
    ev.target.checked = false;
    alert("最多自选 " + POOL_MAX + " 道题");
    return;
  }
  const sel = document.getElementById("pool-track");
  if (checked.length) {
    const tracks = new Set(checked.map(c => c.dataset.track));
    if (!tracks.has(sel.value)) sel.value = checked[0].dataset.track;
  }
  updatePoolCount();
}

function fillPoolTrackOptions(questions) {
  const sel = document.getElementById("pool-track");
  sel.innerHTML = "";
  [...new Set(questions.map(q => q.track))].forEach(t => {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = trackName(t);
    sel.appendChild(opt);
  });
}

function renderPoolTrack3(box, questions) {
  poolDomById = {};
  const visible = poolSortByTrack(
    poolFilterByDomains(questions, state.domain_keys || []));
  const note = document.createElement("div");
  note.className = "muted";
  note.textContent = "自由创作型：只显示你报名时选择的领域，题目按领域分组（领域内按 理论→实操→自由发挥 排列）；"
    + "每个领域最多勾 1 道题，不同领域可各勾 1 道，最多 " + POOL_MAX + " 道。";
  box.appendChild(note);
  const groups = {};
  visible.forEach(q => {
    poolDomById[q.id] = q.domain || "";
    (groups[q.domain || "未分类"] = groups[q.domain || "未分类"] || []).push(q);
  });
  for (const [domain, qList] of Object.entries(groups)) {
    const dh = document.createElement("div");
    dh.className = "pool-domain-head";
    dh.textContent = "领域：" + (DOMAIN_LABELS[domain] || domain)
      + "（共 " + qList.length + " 题）";
    box.appendChild(dh);
    qList.forEach((q, i) => {
      const item = document.createElement("div");
      item.className = "pool-item";
      const label = document.createElement("label");
      label.className = "pool-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = q.id;
      cb.dataset.domain = q.domain || "";
      cb.addEventListener("change", onPoolCheckChange);
      label.appendChild(cb);
      const badge = document.createElement("span");
      badge.className = "pool-badge " + (q.track === "track1" ? "t1"
        : q.track === "track2" ? "t2" : "t3");
      badge.textContent = trackTagLabel(q.track);
      label.appendChild(badge);
      const num = document.createElement("span");
      num.className = "qnum";
      num.textContent = "第 " + (i + 1) + " 题";
      label.appendChild(num);
      const title = document.createElement("span");
      title.className = "qtitle";
      title.textContent = q.title;
      label.appendChild(title);
      const qid = document.createElement("span");
      qid.className = "qid";
      qid.textContent = q.id || "";
      label.appendChild(qid);
      item.appendChild(label);
      const det = document.createElement("details");
      det.className = "pool-body";
      const sum = document.createElement("summary");
      sum.textContent = "展开题目全文";
      det.appendChild(sum);
      const body = document.createElement("div");
      body.className = "qbody";
      body.textContent = q.body;
      det.appendChild(body);
      item.appendChild(det);
      box.appendChild(item);
    });
  }
  if (!visible.length) {
    const none = document.createElement("div");
    none.className = "muted";
    none.textContent = "没有在你选择的领域里找到已开放的题目，请联系组织者处理。";
    box.appendChild(none);
  }
  const bar = document.createElement("div");
  bar.style.marginTop = "8px";
  bar.innerHTML = '<button onclick="enrollSelfSelectedTrack3()">确认自选开考</button> ' +
    '<span id="pool-count" class="muted"></span>';
  box.appendChild(bar);
  updatePoolCount();
}

function renderQuestionPool(questions, track3mode) {
  const box = document.getElementById("question-pool");
  box.classList.remove("hidden");
  box.textContent = "";
  if (track3mode) { renderPoolTrack3(box, questions); return; }
  const groups = {};
  questions.forEach(q => {
    const t = q.track || "未分轨";
    groups[t] = groups[t] || {};
    (groups[t][q.domain || "未分类"] = groups[t][q.domain || "未分类"] || []).push(q);
  });
  for (const [track, domains] of Object.entries(groups)) {
    const th = document.createElement("div");
    th.className = "pool-track-head";
    th.textContent = "轨迹：" + trackName(track);
    box.appendChild(th);
    for (const [domain, qList] of Object.entries(domains)) {
      const dh = document.createElement("div");
      dh.className = "pool-domain-head";
      dh.textContent = "领域：" + domain + "（共 " + qList.length + " 题）";
      box.appendChild(dh);
      qList.forEach((q, i) => {
        const item = document.createElement("div");
        item.className = "pool-item";
        const label = document.createElement("label");
        label.className = "pool-row";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.value = q.id;
        cb.dataset.track = q.track || "";
        cb.addEventListener("change", onPoolChange);
        label.appendChild(cb);
        const num = document.createElement("span");
        num.className = "qnum";
        num.textContent = "第 " + (i + 1) + " 题";
        label.appendChild(num);
        const title = document.createElement("span");
        title.className = "qtitle";
        title.textContent = q.title;
        label.appendChild(title);
        const qid = document.createElement("span");
        qid.className = "qid";
        qid.textContent = q.id || "";
        label.appendChild(qid);
        item.appendChild(label);
        const det = document.createElement("details");
        det.className = "pool-body";
        const sum = document.createElement("summary");
        sum.textContent = "展开题目全文";
        det.appendChild(sum);
        const body = document.createElement("div");
        body.className = "qbody";
        body.textContent = q.body;
        det.appendChild(body);
        item.appendChild(det);
        box.appendChild(item);
      });
    }
  }
  const bar = document.createElement("div");
  bar.style.marginTop = "8px";
  bar.innerHTML = '轨迹：<select id="pool-track"></select> ' +
    '<button onclick="enrollSelfSelected()">确认自选开考</button> ' +
    '<span id="pool-count" class="muted"></span>';
  box.appendChild(bar);
  fillPoolTrackOptions(questions);
  updatePoolCount();
}

async function loadQuestionPool() {
  showErr("");
  try {
    const questions = await api("/api/exam_questions");
    renderQuestionPool(questions, state.exam_mode === "free_creation");
  } catch (e) { showErr(e.message); }
}

async function enrollSelfSelected() {
  const ids = checkedPoolBoxes().map(c => c.value);
  const err = poolValidateStandard(ids);
  if (err) { showErr(err); return; }
  enroll(document.getElementById("pool-track").value, ids);
}

async function enrollSelfSelectedTrack3() {
  const ids = checkedPoolBoxes().map(c => c.value);
  const err = poolValidateTrack3(ids, poolDomById);
  if (err) { showErr(err); return; }
  enroll("track3", ids);
}

async function openWorkspace(dir) {
  showErr("");
  showNote("");
  try {
    const data = await api("/api/open_agent_workspace", {domain_dir: dir});
    showNote(data.hint || (data.already ? "该工作区的窗口已经开着。" : "已在对应工作区打开 AI 工具窗口。"));
  } catch (e) { showErr(e.message); }
}

async function reroll() {
  showErr("");
  try { state = await api("/api/reroll", {}); render(); }
  catch (e) { showErr(e.message); refreshSilently(); }
}

function showFinishSuccess(stateData) {
  hide("finish-busy"); hide("finish-result-fail");
  unhide("finish-result-success");
  const sid = stateData.agent_session_id || "";
  document.getElementById("success-detail").textContent =
    sid ? ("作答会话：" + sid) : "";
}

function showFinishFail(message) {
  hide("finish-busy"); hide("finish-result-success");
  document.getElementById("fail-msg").textContent = message || "交卷没有成功，请点下面的按钮重试。";
  unhide("finish-result-fail");
}

async function finish() {
  const repoUrl = document.getElementById("repo-url").value.trim();
  if (repoUrl && !/^https?:\\/\\//.test(repoUrl)) {
    showErr("仓库链接格式不正确：须以 http:// 或 https:// 开头（如 https://github.com/用户名/仓库名）");
    return;
  }
  if (!confirm("确认交卷？系统会自动找到你在 AI 工具里的作答记录，清洗后上传。")) return;
  showErr("");
  unhide("finish-busy");
  hide("finish-result-fail"); hide("finish-result-success");
  document.getElementById("finish-btn").disabled = true;
  try {
    state = await api("/api/finish", {repo_url: repoUrl});
    if (state.stage === "uploaded" || state.uploaded) showFinishSuccess(state);
    else if (state.stage === "finished") showFinishFail(state.upload_error || "");
    else render();
  } catch (e) { showFinishFail(e.message); }
  finally {
    document.getElementById("finish-btn").disabled = false;
    hide("finish-busy");
  }
}

async function finishManual(file) {
  showErr("");
  document.getElementById("manual-status").textContent = "正在清洗并上传，请稍候…";
  try {
    const text = await file.text();
    state = await fetch("/api/finish_manual", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: text,
    }).then(r => r.json().then(d => {
      if (!r.ok) throw new Error(d.error || ("请求失败 HTTP " + r.status));
      return d;
    }));
    if (state.stage === "uploaded" || state.uploaded) showFinishSuccess(state);
    else showFinishFail(state.upload_error || "");
    document.getElementById("manual-status").textContent = "";
  } catch (e) {
    document.getElementById("manual-status").textContent = "";
    showFinishFail(e.message);
  }
}

async function retryUpload() {
  showErr("");
  try {
    state = await api("/api/retry_upload", {});
    render();
  } catch (e) { showErr(e.message); }
}

fetch("/api/tracks").then(r => r.json()).then(tracks => {
  allTracks = tracks;
  renderTracks();
});
document.getElementById("attachment-file").addEventListener("change", uploadAttachment);
document.getElementById("manual-session-file").addEventListener("change", ev => {
  if (ev.target.files[0]) finishManual(ev.target.files[0]);
});
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


# ---------- HTTP 服务 ----------

class ExamHandler(BaseHTTPRequestHandler):
    server_version = "AlethicExam/0.1"

    @property
    def cfg(self):
        return self.server.cfg

    def log_message(self, fmt, *args):
        print(f"[exam_server] {self.address_string()} {fmt % args}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _error(self, exc: CloudError):
        self._send_json({"error": str(exc)}, status=exc.status or 502)

    # ---- GET ----

    def do_GET(self):
        if self.path == "/":
            return self._send_html(PAGE.replace("__APPLY_URL__", apply_url(self.cfg)))
        if self.path == "/api/tracks":
            return self._send_json(TRACKS)
        if self.path == "/api/state":
            return self._send_json(load_state())
        if self.path == "/api/exam_questions":
            return self._exam_questions()
        if self.path == "/api/health":
            return self._send_json({"ok": True})
        self._send_json({"error": "not found"}, status=404)

    # ---- POST ----

    def do_POST(self):
        try:
            if self.path == "/api/login":
                return self._login()
            if self.path == "/api/enroll":
                return self._enroll()
            if self.path == "/api/reroll":
                return self._reroll()
            if self.path == "/api/finish":
                return self._finish()
            if self.path == "/api/finish_manual":
                return self._finish_manual()
            if self.path == "/api/retry_upload":
                return self._retry_upload()
            if self.path == "/api/open_agent_workspace":
                return self._open_agent_workspace()
            self._send_json({"error": "not found"}, status=404)
        except CloudError as exc:
            self._error(exc)
        except Exception as exc:  # 考生页面需要可读错误，不暴露堆栈
            self._send_json({"error": f"本地服务出错：{exc}"}, status=500)

    def do_PUT(self):
        try:
            if self.path == "/api/attachment":
                return self._put_attachment()
            self._send_json({"error": "not found"}, status=404)
        except Exception as exc:
            self._send_json({"error": f"本地服务出错：{exc}"}, status=500)

    def do_DELETE(self):
        try:
            if self.path == "/api/attachment":
                return self._delete_attachment()
            self._send_json({"error": "not found"}, status=404)
        except Exception as exc:
            self._send_json({"error": f"本地服务出错：{exc}"}, status=500)

    def _login(self):
        body = self._read_body()
        student_id = str(body.get("student_id", "")).strip()
        password = str(body.get("password", ""))
        if not student_id:
            return self._send_json({"error": "请输入学号"}, status=400)
        if not password:
            return self._send_json({"error": "请输入密码（初始密码为学号）"}, status=400)
        cfg = self.cfg
        query = urllib.parse.urlencode(
            {"cid": cfg["campaign_id"], "student_id": student_id})
        try:
            _, salt_data = cloud_request(cfg, "GET", f"/api/auth/salt?{query}")
            client_hash = compute_client_hash(
                password, str(salt_data.get("salt", "")))
            _, login_data = cloud_request(
                cfg, "POST", f"/api/campaigns/{cfg['campaign_id']}/auth/login",
                {"student_id": student_id, "client_hash": client_hash})
        except CloudError as exc:
            if exc.status == 404:
                return self._send_json(
                    {"error": "未找到该学号的报名记录，请先填写报名问卷"}, status=404)
            if exc.status == 401:
                return self._send_json({"error": "学号或密码错误"}, status=401)
            raise
        token = str(login_data["token"])
        try:
            _, me = cloud_request(
                cfg, "GET", f"/api/campaigns/{cfg['campaign_id']}/auth/me",
                bearer=token)
        except CloudError as exc:
            if exc.status == 404:  # 旧版云端无此端点，按标准机试处理
                me = {}
            else:
                raise
        exam_mode = str(me.get("exam_mode") or "standard")
        if exam_mode not in EXAM_MODES:
            exam_mode = "standard"
        state = {
            "stage": "logged_in",
            "student_id": student_id,
            "name": str(login_data.get("name", "")),
            "pw_changed": bool(login_data.get("pw_changed")),
            "auth_token": token,
            "exam_mode": exam_mode,
            "has_session": bool(me.get("has_session")),
        }
        # 问卷所选领域 -> 建答题文件夹（云端本人可读自己的报名记录；拿不到则静默降级为空）
        domain_keys: list[str] = []
        try:
            _, app = cloud_request(
                cfg, "GET",
                f"/api/campaigns/{cfg['campaign_id']}/applications/{student_id}",
                bearer=token)
            domain_keys = [str(d) for d in (app.get("domains") or [])]
        except Exception:
            domain_keys = []
        domain_labels, domain_dirs = ensure_domain_workspaces(cfg, domain_keys)
        workdir = agent_workdir(cfg)
        state["domains"] = domain_labels
        state["domain_dirs"] = domain_dirs
        state["domain_keys"] = domain_keys
        state["workdir_abs"] = workdir.as_posix()          # opencode 可直接识别的路径
        state["workdir_human"] = human_home_label(workdir)  # 人话路径前缀（桌面\…\考试工作目录）
        state["agent_label"] = AGENT_LABELS.get(str(cfg.get("agent", "kimi")),
                                                str(cfg.get("agent", "kimi")))
        save_state(state)
        self._send_json(state)

    def _session_expired(self):
        """云端 401（登录会话过期/无效）：清本地登录态，回登录卡。"""
        state = load_state()
        state.pop("auth_token", None)
        state["stage"] = ""
        save_state(state)
        return self._send_json({"error": "登录会话已过期，请重新登录"}, status=401)

    def _exam_questions(self):
        """代理云端题库（仅 active 题目，不含 meta/评审字段），供自选题模式展示。"""
        state = load_state()
        auth_token = state.get("auth_token")
        if not auth_token:
            return self._send_json({"error": "请先登录"}, status=400)
        cfg = self.cfg
        try:
            _, questions = cloud_request(
                cfg, "GET",
                f"/api/campaigns/{cfg['campaign_id']}/exam/questions",
                bearer=auth_token)
        except CloudError as exc:
            if exc.status == 401:
                return self._session_expired()
            raise
        self._send_json(questions)

    def _enroll(self):
        state = load_state()
        auth_token = state.get("auth_token")
        if not auth_token:
            return self._send_json({"error": "请先登录"}, status=400)
        body = self._read_body()
        track = str(body.get("track", ""))
        if track not in {t["key"] for t in TRACKS}:
            return self._send_json({"error": "无效轨迹"}, status=400)
        payload = {"track": track}
        question_ids = body.get("question_ids")
        self_selected = question_ids is not None
        if self_selected:
            if (not isinstance(question_ids, list)
                    or not all(isinstance(q, str) and q.strip()
                               for q in question_ids)
                    or not 1 <= len(question_ids) <= QUESTION_IDS_MAX):
                return self._send_json(
                    {"error": f"自选题须勾选 1~{QUESTION_IDS_MAX} 道题"}, status=400)
            payload["question_ids"] = question_ids
        cfg = self.cfg
        path = f"/api/campaigns/{cfg['campaign_id']}/exam/enroll"
        try:
            _, served = cloud_request(cfg, "POST", path, payload,
                                      bearer=auth_token)
        except CloudError as exc:
            if exc.status == 401:
                return self._session_expired()
            if exc.status == 409 and isinstance(exc.detail, dict) \
                    and exc.detail.get("code") == "already_enrolled":
                served = exc.detail  # 续考：detail 里已带完整会话信息
            else:
                raise
        track_name = next((t["name"] for t in TRACKS if t["key"] == served["track"]), served["track"])
        state.update({
            "stage": "enrolled",
            "session_id": served["session_id"],
            "session_token": served["session_token"],
            "machine_id": served.get("machine_id", ""),
            "track": served["track"],
            "track_name": track_name,
            "questions": served.get("questions", []),
            "track3_guide_text": served.get("track3_guide_text", ""),
            "rerolls_left": served.get("rerolls_left", 0),
            "opened_at": state.get("opened_at") or now_iso(),
        })
        if self_selected:
            state["self_selected"] = True
        else:
            state.pop("self_selected", None)
        save_state(state)
        self._send_json(state)

    def _reroll(self):
        state = load_state()
        if state.get("stage") != "enrolled":
            return self._send_json({"error": "当前不在答题状态"}, status=400)
        if not state.get("auth_token"):
            return self._session_expired()
        cfg = self.cfg
        try:
            _, served = cloud_request(
                cfg, "POST", f"/api/campaigns/{cfg['campaign_id']}/exam/reroll",
                {"session_id": state["session_id"]},
                session_token=state["session_token"],
                bearer=state["auth_token"])
        except CloudError as exc:
            if exc.status == 401:
                return self._session_expired()
            raise
        state.update({
            "questions": served.get("questions", []),
            "rerolls_left": served.get("rerolls_left", 0),
        })
        save_state(state)
        self._send_json(state)

    def _finish(self):
        state = load_state()
        if state.get("stage") != "enrolled":
            return self._send_json({"error": "当前不在答题状态"}, status=400)
        repo_url = str(self._read_body().get("repo_url", "")).strip()
        if repo_url and not repo_url.startswith(("http://", "https://")):
            return self._send_json(
                {"error": "仓库链接格式不正确：须以 http:// 或 https:// 开头，"
                          "请修改后重新交卷（本次未交卷，会话记录未受影响）"},
                status=400)
        cfg = self.cfg
        cleaner_mod = load_cleaner()
        if cleaner_mod is None:
            return self._send_json(
                {"error": "清洗模块 cleaner.py 缺失，交卷包不完整，请联系组织者处理。"},
                status=500)
        base = f"{state['student_id']}_{state['session_id']}"
        opened_ms = iso_to_ms(state["opened_at"]) if state.get("opened_at") else 0
        try:
            raw_path, method, agent_session_id = self._locate_and_export(
                cfg, opened_ms, base)
        except SessionNotFound as exc:
            return self._send_json(
                {"error": str(exc), "hint": "manual"}, status=404)
        return self._clean_and_upload(
            state, cfg, base, raw_path, method, agent_session_id,
            repo_url, cleaner_mod)

    def _locate_and_export(self, cfg: dict, opened_ms: int, base: str):
        """定位开考后的最新会话并导出/落地原始文件。

        返回 (raw_path, method, agent_session_id)；找不到抛 SessionNotFound
        （错误信息为考生可读的白话文案）。
        """
        PENDING_DIR.mkdir(exist_ok=True)
        agent = cfg["agent"]
        if agent == "opencode":
            return self._locate_opencode_session(cfg, opened_ms, base)

        sessions = iter_kimi_sessions(kimi_home(cfg))
        candidates = pick_candidates(sessions, opened_ms)
        if not candidates:
            raise SessionNotFound(
                "没找到你的作答记录——请确认你是在「考试工作目录」文件夹里"
                "使用 Kimi Code 完成的作答（且是在点「我已理解，开始在 AI 工具里"
                "作答」之后开始的）。确实答过题的话请联系组织者处理。")
        target = candidates[0]
        out_path = PENDING_DIR / f"{base}.zip"
        method = export_session(cfg, target, out_path)
        return out_path, method, target["sessionId"]

    def _locate_opencode_session(self, cfg: dict, opened_ms: int, base: str):
        """opencode 取会话，降级链：① opencode.db（新版 SQLite 存储）→
        ② 旧版 storage/*.json 布局 / 数据目录里的导出 JSON → 手动选文件兜底
        （手动兜底在页面/接口层）。每层失败原因进日志与白话提示。"""
        home = opencode_home(cfg)
        workdir = agent_workdir(cfg)
        raw_path = PENDING_DIR / f"{base}.json"
        reasons: list[str] = []

        # 通道①：v1.18.29 起会话存 opencode.db（SQLite），首选
        db_sessions = opencode_db_sessions(home)
        if db_sessions:
            db_cands = pick_opencode_candidates(db_sessions, opened_ms, workdir)
            if db_cands:
                target = db_cands[0]
                doc, method = _opencode_document(
                    cfg, home, target["session_id"], raw_path)
                if doc is not None:
                    return raw_path, method, target["session_id"]
                reasons.append("找到了会话记录，但导出会话内容失败")
                print(f"[exam_server] opencode 会话 {target['session_id']} "
                      f"内容导出失败（export 与数据库副本两条路都没走通）")
            else:
                reasons.append("opencode 数据库里没有开考之后的会话记录")
                print("[exam_server] opencode.db 中没有开考时间之后的会话")
        elif (home / OPENCODE_DB_NAME).exists():
            reasons.append("opencode 数据库暂时无法读取")
            print(f"[exam_server] opencode.db 无法只读打开：{home / OPENCODE_DB_NAME}")
        else:
            reasons.append("本机没有找到 opencode 数据库")
            print(f"[exam_server] 没有 opencode.db：{home}")

        # 通道②：旧版 storage/*.json 布局（v1.18.29 之前）/ 手工放进数据目录的导出 JSON
        json_cands = pick_opencode_candidates(
            iter_opencode_sessions(home), opened_ms, workdir)
        if json_cands:
            target = json_cands[0]
            raw_path.write_text(
                json.dumps(target["doc"], ensure_ascii=False), encoding="utf-8")
            return raw_path, "opencode 会话文件", target["session_id"]
        reasons.append("也没有找到旧格式的会话文件")

        raise SessionNotFound(
            "没找到你的作答记录（" + "；".join(reasons) + "）——请确认你是在"
            "「考试工作目录」文件夹里使用 opencode 完成的作答（且是在点"
            "「我已理解，开始在 AI 工具里作答」之后开始的）。确实答过题的话，"
            "可稍等几秒再点一次「重试上传」，或在下方「手动选择会话文件」里选中 "
            "opencode 导出的会话 JSON 文件重试，或联系组织者处理。")

    def _clean_and_upload(self, state: dict, cfg: dict, base: str, raw_path: Path,
                          method: str, agent_session_id: str, repo_url: str,
                          cleaner_mod):
        """清洗 → 落 pending_upload → 立即上传；断网时本地留存。返回接口响应。"""
        out_dir = PENDING_DIR / base
        out_dir.mkdir(exist_ok=True)
        try:
            if hasattr(cleaner_mod, "clean_session"):
                result = cleaner_mod.clean_session(raw_path, out_dir)
            elif cfg["agent"] == "opencode" \
                    and hasattr(cleaner_mod, "clean_opencode"):
                result = cleaner_mod.clean_opencode(raw_path, out_dir)
            else:
                result = cleaner_mod.clean_export(raw_path, out_dir)
        except Exception as exc:
            print(f"[exam_server] 会话清洗失败: {exc!r}")
            return self._send_json(
                {"error": "整理你的作答记录时出了点问题，请重试一次；"
                          "仍失败请联系组织者处理。"},
                status=500)
        cleaned_jsonl = Path(result["cleaned_jsonl"])
        cleaned_md = Path(result["cleaned_md"])
        meta = {
            "campaign_id": cfg["campaign_id"],
            "session_id": state["session_id"],
            "session_token": state["session_token"],
            "student_id": state["student_id"],
            "track": state.get("track", ""),
            "agent": cfg["agent"],
            "agent_session_id": agent_session_id,
            "export_method": method,
            "export_file": Path(raw_path).name,
            "cleaned_jsonl": _rel_to_pending(cleaned_jsonl),
            "cleaned_md": _rel_to_pending(cleaned_md),
            "repo_url": repo_url,
            "attachment_name": state.get("attachment_name", ""),
            "uploaded": False,
            "stats": result.get("stats", {}),
            "finished_at": now_iso(),
        }
        save_meta(base, meta)
        state.update({
            "stage": "finished",
            "agent_session_id": agent_session_id,
            "export_method": method,
            "finished_at": meta["finished_at"],
            "pending_base": base,
            "repo_url": repo_url,
            "uploaded": False,
            "upload_pending": True,
        })
        state.pop("upload_error", None)
        try:
            upload_session(cfg, meta)
        except CloudError as exc:
            state["upload_error"] = _friendly_upload_error(exc)
            save_state(state)  # 断网兜底：本地留存，稍后可重试/自动补传
            return self._send_json(state)
        meta["uploaded"] = True
        save_meta(base, meta)
        state.update({"stage": "uploaded", "uploaded": True})
        state.pop("upload_pending", None)
        save_state(state)
        self._send_json(state)

    def _finish_manual(self):
        """兜底：考生手动选择 opencode 导出的会话 JSON 文件交卷。"""
        state = load_state()
        if state.get("stage") not in ("enrolled", "finished"):
            return self._send_json({"error": "当前不在答题状态"}, status=400)
        if not state.get("session_id"):
            return self._send_json({"error": "请先登录并领取题目"}, status=400)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            doc = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return self._send_json(
                {"error": "选择的文件不是合法的会话文件：请选择 opencode 导出的"
                          "会话 JSON 文件（文件内容以 { 开头）"}, status=400)
        if not cleaner_mod_looks_like_opencode(doc):
            return self._send_json(
                {"error": "选择的文件不是 opencode 会话导出文件。"
                          "请在 opencode 里执行导出会话后，再选择导出的 JSON 文件；"
                          "仍有问题请联系组织者。"}, status=400)
        cfg = self.cfg
        cleaner_mod = load_cleaner()
        if cleaner_mod is None:
            return self._send_json(
                {"error": "清洗模块 cleaner.py 缺失，交卷包不完整，请联系组织者处理。"},
                status=500)
        base = f"{state['student_id']}_{state['session_id']}"
        PENDING_DIR.mkdir(exist_ok=True)
        raw_path = PENDING_DIR / f"{base}.json"
        raw_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        session_id = str((doc.get("info") or {}).get("id", ""))
        return self._clean_and_upload(
            state, cfg, base, raw_path, "手动选择的会话文件", session_id,
            str(state.get("repo_url", "")), cleaner_mod)

    def _retry_upload(self):
        state = load_state()
        base = state.get("pending_base", "")
        if not base:
            return self._send_json({"error": "当前没有可重试的交卷"}, status=400)
        meta = load_meta(base)
        if meta is None:
            return self._send_json({"error": "未找到交卷记录，请联系组织者处理。"}, status=404)
        if meta.get("uploaded"):
            state.update({"stage": "uploaded", "uploaded": True})
            state.pop("upload_pending", None)
            save_state(state)
            return self._send_json(state)
        try:
            upload_session(self.cfg, meta)
        except CloudError as exc:
            state["uploaded"] = False
            state["upload_pending"] = True
            state["upload_error"] = str(exc)
            save_state(state)
            return self._send_json(state)
        meta["uploaded"] = True
        save_meta(base, meta)
        state.update({"stage": "uploaded", "uploaded": True})
        state.pop("upload_pending", None)
        state.pop("upload_error", None)
        save_state(state)
        self._send_json(state)

    def _open_agent_workspace(self):
        """在指定领域工作区目录拉起作答工具 Web（cmd /k <agent web 指令>，新控制台）。

        body: {domain_dir: "<领域文件夹名>"}。domain_dir 必须是考试工作目录的直接
        子目录（防路径穿越）；端口已占用则视为窗口已开着，不再重复拉起。
        """
        state = load_state()
        if not state.get("student_id"):
            return self._send_json({"error": "请先登录"}, status=400)
        name = str(self._read_body().get("domain_dir", "")).strip()
        if (not name or name in (".", "..") or "/" in name or "\\" in name
                or ":" in name):
            return self._send_json({"error": "无效的工作区名称"}, status=400)
        cfg = self.cfg
        workdir = agent_workdir(cfg)
        target = workdir / name
        try:
            ok = (target.is_dir()
                  and target.resolve().parent == workdir.resolve())
        except OSError:
            ok = False
        if not ok:
            return self._send_json(
                {"error": "工作区不存在，请重新登录后再试"}, status=400)
        port = agent_web_port(cfg)
        if port is not None and _port_in_use(port):
            return self._send_json(
                {"ok": True, "already": True,
                 "hint": f"该工具的窗口已经打开（端口 {port}），直接切过去即可，无需重复打开。"})
        try:
            subprocess.Popen(
                ["cmd", "/k", agent_web_cmd(cfg)], cwd=str(target),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return self._send_json({"error": f"打开工具窗口失败：{exc}"}, status=500)
        return self._send_json(
            {"ok": True, "already": False,
             "hint": f"正在「{name}」工作区打开 AI 工具窗口，请稍候片刻。"})

    def _attachment_path(self) -> Path | None:
        student_id = load_state().get("student_id")
        if not student_id:
            return None
        return PENDING_DIR / f"attachment_{student_id}.zip"

    def _put_attachment(self):
        state = load_state()
        if not state.get("student_id"):
            return self._send_json({"error": "请先登录"}, status=400)
        length = int(self.headers.get("Content-Length") or 0)
        if length > ATTACHMENT_MAX_BYTES:
            return self._send_json({"error": "附件超过 50MB 上限"}, status=413)
        filename = Path(self.headers.get("X-Filename", "").strip()).name
        if not filename.lower().endswith(".zip"):
            return self._send_json({"error": "补充材料仅支持 .zip 文件"}, status=400)
        if length <= 0:
            return self._send_json({"error": "附件内容为空"}, status=400)
        data = self.rfile.read(length)
        PENDING_DIR.mkdir(exist_ok=True)
        dest = PENDING_DIR / f"attachment_{state['student_id']}.zip"
        dest.write_bytes(data)
        state["attachment_name"] = filename
        save_state(state)
        self._send_json({"attachment_name": filename, "size": length})

    def _delete_attachment(self):
        state = load_state()
        dest = self._attachment_path()
        if dest is not None:
            dest.unlink(missing_ok=True)
        state.pop("attachment_name", None)
        save_state(state)
        self._send_json({"ok": True})


def _browser_candidates() -> list[tuple[str, str]]:
    """按用户偏好的优先级返回 (名称, exe路径)：chrome > firefox > edge > 其他常见。"""
    pairs = [
        ("Chrome", r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        ("Chrome", r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        ("Firefox", r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
        ("Firefox", r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"),
        ("Edge", r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        ("Edge", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        ("Edge", r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
        ("Brave", r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("Opera", r"%ProgramFiles%\Opera\launcher.exe"),
    ]
    out = []
    seen = set()
    for name, tmpl in pairs:
        p = os.path.expandvars(tmpl)
        if p and p not in seen and os.path.isfile(p):
            seen.add(p)
            out.append((name, p))
    return out


def open_browser(url: str) -> bool:
    """直接拉起已装浏览器（chrome>firefox>edge>其他），保证在无默认应用的机器也能开。
    webbrowser.open 走默认关联，无 http 默认应用时会弹“需要新应用打开”并“假成功”，故仅作最后兜底。"""
    for name, exe in _browser_candidates():
        try:
            subprocess.Popen([exe, url], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            continue
    # 无已装浏览器：退回系统默认关联，再退 rundll32 外壳
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    try:
        subprocess.Popen(["rundll32", "url.dll,FileProtocolHandler", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


LOG_FILE = Path(__file__).resolve().parent / "考试系统日志.txt"


def _log(msg: str) -> None:
    """把运行信息同时打印到控制台并追加到日志文件（考试系统日志.txt）。"""
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main():
    """常驻服务。错误不闪退：打印 + 写日志，窗口由 cmd /k 挂着并最后回落提示符。"""
    try:
        cfg = load_config()
        PENDING_DIR.mkdir(exist_ok=True)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", cfg["exam_port"]), ExamHandler)
        except OSError as exc:
            _log(f"启动失败：端口 {cfg['exam_port']} 可能被占用或系统拒绝绑定。\n    详情：{exc}")
            return
        server.cfg = cfg
        server.daemon_threads = True
        url = f"http://127.0.0.1:{cfg['exam_port']}"
        _log(f"机试页面: {url}")
        _log(f"云端: {cfg['cloud_base']}  届次: {cfg['campaign_id']}")
        _log(f"作答工具: {AGENT_LABELS[cfg['agent']]}  Web 指令: {agent_web_cmd(cfg)}")
        _log(f"考试工作目录: {agent_workdir(cfg)}")
        _log(">>> 如果浏览器没有自动打开，请手动在 Edge 地址栏输入：%s" % url)
        start_upload_worker()  # 断网遗留的交卷后台自动补传
        if "--no-browser" not in sys.argv:
            # 自动开默认浏览器（安装器/启动 bat 不再重复开页面）。
            # 注意：threading.Timer 不接受 name= 参数（只有 Thread 才收），勿加。
            threading.Timer(
                0.5, lambda: open_browser(url)).start()
            a_url = agent_web_url(cfg)
            if a_url:
                _log(">>> 作答工具界面地址：%s（稍后自动用 %s 打开）"
                     % (a_url, _browser_candidates()[0][0] if _browser_candidates() else "浏览器"))
                threading.Timer(
                    8.0, lambda: open_browser(a_url)).start()
        threading.current_thread().name = "main"
        _log("考试系统已启动。保持本窗口开着，考试结束时直接关闭本窗口即停止服务。")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        _log("考试系统已停止。")
    except Exception:
        _log("考试系统发生未预期错误：\n" + traceback.format_exc())
        # 不向异常抛出：配合 cmd /k 让窗口留着显示报错并回到提示符，避免闪退
        sys.exit(1)


if __name__ == "__main__":
    main()

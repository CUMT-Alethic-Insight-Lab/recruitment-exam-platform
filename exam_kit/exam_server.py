"""Alethic Insight 招新机试 — 考生本地服务（Python 3.12 纯标准库）。

流程：学号+密码登录（云端账号体系，PBKDF2 客户端散列）→ 按 auth/me 的 exam_mode
推荐默认落页（只推荐不硬拦，页面间可互相切换）→ 开考二选一：
题库自选（全量题按「你的领域/其他领域」分组展示，只能勾选报名领域的题，1~3 题，
会话 track=selected）或交自有项目（GitHub 仓库链接 + 可选 zip 附件，
multipart 直交云端即完成交卷，会话 track=project）；free_creation 分领域循环向导
（领域列表卡带「已提交 ✓/待作答」徽标 → 选一个未提交领域 → 选题或自由发挥 → 作答 →
交卷，每个领域独立提交一次、各自可带 GitHub 仓库链接；完成状态以云端 my_sessions 为准
对账，本地 state.submissions 仅作 UI 兜底；全部交完进汇总完成页）；
own_project 默认落自有项目提交页（与 standard 共用同一 multipart 直交通道）→
题目展示/换题 → 交卷（M12 起隐私优先：考生亲手导出会话文件 → 本地清洗 →
页面预览 cleaned.html/cleaned.md → 确认上传；上传只传 cleaned.jsonl，
断网时本地留存 pending_upload/ 自动补传；预览步与完成页可下载清洗结果留档，
文件名 {学号}-{领域中文名}.{后缀}）。交卷上传默认云端 HTTPS；若考试包带了
bundled tsnet-socks（tsnet 节点单文件，免管理员）且云端开放 P2P，则接入考场
tailnet 直传实验室主机接收机（复用 lan 分流协议 + SOCKS5），失败静默回退云端。
作答工具双支持（M11）：Kimi Code（wire.jsonl 导出 zip）与 opencode（会话 JSON），
由 config.json 的 agent 选择，清洗自动识别格式。
M12 隐私口径：不再默认自动扫描会话库；「在考试工作区帮我找」按钮点击后才扫描，
且只列出目录属于考试工作区的会话（opencode.db / opencode 导出 JSON / kimi 会话索引），
绝不默认读取全盘会话内容。
云端 API 见 server/app/routes/exam.py 与 server/app/routes/auth.py。
"""

import atexit
import hashlib
import json
import os
import queue
import random
import re
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
EXPORT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 手动提供的会话导出文件上限 2GB（清洗器已流式化）

# 机试包自身版本（OTA 检测用）：格式 YYYY.M.D-N，与云端
# server/static/kit-version.json 的 version 按 int 元组比较。发新安装包时
# 同步抬高这里与 kit-version.json（步骤见 README「维护」段）。
KIT_VERSION = "2026.10.13-1"

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
    # P2P 直连上传：cloud 模式下，交卷时尝试用 bundled tsnet-socks.exe
    # （tsnet 节点单文件，免管理员/免驱动/免管道）接入考场 tailnet，文件经其
    # SOCKS5 直传实验室主机 tailnet IP 上的接收机；任何一步失败都静默回退云端
    "p2p_enabled": True,
    "tailscale_dir": r"%LOCALAPPDATA%\Programs\AlethicTools\tailscale",  # bundled tsnet-socks.exe 目录
    "p2p_connect_timeout": 30,  # 等待入网 READY 的上限（秒；tsnet 内部入网超时 25s）
}

TRACKS = [
    {"key": "track1", "name": "研发型（理论/概念导向）"},
    {"key": "track2", "name": "实操型（落地实践）"},
    {"key": "track3", "name": "自由创作型（自选领域现场创作/已有项目增量）"},
]

EXAM_MODES = {"standard", "own_project", "free_creation"}
# 会话 track 在 TRACKS 之外的落库值 → 展示标签（页面/状态/对账共用）
SESSION_TRACK_LABELS = {"selected": "题库自选", "project": "自有项目"}
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
    cfg["tailscale_dir"] = os.path.expandvars(str(cfg.get("tailscale_dir") or ""))
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
    def __init__(self, message: str, status: int = 0, detail=None,
                 friendly: bool = False):
        super().__init__(message)
        self.status = status
        self.detail = detail
        self.friendly = friendly  # True = 文案已是白话，页面可直接展示


class SessionNotFound(Exception):
    """保留兼容：旧版自动定位会话流程的异常类型（M12 起交卷改手动导出，不再抛出）。"""


def _friendly_upload_error(exc: CloudError) -> str:
    """把上传失败翻译成考生能懂的白话（页面不出现术语/堆栈）。"""
    if getattr(exc, "friendly", False):
        return str(exc)  # 已是白话（如 CF 拦截提示），直接展示
    if exc.status in (0, None) or "无法连接" in str(exc):
        return ("网络不通，没能上传到云端。你的作答已安全保存在本机的"
                " pending_upload 文件夹里——恢复网络后点「重试上传」即可；"
                "保持本页开着也会自动补传。")
    return ("云端暂时没能接收你的交卷。你的作答已安全保存在本机的"
            " pending_upload 文件夹里，请稍后点「重试上传」；"
            "多次失败请联系组织者。")


# ---------- HTTP 传输层 ----------
# Cloudflare 按 TLS 指纹拦 python-urllib（实测握手即断：SSL: UNEXPECTED_EOF_WHILE_READING），
# 但透过 curl.exe（Windows 自带，Schannel 栈）正常。因此 CF 前置域名（alethicinsight.com）
# 只走 curl：网络类失败退避重试（_curl_exec_cf），绝不静默回退 urllib；urllib 兜底仅限
# 非 CF 目标（考场局域网/tailnet 直连）或显式 ALE_THIC_HTTP_TRANSPORT=urllib|curl
# 强制指定（测试用）。

CF_FRONTED_HOSTS = ("alethicinsight.com",)
CF_CURL_RETRIES = 3            # CF 前置目标的 curl 尝试次数（含首次，退避重试）
CF_CURL_BACKOFF = (2.0, 5.0)   # 第 1/2 次失败后的退避秒数
# CF 拦截特征：TLS 指纹断连 / 挑战页文案（命中即白话报错，重试无意义）
CF_BLOCK_MARKERS = ("unexpected_eof_while_reading", "cf-ray", "browser signature")


def _is_cf_fronted(url: str) -> bool:
    """url 是否指向 CF 前置域名（云端 alethicinsight.com 及其子域）。"""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in CF_FRONTED_HOSTS)


def _cf_block_detected(code: int, raw: bytes, err: str) -> bool:
    """识别 CF 拦截特征：UNEXPECTED_EOF_WHILE_READING（TLS 指纹断连）、
    403/503 挑战页里的 cf-ray / browser signature 文案。"""
    text = (err or "").lower()
    if raw:
        text += " " + bytes(raw[:8192]).decode("utf-8", "replace").lower()
    return any(m in text for m in CF_BLOCK_MARKERS)


def _cf_blocked_message() -> str:
    return ("网络拦住了到考试云端的安全连接（校园网/公司网/代理常见）。"
            "你的作答已安全保存在本机 pending_upload 文件夹，不会丢失；"
            "建议换个网络（如手机热点）后点「重试上传」，多次失败请联系组织者。")

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


def _http_transport(url: str = "") -> str:
    force = os.environ.get("ALE_THIC_HTTP_TRANSPORT", "").strip().lower()
    if force in ("curl", "urllib"):
        return force
    if url and _is_cf_fronted(url):
        return "curl"  # CF 前置只认 Schannel 栈，禁止 urllib 兜底
    return "curl" if _curl_exe() else "urllib"


CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED_PROCESS = 0x00000008 if os.name == "nt" else 0


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


def _curl_exec_cf(argv: list[str], input_data: bytes | None = None,
                  timeout: float = 60, url: str = "") -> tuple[int, bytes]:
    """CF 前置目标的 curl 调用：网络类失败按 CF_CURL_BACKOFF 退避重试
    （共 CF_CURL_RETRIES 次），绝不回退 urllib（TLS 指纹必被拦）；命中 CF 拦截
    特征直接白话报错（重试无意义）。每次尝试的传输方式与失败原因写日志。"""
    last: CloudError | None = None
    for attempt in range(1, CF_CURL_RETRIES + 1):
        try:
            code, raw = _curl_exec(argv, input_data=input_data, timeout=timeout)
        except CloudError as exc:
            last = exc
            _log(f"请求失败（传输=curl，第 {attempt}/{CF_CURL_RETRIES} 次）{url}: {exc}")
            if _cf_block_detected(0, b"", str(exc)):
                raise CloudError(_cf_blocked_message(), status=0,
                                 friendly=True) from None
            if attempt < CF_CURL_RETRIES:
                time.sleep(CF_CURL_BACKOFF[min(attempt - 1, len(CF_CURL_BACKOFF) - 1)])
            continue
        if _cf_block_detected(code, raw, ""):
            _log(f"请求返回 HTTP {code}（传输=curl，第 {attempt} 次）{url}，"
                 "特征疑似 Cloudflare 拦截")
            raise CloudError(_cf_blocked_message(), status=code,
                             friendly=True) from None
        if attempt > 1:
            _log(f"请求成功（传输=curl，第 {attempt} 次重试）{url}")
        return code, raw
    raise CloudError(f"无法连接云端（已用 curl 重试 {CF_CURL_RETRIES} 次）：{last}")


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
                  bearer: str | None = None,
                  multipart_fields: dict | None = None,
                  multipart_files: list[tuple[str, Path]] | None = None):
    """JSON/GET 之外的可选 multipart 模式：传 multipart_fields / multipart_files
    （files 为 (字段名, 文件路径)）即按 multipart/form-data 发送（自有项目直交用，
    附件可达 50MB，故超时口径与交卷上传一致，至少 300s）。"""
    url = f"{cfg['cloud_base']}{path}"
    is_multipart = multipart_fields is not None or multipart_files is not None
    data = None
    content_type = "application/json"
    if is_multipart:
        data, content_type = build_multipart(
            multipart_fields or {}, multipart_files or [])
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
    timeout = (max(float(cfg["http_timeout"]), 300) if is_multipart
               else cfg["http_timeout"])
    transport = _http_transport(url)
    if transport == "curl":
        exe = _curl_exe()
        if not exe:
            raise CloudError(
                "这台电脑缺少系统自带的 curl 组件，无法连接考试云端。"
                "你的作答已安全保存在本机 pending_upload 文件夹，不会丢失；"
                "请联系组织者处理。", status=0, friendly=True)
        argv = [exe]
        if session_token:
            argv += ["-H", f"X-Session-Token: {session_token}"]
        if bearer:
            argv += ["-H", f"Authorization: Bearer {bearer}"]
        argv += ["-H", "Accept: application/json"]
        payload = None
        if is_multipart:
            for name, value in (multipart_fields or {}).items():
                argv += ["-F", f"{name}={value}"]
            for name, fpath in (multipart_files or []):
                argv += ["-F", f"{name}=@{fpath};type=application/octet-stream"]
            argv += ["-X", method, url]
        elif data is not None:
            argv += ["-H", "Content-Type: application/json", "-X", method,
                     "--data-binary", "@-", url]
            payload = data
        else:
            argv += ["-X", method, url]
        if _is_cf_fronted(url):  # CF 前置：退避重试，绝不回退 urllib
            code, raw = _curl_exec_cf(argv, input_data=payload,
                                      timeout=timeout, url=url)
        else:
            code, raw = _curl_exec(argv, input_data=payload, timeout=timeout)
        if code >= 200 and code < 300:
            return code, json.loads(raw.decode("utf-8")) if raw else (code, {})
        raise _parse_cloud_error(code, raw)
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = content_type
    if session_token:
        headers["X-Session-Token"] = session_token
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    云端只收轻量标记）；cloud 模式下先试 P2P 直连（tailnet 内的实验室接收机，
    见 _P2P），不可用再直传云端。成功（含 409 already_uploaded 幂等）直接返回；
    401/4xx/5xx/网络错误抛 CloudError。"""
    if cfg.get("upload_mode") == "lan":
        return upload_session_lan(cfg, meta)
    p2p_cfg = _P2P.effective_cfg(cfg)
    if p2p_cfg is not None:
        try:
            upload_session_lan(p2p_cfg, meta)
            return
        except CloudError as exc:
            # tailnet 链路中途失败：文件仍在本机 pending_upload，回退云端直传，
            # 本会话后续上传也不再走 P2P
            _log(f"P2P 上传中途失败，回退云端直传: {exc}")
            _P2P.mark_fallback()
    cleaned = PENDING_DIR / meta["cleaned_jsonl"]
    if not cleaned.exists():
        raise CloudError(f"清洗文件缺失：{meta['cleaned_jsonl']}")
    fields = {"session_id": str(meta["session_id"])}
    slot_index = meta.get("slot_index")
    if slot_index is not None:
        fields["slot"] = str(slot_index)  # 逐题提交：云端按题各存一份
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
    # 上传用独立的长超时：几 MB 的清洗件在慢上行的家用宽带上远超 10s 的
    # 普通请求超时（2026-09 实际踩过：curl (28) 10s 超时）
    upload_timeout = max(float(cfg["http_timeout"]), 300)
    if _http_transport(url) == "curl":
        exe = _curl_exe()
        if not exe:
            raise CloudError(
                "这台电脑缺少系统自带的 curl 组件，无法连接考试云端。"
                "你的作答已安全保存在本机 pending_upload 文件夹，不会丢失；"
                "请联系组织者处理。", status=0, friendly=True)
        argv = [exe, "-H", f"X-Session-Token: {meta['session_token']}",
                "-H", "Accept: application/json"]
        for name, value in fields.items():
            argv += ["-F", f"{name}={value}"]
        for name, path in files:
            argv += ["-F", f"{name}=@{path};type=application/octet-stream"]
        argv.append(url)
        if _is_cf_fronted(url):  # CF 前置：退避重试，绝不回退 urllib
            code, raw = _curl_exec_cf(argv, timeout=upload_timeout, url=url)
        else:
            code, raw = _curl_exec(argv, timeout=upload_timeout)
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
        with urllib.request.urlopen(req, timeout=upload_timeout) as resp:
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

def _lan_http_curl(base: str, method: str, path: str, data: bytes | None,
                   headers: dict | None, timeout: float, socks5: str) -> dict:
    """经 SOCKS5 代理访问接收机的 curl 传输（P2P 直连专用：urllib 不支持 SOCKS，
    强制 curl 且绝不回落 urllib）。失败抛 CloudError。"""
    exe = _curl_exe()
    if not exe:
        raise CloudError("P2P 直连上传需要系统 curl.exe（经 SOCKS5 代理），未找到")
    argv = [exe, "--socks5-hostname", socks5, "-H", "Accept: application/json"]
    for k, v in (headers or {}).items():
        argv += ["-H", f"{k}: {v}"]
    argv += ["-X", method]
    if data is not None:
        argv += ["--data-binary", "@-"]
    argv.append(base.rstrip("/") + path)
    code, raw = _curl_exec(argv, input_data=data, timeout=timeout)
    if 200 <= code < 300:
        return json.loads(raw.decode("utf-8")) if raw else {}
    try:
        detail = json.loads(raw.decode("utf-8")).get("detail")
    except ValueError:
        detail = None
    raise CloudError(str(detail) if detail else f"接收机返回错误（HTTP {code}）",
                     status=code) from None


def lan_http(base: str, method: str, path: str, data: bytes | None = None,
             headers: dict | None = None, timeout: float = 60,
             socks5: str | None = None) -> dict:
    """请求考场局域网接收机（session_receiver.py）。成功返回 JSON；
    失败抛 CloudError（连接类错误文案含「无法连接」，供 _friendly_upload_error 识别）。
    socks5 非空时经该 SOCKS5 代理（P2P 直连 tailnet 用），强制 curl 传输。"""
    if socks5:
        return _lan_http_curl(base, method, path, data, headers, timeout, socks5)
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
    socks5 = str(cfg.get("_p2p_socks5") or "") or None  # P2P 直连时经 tailnet SOCKS5
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
             headers={"Content-Type": "application/json"}, timeout=timeout,
             socks5=socks5)
    uploads: list[tuple[str, Path]] = []
    slot_index = meta.get("slot_index")
    suffix = f"_q{slot_index}" if slot_index is not None else ""
    uploads.append((f"cleaned_jsonl{suffix}", PENDING_DIR / meta["cleaned_jsonl"]))
    if meta.get("cleaned_md"):
        uploads.append((f"cleaned_md{suffix}", PENDING_DIR / meta["cleaned_md"]))
    if meta.get("attachment_name") and slot_index in (None, 0):
        uploads.append(("attachment",
                        PENDING_DIR / f"attachment_{meta['student_id']}.zip"))
    for kind, path in uploads:
        if not path.exists():
            if kind.startswith("cleaned_jsonl"):
                raise CloudError(f"清洗文件缺失：{path.name}")
            continue  # md / 附件缺失不算错（与云端上传口径一致）
        lan_http(base, "POST", f"{api}/file/{kind}", data=path.read_bytes(),
                 headers={"X-Filename": path.name}, timeout=300, socks5=socks5)
    lan_http(base, "POST", f"{api}/done",
             data=json.dumps({"repo_url": repo_url}).encode("utf-8"),
             headers={"Content-Type": "application/json"}, timeout=timeout,
             socks5=socks5)
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


# ---------- P2P 直连上传（bundled tsnet-socks + Headscale 考场 mesh） ----------
# cloud 模式下，首次交卷时惰性尝试：向云端取一次性 p2p-key（申请人 Bearer）→
# 起 tsnet-socks.exe（tsnet 节点嵌进单文件：免管理员/免 TUN/免命名管道——stock
# tailscaled 的 IPC 管道 SDDL 硬编码 Administrators，非提权 error 1307 直接退出，
# 故不可用）→ 读 stdout 等 READY <tailnet-ip>（SOCKS5 已在 127.0.0.1 监听）→
# 复用 upload_session_lan 把文件直传实验室主机 tailnet IP。任何一步失败只记日志
# 并回退云端直传，且本会话内不再重试 P2P。

class P2PUnavailable(Exception):
    """P2P 直连不可用的内部信号：一律回退云端上传，不打扰考生。"""


def _p2p_hostname(state: dict) -> str:
    """节点名：exam-<学号>（DNS 安全字符，ephemeral 节点在 headscale 上可辨认）。"""
    sid = re.sub(r"[^a-z0-9-]+", "-",
                 str(state.get("student_id") or "").lower()).strip("-")
    return f"exam-{sid}" if sid else "exam-candidate"


class P2PManager:
    """tailnet 接入的生命周期管理（惰性接入 / 状态查询 / 退出清理）。全部尽力而为。"""

    EXE_NAME = "tsnet-socks.exe"
    SOCKS_PORT = 1055

    def __init__(self):
        self._attempted = False    # 本会话已尝试过接入（成败都不再自动重试）
        self._connected = False
        self._proc = None          # tsnet-socks 子进程
        self._lab_ip = ""
        self._receiver_port = 0
        self._tailnet_ip = ""      # 本节点分到的 tailnet IP（READY 行带回）
        self._socks5 = ""          # 实际监听的 SOCKS5 地址（1055 被占时探测空闲端口）
        self._lock = threading.Lock()
        self._atexit_done = False
        # 测试可覆盖到临时目录，避免往真实考试包目录写
        self.state_dir = BASE_DIR / "ts_state"

    def status(self) -> str:
        """供 /api/state 上报："connected"=已接入 tailnet；"fallback"=试过但失败，
        上传走云端；"off"=未启用/尚未尝试。"""
        if self._connected:
            return "connected"
        if self._attempted:
            return "fallback"
        return "off"

    def mark_fallback(self) -> None:
        """P2P 传输中途失败：本会话后续上传固定走云端（进程留待退出时清理）。"""
        self._connected = False

    def effective_cfg(self, cfg: dict) -> dict | None:
        """首次上传时惰性接入 tailnet；成功返回指向实验室 tailnet 接收机的 lan 分流
        cfg（含 _p2p_socks5 代理），不可用/失败返回 None（调用方走云端直传）。"""
        if not cfg.get("p2p_enabled", True):
            return None
        if self._connected:
            return self._lan_cfg(cfg)
        with self._lock:
            if self._attempted:
                return None
            self._attempted = True
            try:
                self._join(cfg)
            except Exception as exc:
                _log(f"P2P 直连不可用，本次及本会话后续上传走云端: {exc}")
                self._teardown()
                return None
        _log(f"P2P 直连已建立（本节点 {self._tailnet_ip}，SOCKS5 {self._socks5}）："
             f"交卷经 tailnet 直传实验室接收机 "
             f"http://{self._lab_ip}:{self._receiver_port}")
        return self._lan_cfg(cfg)

    def _lan_cfg(self, cfg: dict) -> dict:
        out = dict(cfg)
        out["upload_mode"] = "lan"
        out["lan_receiver_base"] = f"http://{self._lab_ip}:{self._receiver_port}"
        out["_p2p_socks5"] = self._socks5
        return out

    def _pick_socks_addr(self) -> str:
        """默认 127.0.0.1:1055；被占则向系统要一个空闲端口。"""
        port = self.SOCKS_PORT
        if _port_in_use(port):
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
        return f"127.0.0.1:{port}"

    def _join(self, cfg: dict) -> None:
        if os.name != "nt":
            raise P2PUnavailable("非 Windows 环境")
        if not _curl_exe():
            raise P2PUnavailable("系统缺少 curl.exe，无法经 SOCKS5 代理传输")
        exe = Path(str(cfg.get("tailscale_dir") or "")) / self.EXE_NAME
        if not exe.is_file():
            raise P2PUnavailable(f"未找到 bundled {self.EXE_NAME}（{exe.parent}）")
        state = load_state()
        token = str(state.get("auth_token") or "")
        if not token:
            raise P2PUnavailable("尚未登录，没有申请人 token")
        try:
            _, key_data = cloud_request(
                cfg, "POST",
                f"/api/campaigns/{cfg['campaign_id']}/exam/p2p-key", bearer=token)
        except CloudError as exc:
            if exc.status == 503:  # 云端未开放 P2P：静默回退，不算故障
                raise P2PUnavailable("云端未开放 P2P 直连（p2p unavailable）") from None
            raise P2PUnavailable(f"p2p-key 获取失败: {exc}") from None
        login_server = str(key_data.get("login_server") or "").strip()
        auth_key = str(key_data.get("auth_key") or "").strip()
        lab_ip = str(key_data.get("lab_ip") or "").strip()
        try:
            receiver_port = int(key_data.get("receiver_port") or 0)
        except (TypeError, ValueError):
            receiver_port = 0
        if not (login_server and auth_key and lab_ip and receiver_port):
            raise P2PUnavailable(f"p2p-key 响应缺字段: {key_data!r}")
        self._lab_ip, self._receiver_port = lab_ip, receiver_port
        self._socks5 = self._pick_socks_addr()
        try:
            self.state_dir.mkdir(exist_ok=True)
            self._proc = self._spawn([
                str(exe),
                "-control-url", login_server,
                "-auth-key", auth_key,
                "-hostname", _p2p_hostname(state),
                "-state-dir", str(self.state_dir),
                "-socks5", self._socks5,
                "-timeout", "25s"])
        except OSError as exc:
            raise P2PUnavailable(f"{self.EXE_NAME} 拉起失败: {exc}") from None
        if not self._atexit_done:
            atexit.register(self.teardown)
            self._atexit_done = True
        timeout = max(float(cfg.get("p2p_connect_timeout") or 30), 1)
        self._tailnet_ip = self._await_ready(timeout)
        self._connected = True

    def _spawn(self, argv: list[str]):
        """拉起 tsnet-socks：无窗口后台驻留；stdout 逐行等 READY/FAIL，
        stderr 由独立线程 drain 进 kit 日志（考试系统日志.txt）。"""
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS)
        threading.Thread(target=self._drain_stderr, args=(proc,),
                         daemon=True).start()
        return proc

    def _drain_stderr(self, proc) -> None:
        """[tsnet] 调试日志逐行转进 kit 日志；进程退出/管道关闭即结束。"""
        try:
            for line in proc.stderr or []:
                line = line.rstrip()
                if line:
                    _log(f"tsnet-socks: {line}")
        except (OSError, ValueError):
            pass

    def _await_ready(self, timeout: float) -> str:
        """逐行读 stdout 等 READY <ip> / FAIL ...，整体超时 timeout 秒。
        成功返回 tailnet IP；否则抛 P2PUnavailable。"""
        lines: queue.Queue = queue.Queue()

        def pump():
            try:
                for line in self._proc.stdout or []:
                    lines.put(line)
            except (OSError, ValueError):
                pass
            finally:
                lines.put(None)  # EOF 哨兵：进程退出且输出耗尽

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise P2PUnavailable(f"等待 {self.EXE_NAME} 入网超时"
                                     f"（{timeout:.0f}s）")
            try:
                line = lines.get(timeout=min(0.5, remaining))
            except queue.Empty:
                if self._proc.poll() is not None:
                    raise P2PUnavailable(
                        f"{self.EXE_NAME} 已退出（code={self._proc.returncode}）") \
                        from None
                continue
            if line is None:
                raise P2PUnavailable(
                    f"{self.EXE_NAME} 输出通道关闭"
                    f"（code={self._proc.poll()}）")
            line = line.strip()
            if line.startswith("READY"):
                ip = line[5:].strip()
                if not ip:
                    raise P2PUnavailable(f"{self.EXE_NAME} READY 行缺 tailnet IP")
                return ip
            if line.startswith("FAIL"):
                raise P2PUnavailable(f"{self.EXE_NAME} 入网失败: {line}")

    def teardown(self) -> None:
        """kit 退出清理：terminate tsnet-socks（ephemeral 节点，强杀也会由
        headscale 自动蒸发）→ 删 ts_state。绝不抛异常。"""
        try:
            with self._lock:
                self._teardown()
        except Exception:
            pass

    def _teardown(self) -> None:
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        try:
            shutil.rmtree(self.state_dir, ignore_errors=True)
        except OSError:
            pass
        self._connected = False


_P2P = P2PManager()


def p2p_status() -> str:
    return _P2P.status()


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
            if _slot_count(state) <= 1 \
                    and state.get("pending_base") == base \
                    and not state.get("uploaded"):
                _advance_after_upload(state, meta)
                save_state(state)
        time.sleep(1)
    # 多题考试：已登记槽位全部传完后收敛到完成页
    state = load_state()
    slots = state.get("slots") or {}
    if _slot_count(state) > 1 and slots and not state.get("uploaded"):
        metas = [load_meta(str(info["base"]))
                 for info in slots.values()
                 if isinstance(info, dict) and info.get("base")]
        if metas and all(m is not None and m.get("uploaded") for m in metas):
            _advance_after_upload(state, metas[-1])
            save_state(state)


# 手动重试/交卷上传与后台补传互斥：同一件交卷绝不并发重传
_upload_lock = threading.Lock()

UPLOAD_RETRY_INTERVAL = 60.0        # 自动补传轮询周期（秒）
UPLOAD_RETRY_JITTER_RATIO = 0.1     # 周期加少量抖动，防多台考试机扎堆


def _has_unuploaded_meta() -> bool:
    """pending_upload 里是否还有 uploaded!=true 的 meta（没有就空转休眠）。"""
    try:
        for meta_path in PENDING_DIR.glob("**/*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not meta.get("uploaded"):
                return True
    except OSError:
        pass
    return False


def _upload_worker(stop: threading.Event, interval: float) -> None:
    """周期补传循环：启动即跑第一趟，之后每 interval（+抖动）扫一遍 pending_upload，
    有待补传项才重传。单轮异常只记日志，循环不死；stop 置位后尽快退出（测试用）。"""
    while not stop.is_set():
        try:
            if _has_unuploaded_meta():
                with _upload_lock:
                    retry_pending_uploads()
        except Exception as exc:
            _log(f"自动补传轮次异常（下轮继续）: {exc}")
        stop.wait(interval + random.uniform(0, interval * UPLOAD_RETRY_JITTER_RATIO))


def start_upload_worker(interval: float = UPLOAD_RETRY_INTERVAL) -> None:
    """后台周期补传：断网/上传失败的交卷，联网后无需考生操作自动重传。"""
    stop = threading.Event()  # 常驻 daemon，随进程退出，不主动停
    threading.Thread(target=_upload_worker, args=(stop, interval),
                     name="upload-retry", daemon=True).start()


# ---------- OTA 新版本检测（非阻断：后台 daemon，任何失败只记日志） ----------
# 启动约 5s 后首查、之后每 30min 一查 {cloud_base}/static/kit-version.json；
# 比本包 KIT_VERSION 新 → 写 state["kit_update"]，页面顶部出现可关闭的非模态
# 横幅（考生可无视继续考试）。「下载新版」经 curl（CF 加固通道）把新安装包
# 下到「下载」文件夹，sha256 非空时校验，不符即删文件报白话；绝不自动替换
# 运行中的 exe、绝不自动重启/退出考试进程。考场断网可用
# ALE_THIC_KIT_UPDATE=off 整体关闭检测。

KIT_UPDATE_FIRST_DELAY = 5.0        # 启动后首查延迟（秒）
KIT_UPDATE_INTERVAL = 30 * 60.0     # 之后每 30min 一查
KIT_UPDATE_JITTER_RATIO = 0.05      # 周期抖动，防多台考试机扎堆
KIT_UPDATE_ENV = "ALE_THIC_KIT_UPDATE"  # =off 时整体关闭检测（考场断网场景）
KIT_DOWNLOAD_TIMEOUT = 600          # 安装包下载超时（秒）


def _kit_update_enabled() -> bool:
    return os.environ.get(KIT_UPDATE_ENV, "").strip().lower() not in (
        "off", "0", "false")


def _kit_version_tuple(v: str) -> tuple[int, ...] | None:
    """"YYYY.M.D-N" → (2026, 10, 13, 1)；格式不符返回 None。"""
    m = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d{1,2})-(\d+)", str(v).strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups())


def kit_version_newer(remote: str, local: str = KIT_VERSION) -> bool:
    """remote 版本是否比 local 新（int 元组比较，跨月/跨年自然正确）。"""
    r, l = _kit_version_tuple(remote), _kit_version_tuple(local)
    return r is not None and l is not None and r > l


def check_kit_update(cfg: dict) -> dict | None:
    """GET {cloud_base}/static/kit-version.json，比 KIT_VERSION 新则返回
    {version, url, notes, sha256}，否则 None。网络失败抛 CloudError
    （调用方静默吞掉）；响应不是合法 JSON 记日志后按「无更新」处理。"""
    try:
        _, data = cloud_request(cfg, "GET", "/static/kit-version.json")
    except ValueError:
        _log("机试包版本信息不是合法 JSON，本次检测跳过")
        return None
    if not isinstance(data, dict):
        _log(f"机试包版本信息结构异常，本次检测跳过: {type(data).__name__}")
        return None
    version = str(data.get("version") or "").strip()
    if not version or not kit_version_newer(version):
        return None
    return {
        "version": version,
        "url": str(data.get("url") or "").strip(),
        "notes": str(data.get("notes") or ""),
        "sha256": str(data.get("sha256") or "").strip(),
    }


def _kit_update_worker(stop: threading.Event, interval: float,
                       first_delay: float = KIT_UPDATE_FIRST_DELAY) -> None:
    """版本检测循环：先等 first_delay 首查，之后每 interval（+抖动）一查。
    单轮任何异常只记日志，循环不死；stop 置位后尽快退出（测试用）。"""
    if stop.wait(first_delay):
        return
    while not stop.is_set():
        try:
            info = check_kit_update(load_config())
            state = load_state()
            if info:
                old = state.get("kit_update") or {}
                # 同一版本复检：保住已下载完成的记录（不覆盖 downloaded_path）
                if old.get("version") == info["version"] \
                        and old.get("downloaded_path"):
                    info["downloaded_path"] = old["downloaded_path"]
                state["kit_update"] = info
                save_state(state)
                _log(f"检测到机试包新版本 {info['version']}"
                     f"（当前 {KIT_VERSION}），页面将提示下载")
            elif state.pop("kit_update", None) is not None:
                save_state(state)  # 云端版本回落/撤下：摘掉过期横幅
        except Exception as exc:
            _log(f"机试包版本检测失败（静默，下轮继续）: {exc}")
        stop.wait(interval + random.uniform(
            0, interval * KIT_UPDATE_JITTER_RATIO))


def start_kit_update_worker(interval: float = KIT_UPDATE_INTERVAL,
                            first_delay: float = KIT_UPDATE_FIRST_DELAY) -> None:
    """后台版本检测：不阻断启动，检测失败对考试零影响。"""
    if not _kit_update_enabled():
        _log(f"机试包版本检测已关闭（{KIT_UPDATE_ENV}=off）")
        return
    stop = threading.Event()  # 常驻 daemon，随进程退出，不主动停
    threading.Thread(target=_kit_update_worker,
                     args=(stop, interval, first_delay),
                     name="kit-update-check", daemon=True).start()


def _known_folder(guid_str: str) -> Path | None:
    """SHGetKnownFolderPath 取 Windows 已知文件夹（兼容 OneDrive 重定向，
    与 installer 取真实桌面同一做法）；非 Windows 或失败返回 None。"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import byref, c_ulong, c_void_p
        from ctypes import c_ubyte as _c_ubyte
        u = uuid.UUID(guid_str)

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", _c_ubyte * 8)]

        guid = _GUID(u.fields[0], u.fields[1], u.fields[2],
                     (_c_ubyte * 8)(*u.bytes[8:]))
        p = c_void_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(
            byref(guid), 0, None, byref(p))
        if hr != 0 or not p.value:
            return None
        try:
            return Path(ctypes.wstring_at(p))
        finally:
            ctypes.windll.ole32.CoTaskMemFree(p)
    except Exception:
        return None


def downloads_dir() -> Path:
    """考生「下载」文件夹：FOLDERID_Downloads（OneDrive 重定向也拿真路径），
    拿不到回退 ~/Downloads。"""
    d = _known_folder("374DE290-123F-4565-9164-39C4925E467B")
    return d if d is not None else Path.home() / "Downloads"


def _friendly_download_error(exc: CloudError) -> str:
    """新版安装包下载失败的白话（与 _friendly_upload_error 同款口径，
    强调不影响继续考试）。"""
    if getattr(exc, "friendly", False):
        return str(exc)
    if exc.status in (0, None) or "无法连接" in str(exc):
        return ("网络不通，没能下载新版安装包。不影响继续考试；"
                "恢复网络后再点「下载新版」即可。")
    return ("新版安装包暂时下载失败。不影响继续考试；"
            "稍后再点「下载新版」重试，多次失败请联系组织者。")


def _resolve_kit_download_url(cfg: dict, info: dict) -> str:
    """kit-version.json 的 url 为 "auth-required:kit-token" 时，带考生 Bearer
    调 lab 签发端点换短时签名 URL（R2 私有 + Worker 验签，见
    docs/PROPOSAL_r2_download_gating.md）；裸 http(s) 直链保持旧行为（过渡期兼容）。
    403（窗口关闭/签名过期）等由 cloud_request 的 _parse_cloud_error 透出白话 detail。"""
    url = str(info.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        return url  # 旧格式：公开直链（Worker 上线前的旧包/旧清单）
    if url == "auth-required:kit-token":
        token = str(load_state().get("auth_token") or "")
        if not token:
            raise CloudError("请先在机试页面登录后，再下载新版安装包。", friendly=True)
        _, data = cloud_request(
            cfg, "POST",
            f"/api/campaigns/{cfg['campaign_id']}/exam/kit-token", bearer=token)
        signed = str(data.get("url") or "").strip()
        if signed.startswith(("http://", "https://")):
            return signed
    raise CloudError("新版安装包的下载地址缺失或不正确，请联系组织者处理。",
                     friendly=True)


def download_kit_installer(cfg: dict, info: dict,
                           dest_dir: Path | None = None) -> Path:
    """把新版安装包下载到「下载」文件夹（curl 传输，CF 前置走加固重试通道）。
    sha256 非空时校验，不符即删文件抛白话 CloudError。成功返回落盘路径。"""
    url = _resolve_kit_download_url(cfg, info)
    dest_dir = Path(dest_dir) if dest_dir is not None else downloads_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _sanitize_filename(
        f"AlethicInsight机试安装包-{info.get('version') or 'new'}") + ".exe"
    dest = dest_dir / name
    if _http_transport(url) == "curl":
        exe = _curl_exe()
        if not exe:
            raise CloudError("这台电脑缺少系统自带的 curl 组件，无法下载新版安装包；"
                             "不影响继续考试，请联系组织者处理。", friendly=True)
        argv = [exe, "-H", "Accept: application/octet-stream", url]
        if _is_cf_fronted(url):  # CF 前置：退避重试，绝不回退 urllib
            code, raw = _curl_exec_cf(argv, timeout=KIT_DOWNLOAD_TIMEOUT, url=url)
        else:
            code, raw = _curl_exec(argv, timeout=KIT_DOWNLOAD_TIMEOUT)
        if not 200 <= code < 300:
            raise CloudError(f"下载失败（HTTP {code}）", status=code)
        dest.write_bytes(raw)
    else:
        try:  # 非 CF 目标的 urllib 兜底
            with urllib.request.urlopen(url, timeout=KIT_DOWNLOAD_TIMEOUT) as resp:
                dest.write_bytes(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise CloudError(f"无法连接下载地址：{exc}") from None
    want = str(info.get("sha256") or "").strip().lower()
    if want:
        got = hashlib.sha256(dest.read_bytes()).hexdigest()
        if got != want:
            dest.unlink(missing_ok=True)
            raise CloudError("下载的安装包校验不一致（文件可能已损坏），"
                             "已自动删除该文件，请重新点「下载新版」；"
                             "多次失败请联系组织者。", friendly=True)
    return dest


def _open_in_folder(path: Path) -> bool:
    """在文件管理器里打开文件所在文件夹并选中该文件（Windows explorer /select），
    失败回退打开所在文件夹/浏览器兜底链。"""
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except OSError:
        pass
    try:
        if os.name == "nt":
            os.startfile(str(path.parent))  # noqa: S606 仅打开本地文件夹
            return True
    except OSError:
        pass
    try:
        return open_browser(path.parent.as_uri())
    except (OSError, ValueError):
        return False



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


# ---------- M12 考试工作区会话发现（仅在考生点击后执行） ----------

def scan_workdir_candidates(cfg: dict, since_ms: int) -> list[dict]:
    """列出目录属于考试工作区的作答会话候选（新的在前）。

    M12 隐私口径：本函数只应在考生主动点击「在考试工作区帮我找」后调用；
    严格限定会话目录在考试工作区内（_opencode_under），不读取其它会话的内容。
    候选结构：{kind, session_id, directory, updated, source, file?, session_dir?}，
    kind ∈ {"kimi", "opencode_db", "opencode_json"}。"""
    workdir = agent_workdir(cfg)
    cands: list[dict] = []
    if cfg["agent"] == "opencode":
        home = opencode_home(cfg)
        for s in opencode_db_sessions(home):
            if s["updated"] >= since_ms and s.get("directory") \
                    and _opencode_under(s["directory"], workdir):
                cands.append({
                    "kind": "opencode_db",
                    "session_id": s["session_id"],
                    "directory": s["directory"],
                    "updated": s["updated"],
                    "source": "opencode 数据库",
                })
        for s in iter_opencode_sessions(home):
            if s["updated"] >= since_ms and s.get("directory") \
                    and _opencode_under(s["directory"], workdir):
                cands.append({
                    "kind": "opencode_json",
                    "session_id": s["session_id"],
                    "directory": s["directory"],
                    "updated": s["updated"],
                    "file": s["source"] if str(s["source"]).endswith(".json") else "",
                    "source": "opencode 会话文件",
                })
    else:
        for s in pick_candidates(iter_kimi_sessions(kimi_home(cfg)), since_ms):
            if s.get("workDir") and _opencode_under(s["workDir"], workdir):
                cands.append({
                    "kind": "kimi",
                    "session_id": s["sessionId"],
                    "session_dir": s["sessionDir"],
                    "directory": s["workDir"],
                    "updated": s["createdAt"],
                    "source": "Kimi Code 会话索引",
                })
    cands.sort(key=lambda c: c["updated"], reverse=True)
    return cands


def _agent_session_id_from_export(path: Path) -> str:
    """尽力从导出文件里取 agent 侧会话 id（opencode info.id / kimi zip 内 state.json 的 id）。"""
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as z:
                for member in ("state.json", "manifest.json"):
                    try:
                        obj = json.loads(z.read(member).decode("utf-8"))
                    except (KeyError, ValueError):
                        continue
                    if isinstance(obj, dict):
                        sid = str(obj.get("id") or obj.get("sessionId")
                                  or obj.get("session_id") or "")
                        if sid:
                            return sid
            return ""
        if path.stat().st_size > 512 * 1024 * 1024:
            return ""  # 大 JSON 不整读；清洗入口的体积护栏会给明确报错
        obj = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(obj, dict):
            return str((obj.get("info") or {}).get("id", ""))
    except (OSError, ValueError, zipfile.BadZipFile):
        pass
    return ""


# ---------- 逐题提交槽（每道题独立清洗、独立上传，云端按题各存一份） ----------

def _slot_count(state: dict) -> int:
    """本次开考的题目数（自由发挥无题按 1 个槽位计）。"""
    return max(len(state.get("questions") or []), 1)


def _slot_base(state: dict, idx: int) -> str:
    """槽位 idx 的 pending base。第 1 题（idx=0）沿用旧命名，保证单题流程不变。"""
    base = f"{state.get('student_id', '')}_{state.get('session_id', '')}"
    return base if idx == 0 else f"{base}_q{idx}"


def _slot_question(state: dict, idx: int) -> dict:
    """槽位对应的题目；自由发挥（无题）时返回伪题（领域取 enroll 时所选）。"""
    qs = state.get("questions") or []
    if 0 <= idx < len(qs):
        return qs[idx]
    domain = (state.get("domain_tags") or [""])[0]
    return {"domain": domain, "title": "自由发挥（自定题目创作）", "body": ""}


def _registered_slot_bases(state: dict) -> set[str]:
    out = set()
    for info in (state.get("slots") or {}).values():
        if isinstance(info, dict) and info.get("base"):
            out.add(str(info["base"]))
    return out


def _upload_slot_entries(state: dict, cfg: dict, repo_url: str
                         ) -> tuple[list[str], dict | None]:
    """多题考试交卷：逐题各自上传（不合并），云端按槽位各存一份清洗件。

    每题独立幂等（云端按槽位 409 视为成功）；附件 zip 只随第一份实际上传的
    槽位走，避免 50MB 附件重复传输。返回 (失败题号列表, 最后一份成功的 meta)。"""
    n = _slot_count(state)
    slots = state.get("slots") or {}
    entries: list[tuple[int, str, dict]] = []
    for i in range(n):
        info = slots.get(str(i))
        if not isinstance(info, dict) or not info.get("base"):
            continue
        meta = load_meta(str(info["base"]))
        if meta is not None:
            entries.append((i, str(info["base"]), meta))
    failures: list[str] = []
    last_meta: dict | None = None
    attach_pending = bool(state.get("attachment_name"))
    for i, base, meta in entries:
        if meta.get("uploaded"):
            last_meta = last_meta or meta
            continue
        meta["repo_url"] = repo_url
        if attach_pending:
            meta["attachment_name"] = str(state.get("attachment_name") or "")
        else:
            meta["attachment_name"] = ""
        save_meta(base, meta)
        try:
            upload_session(cfg, meta)
        except CloudError as exc:
            print(f"[exam_server] 第 {i + 1} 题上传失败: {exc}")
            failures.append(str(i + 1))
            continue
        meta["uploaded"] = True
        save_meta(base, meta)
        attach_pending = False
        last_meta = meta
    return failures, last_meta



# ---------- 按领域循环交卷：状态对账 / 交卷后去向 / 留档下载 ----------

def submissions_from_sessions(sessions: list[dict]) -> dict:
    """云端 my_sessions 响应 → {领域key: 提交信息}。

    只认 uploaded 会话（draft/voided 不算完成）；同一多领域会话的每个领域
    共享同一条提交信息。服务端为准，本地 state.submissions 仅作 UI 兜底。"""
    subs: dict = {}
    for s in sessions or []:
        if not isinstance(s, dict) or s.get("status") != "uploaded":
            continue
        tags = [str(x) for x in (s.get("domain_tags") or []) if x]
        if not tags:
            continue
        info = {"session_id": str(s.get("session_id") or ""),
                "uploaded_at": str(s.get("uploaded_at") or ""),
                "repo_url": str(s.get("repo_url") or ""),
                "domains": tags}
        for d in tags:
            subs[d] = info
    return subs


def merge_submissions(local: dict, server_subs: dict) -> dict:
    """以服务端为准重建 submissions，保留本地同会话记录的 pending_base
    （完成页「下载清洗结果留档」按钮要读本机文件，云端响应里没有）。"""
    merged: dict = {}
    for d, info in (server_subs or {}).items():
        keep = dict(info)
        old = (local or {}).get(d) or {}
        if (old.get("pending_base") and old.get("session_id")
                and old.get("session_id") == info.get("session_id")):
            keep["pending_base"] = old["pending_base"]
        merged[d] = keep
    return merged


_EXAM_SESSION_KEYS = (
    "session_id", "session_token", "machine_id", "track", "track_name",
    "questions", "domain_tags", "track3_guide_text", "rerolls_left",
    "self_selected", "pending_base", "uploaded", "upload_pending",
    "upload_error", "agent_session_id", "export_method", "opened_at",
    "finished_at", "repo_url", "attachment_name", "slots",
)


def _clear_exam_fields(state: dict) -> None:
    """清掉「一轮作答」的会话字段（free_creation 交完一个领域回领域列表时调用）。"""
    for k in _EXAM_SESSION_KEYS:
        state.pop(k, None)


def _advance_after_upload(state: dict, meta: dict | None = None) -> None:
    """上传成功后的去向收敛（_upload_cleaned / _retry_upload / 补传 worker 共用）。

    free_creation：本次提交按领域记入 state.submissions（含 repo_url 与
    pending_base），还有未提交领域则清场回领域列表卡（logged_in），
    全部交完才进完成页（uploaded）；其它模式一锤定音进完成页。"""
    meta = meta or {}
    state["uploaded"] = True
    state.pop("upload_pending", None)
    state.pop("upload_error", None)
    if state.get("exam_mode") != "free_creation":
        state["stage"] = "uploaded"
        return
    domains = [str(d) for d in (state.get("domain_tags") or []) if d]
    if domains:
        subs = dict(state.get("submissions") or {})
        info = {"session_id": str(state.get("session_id") or ""),
                "uploaded_at": now_iso(),
                "repo_url": str(meta.get("repo_url") or state.get("repo_url") or ""),
                "pending_base": str(state.get("pending_base") or ""),
                "domains": domains}
        for d in domains:
            subs[d] = info
        state["submissions"] = subs
        remaining = [k for k in (state.get("domain_keys") or []) if k not in subs]
        if remaining:
            _clear_exam_fields(state)
            state["stage"] = "logged_in"
            return
    state["stage"] = "uploaded"


def _sanitize_filename(name: str) -> str:
    """下载文件名安全化：去路径分隔符与 Windows 非法字符（保留中文与括号）。"""
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "-")
    return name.strip(" .") or "exam"


def _content_disposition(filename: str) -> str:
    """RFC 5987：中文文件名走 filename*=UTF-8''，附 ASCII 兜底名（老浏览器）。"""
    fallback = re.sub(r"[^\x20-\x7e]", "_", filename).replace('"', "'")
    return ("attachment; filename=\"" + fallback + "\"; filename*=UTF-8''"
            + urllib.parse.quote(filename))


def _download_domain_label(state: dict, base: str) -> str:
    """下载文件名里的领域中文名：题槽位取该题领域；其次取该次提交所选领域
    （track3 领域），拿不到则回退 exam_mode 的人话名。"""
    m = re.search(r"_q(\d+)$", base or "")
    if m:
        d = str(_slot_question(state, int(m.group(1))).get("domain") or "")
        if d:
            return DOMAIN_LABELS.get(d, d)
    tags: list = []
    if base and base == str(state.get("pending_base") or ""):
        tags = [str(d) for d in (state.get("domain_tags") or []) if d]
    if not tags:
        for info in (state.get("submissions") or {}).values():
            if isinstance(info, dict) and str(info.get("pending_base") or "") == base:
                tags = [str(d) for d in (info.get("domains") or []) if d]
                break
    if tags:
        return DOMAIN_LABELS.get(tags[0], tags[0])
    mode = str(state.get("exam_mode") or "")
    return {"free_creation": "自由创作", "own_project": "个人项目"}.get(
        mode, mode or "机试")


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
  #kit-update-banner { font-size: 14px; color: #5a4a00; background: #fdf6e3;
            border: 1px solid #e0c97f; border-radius: 8px; margin: 0 0 12px;
            padding: 8px 14px; line-height: 1.7; }
  #kit-update-banner .close { float: right; background: none; color: #8a7a3a;
            font-size: 17px; padding: 0 6px; }
  #kit-update-banner .dl { background: #b8860b; font-size: 14px; padding: 4px 14px;
            border-radius: 6px; margin-left: 10px; }
  #kit-update-done-text { word-break: break-all; }
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
  .pool-row input[type=radio] { width: 20px; height: 20px; flex: none; }
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
<div id="kit-update-banner" class="hidden">
  <button class="close" title="关闭本提示" onclick="dismissKitUpdate()">✕</button>
  <b id="kit-update-text"></b>
  <span class="muted">（不影响当前考试，可稍后再处理）</span>
  <button id="kit-update-btn" class="dl" onclick="downloadKitUpdate()">下载新版</button>
  <span id="kit-update-msg" class="err" style="font-size:13px"></span>
  <div id="kit-update-done" class="hidden">
    <div id="kit-update-done-text"></div>
    <button class="secondary" onclick="openKitUpdateFolder()">打开所在文件夹</button>
  </div>
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
  <h2>交自有项目</h2>
  <div id="ownproject-who" class="muted"></div>
  <p style="font-size:17px">提交你<b>公开的 GitHub 仓库</b>链接即完成交卷；
    可选附一个 zip 补充材料（≤50MB）。此方式无需在本机选题作答。</p>
  <input id="project-repo-url"
         placeholder="GitHub 仓库链接（必填），如 https://github.com/用户名/仓库名">
  <div style="margin-top:8px">
    <input type="file" id="project-attachment-file" accept=".zip"
           style="max-width:430px;font-size:15px;padding:8px">
    <span id="project-attachment-info" class="muted"></span>
  </div>
  <button class="big" id="project-submit-btn" style="background:#1e8449;max-width:430px"
          onclick="submitOwnProject()">提交自有项目，完成交卷</button>
  <p class="muted" style="margin-top:14px">提醒：请确认仓库是<b>公开</b>的，评审老师需要能直接打开；
    提交成功后会进入完成页。提交遇到问题？也可以到招新平台账号页提交：
    <button class="secondary" onclick="openAccountPage()">打开账号页</button></p>
  <p class="muted"><a href="javascript:void(0)" style="color:#2456c8"
     onclick="backToQuestionMode()">改为做题（从题库选题作答）→</a></p>
</div>

<div id="stage-wizard" class="card hidden">
  <h2><span class="step-tag">第 2 步</span>分领域选题交卷</h2>
  <div id="wizard-who" class="muted"></div>
  <p class="muted" style="font-size:13px;margin:2px 0 0;text-align:right">
    想交自己的项目？<a href="javascript:void(0)" style="color:#888"
    onclick="enterProjectMode()">改为交自有项目（GitHub 仓库）→</a></p>
  <div id="wizard-step1">
    <p style="font-size:18px"><b>第 1 小步：选择一个领域</b></p>
    <p class="muted">你在报名问卷里选过下面这些领域——<b>每个领域要各自作答、独立交卷一次</b>。点一个「待作答」的领域进入下一步。</p>
    <div id="wizard-progress" class="pending"></div>
    <div id="wizard-domains"></div>
  </div>
  <div id="wizard-step2" class="hidden">
    <p style="font-size:18px"><b>第 2 小步：选一道题，或者直接自由发挥</b></p>
    <div id="wizard-domain-head" class="pool-domain-head"></div>
    <div id="wizard-questions"></div>
    <div style="margin-top:12px">
      <button class="secondary" onclick="wizardBack()">← 重选领域</button>
      <button id="wizard-confirm" class="big" style="max-width:360px"
              onclick="wizardConfirm()">确认开考</button>
      <div id="wizard-hint" class="muted"></div>
    </div>
  </div>
</div>

<div id="stage-track" class="card hidden">
  <h2><span class="step-tag">第 2 步</span>选择作答方式</h2>
  <div id="who" class="muted"></div>
  <div id="pw-note" class="pending hidden">你仍在使用初始密码，建议到招新平台网页端修改密码。</div>
  <div id="mode-note" class="muted"></div>
  <div id="mode-cards">
    <button class="track" onclick="enterPoolMode()">
      <b>做题：从题库选题作答</b>
      <div style="font-size:14px;opacity:.85;margin-top:6px">从全量题库中挑 1~3 道题作答——只能选你报名问卷里报过的领域；其他领域的题可以浏览，不能作答。</div>
    </button>
    <button class="track" onclick="enterProjectMode()">
      <b>交自有项目：GitHub 仓库 + 可选附件</b>
      <div style="font-size:14px;opacity:.85;margin-top:6px">已有自己的项目？提交公开 GitHub 仓库链接（可附一个 zip 补充材料）即完成交卷，无需做题。</div>
    </button>
  </div>
  <div id="selfselect-block" class="hidden">
    <p class="muted"><a href="javascript:void(0)" style="color:#2456c8"
       onclick="backToModeChoice()">← 返回选择作答方式</a></p>
    <p id="pool-note" class="muted">勾选 1~3 道「你的领域」里的题，然后点页面底部的「确认自选开考」。</p>
    <div id="question-pool" class="hidden"></div>
  </div>
</div>

<div id="stage-exam" class="hidden">
  <div class="card">
    <h2><span class="step-tag">第 3 步</span><span id="exam-title">题目</span></h2>
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
      <h2><span class="step-tag">第 4 步</span>交卷</h2>
      <div class="card" style="background:#f4f8ff;border-color:#2456c8">
        <b style="font-size:17px">第 1 小步：在每道题的工作区里答完，并导出该题的作答会话</b>
        <p style="font-size:15px">本考试系统<b>不会自动翻找你电脑里的历史会话</b>。<b>每道题各自导出一次</b>会话，
          然后在第 2 小步对应题目的格子里清洗。最省事的方式：答完回到本页，点该题格子里的
          <b>「在本题工作区帮我找」</b>，系统自动定位会话（只找考试工作区内的，不碰其它目录）。</p>
        <details style="font-size:15px;line-height:1.8;margin:4px 0">
          <summary style="cursor:pointer;color:#2563eb"><b>手动导出的操作步骤</b>（自动查找找不到时用）</summary>
          <ul style="margin:8px 0">
            <li><b>opencode</b>：在该题工作区文件夹的地址栏输入 <code>cmd</code> 回车，打开一个终端；执行
              <code>opencode session list</code>，在列表里找到本次作答的会话（一般是最上面那条，
              标题是首条提问，核对 Updated 时间），复制它的 Session ID（<code>ses_</code> 开头那串）；
              再执行 <code>opencode export 会话ID &gt; session.json</code>，
              会话内容就保存成了该文件夹里的 <code>session.json</code>。<br>
              <span class="muted">注意：opencode 会话里输入 <code>/export</code> 导出的是
              Markdown 文档（.md），<b>不能</b>用来交卷——必须用上面的 JSON 导出。</span></li>
            <li><b>Kimi Code</b>：在 Kimi Code 会话里输入 <code>/export-debug-zip</code>，
              或在终端执行 <code>kimi export</code>，都会产出一个 .zip 文件。<br>
              <span class="muted">注意：单输 <code>/export</code> 导出的是 Markdown 文档（.md），<b>不能</b>用来交卷。</span></li>
          </ul>
          <p class="muted" style="font-size:14px">导出的文件建议保存在该题自己的工作区文件夹里（好找、好对号）。
            实在导出不了，也可以点格子里的「粘贴会话 JSON」，把 <code>opencode export</code>
            打印在终端里的内容原样粘进去。</p>
        </details>
      </div>
      <div id="finish-step-pick">
        <p style="font-size:17px"><b>第 2 小步：逐题清洗</b><span class="muted">
          ——每道题各占一格，各选各的会话文件，互不影响；只上传清洗后的结果，原始会话文件不出你的电脑</span></p>
        <div id="slot-list"></div>
      </div>
      <div id="finish-step-preview" class="hidden">
        <p style="font-size:17px"><b>预览清洗结果</b></p>
        <p class="muted" id="preview-note"></p>
        <iframe id="preview-frame" title="清洗结果预览"
                style="width:100%;height:420px;border:1px solid #ccc;border-radius:8px;background:#fff"></iframe>
        <pre id="preview-text" class="hidden"
             style="max-height:420px;overflow:auto;border:1px solid #ccc;border-radius:8px;
                    padding:12px;font-size:14px;white-space:pre-wrap;background:#fff"></pre>
        <div>
          <button class="secondary" onclick="downloadCleaned(lastPreviewBase)">下载该题清洗结果留档</button>
          <button class="secondary" onclick="hide('finish-step-preview')">收起预览</button>
        </div>
      </div>
      <div id="finish-step-upload">
        <p style="font-size:17px"><b>第 3 小步：确认没有不该上传的内容后，点上传</b>
          <span id="slot-progress" class="muted"></span></p>
        <button id="upload-btn" class="huge" style="background:#1e8449"
                onclick="uploadCleaned()">确认上传</button>
        <p class="muted">已清洗的每道题会<b>各自作为独立记录</b>上传，评审按题查看；
          没有清洗的题目视为未作答。上传前请逐题预览确认。</p>
      </div>
      <div id="finish-busy" class="pending hidden" style="font-size:18px;text-align:center">
        <span class="spinner"></span><span id="finish-busy-text">正在处理，请勿关闭页面…</span>
      </div>
      <div id="finish-result-success" class="hidden">
        <div class="big-ok">✔ 交卷成功！</div>
        <p style="font-size:17px">你的作答记录已上传到云端，考试完成。</p>
        <p class="muted" id="success-detail"></p>
      </div>
      <div id="finish-result-fail" class="failbox hidden">
        <div class="msg" id="fail-msg"></div>
        <button id="retry-btn" class="big" onclick="retryUpload()">重试上传</button>
        <p class="muted" style="margin-top:14px">也可以回到上面重新选择导出的会话文件，
          再次清洗、预览、上传。</p>
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
  <div id="done-summary"></div>
  <p class="muted" id="done-detail"></p>
  <p class="muted">如需重考请联系组织者作废会话后重新登录。</p>
</div>

<script>
let state = {};
let lastPreviewBase = "";    // 最近一次清洗的 pending_base（预览步「下载留档」用）
let lastMySessionsSync = 0;  // my_sessions 对账节流时间戳（ms）
let repoDirty = false;       // 仓库链接框被手改过后，不再被 5s 轮询的 state 覆盖
let preferredView = "";      // 登录后落页覆盖："project"/"track"/"wizard"，空 = 按 exam_mode 推荐
let poolOpen = false;        // 作答方式页内：题库自选块是否展开（5s 轮询不把它收回去）
const POOL_MAX = 3;
// 会话 track 在 track1/2/3 之外的落库值 → 展示标签（页面/状态/完成页共用）
const SESSION_TRACK_LABELS = {selected: "题库自选", project: "自有项目"};
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
function poolSplitByDomains(questions, keys) {
  // 题库自选分组：报名领域的题可作答，其余仅浏览（禁用勾选）
  const own = [], others = [];
  const ownKeys = keys || [];
  (questions || []).forEach(q => {
    if (ownKeys.indexOf(q.domain || "") !== -1) own.push(q);
    else others.push(q);
  });
  return {own: own, others: others};
}
function wizardQuestionsForDomain(questions, domain) {
  if (!domain) return [];
  return poolSortByTrack(poolFilterByDomains(questions, [domain]));
}
function wizardValidate(domain, pick) {
  if (!domain) return "请先选择领域";
  if (!pick) return "请选择一道题，或选择「自由发挥」";
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

/* OTA 新版本横幅：state.kit_update 由后台检测线程写入；考生可关闭/无视，
   关闭按版本号记忆（sessionStorage，仅本次浏览器会话）。绝不自动下载/重启。 */
function renderKitUpdate() {
  const el = document.getElementById("kit-update-banner");
  if (!el) return;
  const info = state.kit_update;
  if (!info || !info.version
      || sessionStorage.getItem("kitUpdateDismissed") === info.version) {
    el.classList.add("hidden");
    return;
  }
  el.classList.remove("hidden");
  document.getElementById("kit-update-text").textContent =
    "新机试包版本 " + info.version + " 可用" + (info.notes ? "：" + info.notes : "");
  const done = document.getElementById("kit-update-done");
  const btn = document.getElementById("kit-update-btn");
  if (info.downloaded_path) {
    btn.classList.add("hidden");
    done.classList.remove("hidden");
    document.getElementById("kit-update-done-text").textContent =
      "已下载到「下载」文件夹：" + info.downloaded_path
      + "。请先完全关闭本考试系统（关掉黑色窗口），再运行新的安装程序完成升级。";
  } else {
    btn.classList.remove("hidden");
    done.classList.add("hidden");
  }
}

function dismissKitUpdate() {
  const info = state.kit_update || {};
  if (info.version) sessionStorage.setItem("kitUpdateDismissed", info.version);
  hide("kit-update-banner");
}

async function downloadKitUpdate() {
  const btn = document.getElementById("kit-update-btn");
  const msg = document.getElementById("kit-update-msg");
  msg.textContent = "";
  btn.disabled = true;
  msg.textContent = "正在下载新版安装包，请稍候…";
  msg.style.color = "#5a4a00";
  try {
    const data = await api("/api/kit_update/download", {});
    if (data.kit_update) { state.kit_update = data.kit_update; }
    msg.textContent = "";
    renderKitUpdate();
  } catch (e) {
    msg.style.color = "";
    msg.textContent = e.message;
  } finally { btn.disabled = false; }
}

async function openKitUpdateFolder() {
  const msg = document.getElementById("kit-update-msg");
  msg.textContent = "";
  try { await api("/api/kit_update/open_folder", {}); }
  catch (e) { msg.textContent = e.message; }
}

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
  renderKitUpdate();
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
    renderDoneSummary();
  } else if (stage === "enrolled") {
    unhide("stage-exam");
    renderExam();
  } else if (stage === "logged_in") {
    // exam_mode 只做默认落页推荐（preferredView 可互相切换，不硬拦）：
    // own_project→自有项目提交页，free_creation→分领域向导，standard→作答方式选择
    const view = preferredView || (state.exam_mode === "own_project" ? "project"
      : state.exam_mode === "free_creation" ? "wizard" : "track");
    if (view === "project") {
      unhide("stage-ownproject");
      document.getElementById("ownproject-who").textContent =
        state.name + "（" + state.student_id + "）";
      renderProjectAttachment();
    } else if (view === "wizard") {
      unhide("stage-wizard");
      document.getElementById("wizard-who").textContent =
        state.name + "（" + state.student_id + "）· 自由创作轨";
      renderWizard();
    } else {
      unhide("stage-track");
      document.getElementById("who").textContent =
        state.name + "（" + state.student_id + "），请选择作答方式：";
      document.getElementById("pw-note").classList.toggle("hidden", !!state.pw_changed);
      renderModeChoice();
    }
  } else {
    unhide("stage-login");
  }
}

function renderExam() {
  let metaLine = state.name + "（" + state.student_id + "）· 作答方式："
    + (state.track_name || SESSION_TRACK_LABELS[state.track] || state.track);
  if (state.track === "track3") {
    const d = (state.domain_tags || [])[0] || ((state.questions || [])[0] || {}).domain || "";
    if (d) metaLine += " · 本次领域：" + (DOMAIN_LABELS[d] || d);
  }
  document.getElementById("exam-meta").textContent = metaLine;
  const qs = state.questions || [];
  const freeCreate = state.track === "track3" && qs.length === 0;
  document.getElementById("exam-title").textContent = freeCreate ? "自由创作" : "题目";
  const guide = document.getElementById("guide");
  guide.textContent = "";
  if (freeCreate) {
    // 自由发挥开考：引导语 + 工作区卡，无题面正文
    const domain = (state.domain_tags || [])[0] || "";
    const label = DOMAIN_LABELS[domain] || domain || "所选";
    guide.classList.remove("hidden");
    const b = document.createElement("b");
    b.style.fontSize = "18px";
    b.textContent = "自由发挥（不选题）· 领域：" + label;
    guide.appendChild(b);
    const p1 = document.createElement("p");
    p1.style.fontSize = "16px";
    p1.textContent = "请在该领域的工作区里，用 AI 工具自定一个题目进行创作"
      + "（可以是一个小工具、一次实验、一个 Demo，或一份调研报告）。";
    guide.appendChild(p1);
    const p2 = document.createElement("p");
    p2.style.fontSize = "16px";
    p2.textContent = "你的产出将通过本次作答的会话轨迹评估——请认真与 AI 协作完成，"
      + "创作过程与最终结果同样重要。";
    guide.appendChild(p2);
    if (state.track3_guide_text) {
      const p3 = document.createElement("p");
      p3.className = "muted";
      p3.textContent = state.track3_guide_text;
      guide.appendChild(p3);
    }
  } else if (state.track3_guide_text) {
    guide.textContent = state.track3_guide_text;
    guide.classList.remove("hidden");
  } else {
    guide.classList.add("hidden");
  }
  const box = document.getElementById("questions");
  box.innerHTML = "";
  qs.forEach((q, i) => {
    const div = document.createElement("div");
    div.innerHTML = "<b style=\\"font-size:19px\\">Q" + (i + 1) + " [" + q.domain + "] " + q.title + "</b>" +
      '<div class="qbody"></div><hr>';
    div.querySelector(".qbody").textContent = q.body;
    box.appendChild(div);
  });
  const selfSel = !!state.self_selected || state.track === "selected";
  const rerollBtn = document.getElementById("reroll-btn");
  rerollBtn.classList.toggle("hidden", selfSel || freeCreate);
  rerollBtn.disabled = !(state.rerolls_left > 0) || state.track === "track3"
    || state.track === "selected";
  document.getElementById("rerolls-left").textContent = freeCreate ? "" : (selfSel
    ? "自选题模式（发题后如需换题请联系组织者）"
    : "剩余换题次数：" + (state.rerolls_left ?? 0));
  const repoInput = document.getElementById("repo-url");
  if (!repoDirty && document.activeElement !== repoInput) repoInput.value = state.repo_url || "";
  if (state.exam_mode === "free_creation") {
    document.getElementById("repo-hint").textContent =
      "本领域的 GitHub 仓库链接（可选，留空跳过）：每次交卷都会单独问一次，随当次会话记录一并提交。";
  }
  renderAttachment();
  renderWorkspaceCard();
  renderSlots();
}

function renderWorkspaceCard() {
  const el = document.getElementById("workspace-card");
  const qs = state.questions || [];
  if (state.track !== "track3") {
    el.classList.add("hidden");
    return;
  }
  // 选题开考：按题目领域列工作区；自由发挥开考：按 enroll 时所选领域列工作区
  const seen = {};
  const blocks = [];
  const pushDomain = (domain) => {
    if (!domain || seen[domain]) return;
    seen[domain] = true;
    const dir = (state.domain_dirs || [])[(state.domain_keys || []).indexOf(domain)];
    if (dir) blocks.push({domain: domain, dir: dir,
                          label: DOMAIN_LABELS[domain] || domain});
  };
  if (qs.length) qs.forEach(q => pushDomain(q.domain || ""));
  else (state.domain_tags || []).forEach(pushDomain);
  if (!blocks.length) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.textContent = "";
  const multi = blocks.length > 1;
  blocks.forEach(b => {
    const head = document.createElement("div");
    head.style.fontSize = "18px";
    head.style.fontWeight = "bold";
    head.textContent = qs.length
      ? "本题为 " + b.label + " 领域，请在对应工作区作答"
      : "你的创作领域是 " + b.label + "，请在该工作区作答";
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

function slotList() {
  // 交卷槽位：每道考题一格；自由发挥（无题）按一个「自由发挥」槽位
  const qs = state.questions || [];
  if (qs.length) return qs.map((q, i) => ({q: q, i: i}));
  const d = (state.domain_tags || [])[0] || "";
  return [{q: {domain: d, title: "自由发挥（自定题目创作）"}, i: 0}];
}

function slotDir(q) {
  const idx = (state.domain_keys || []).indexOf(q.domain || "");
  return idx >= 0 ? (state.domain_dirs || [])[idx] : "";
}

let slotSig = "";  // 槽位结构签名：只看题目结构；清洗状态原地更新，不重建 DOM（保住正在选择的文件）

function updateSlotStatus(i, info) {
  // 原地刷新某一题的清洗状态与按钮文字，不碰文件输入框
  const st = document.getElementById("slot-status-" + i);
  if (st) {
    st.textContent = "";
    if (info && info.base) {
      const ok = document.createElement("span");
      ok.style.color = "#1e8449";
      ok.style.fontWeight = "bold";
      const s = info.stats || {};
      const parts = [];
      if (s.user !== undefined) parts.push("用户输入 " + s.user + " 条");
      if (s.assistant !== undefined) parts.push("助手回复 " + s.assistant + " 段");
      if (s.tool !== undefined) parts.push("工具调用 " + s.tool + " 次");
      ok.textContent = "✔ 已清洗" + (parts.length ? "（" + parts.join("、") + "）" : "");
      st.appendChild(ok);
      const pv = document.createElement("button");
      pv.className = "secondary";
      pv.textContent = "预览";
      pv.onclick = () => previewSlot(i);
      st.appendChild(pv);
      const dl = document.createElement("button");
      dl.className = "secondary";
      dl.textContent = "下载留档";
      dl.onclick = () => downloadCleaned(info.base);
      st.appendChild(dl);
    } else {
      const wait = document.createElement("span");
      wait.className = "muted";
      wait.textContent = "未清洗";
      st.appendChild(wait);
    }
  }
  const btn = document.getElementById("slot-cleanbtn-" + i);
  if (btn) btn.textContent = info && info.base ? "重新清洗本题" : "清洗本题";
}

function renderSlots(force) {
  const el = document.getElementById("slot-list");
  if (!el) return;
  const slots = state.slots || {};
  const items = slotList();
  const sig = JSON.stringify(
    items.map(x => (x.q.domain || "") + "|" + (x.q.title || "")));
  const prog = document.getElementById("slot-progress");
  const cleanedCount = Object.keys(slots).filter(k => slots[k] && slots[k].base).length;
  if (prog) prog.textContent = "（已清洗 " + cleanedCount + "/" + items.length + " 题）";
  const upBtn = document.getElementById("upload-btn");
  if (upBtn) upBtn.textContent = cleanedCount
    ? "确认上传（逐题上传 " + cleanedCount + " 道题的会话）" : "确认上传";
  if (!force && sig === slotSig) {
    // 结构没变：只原地更新各题状态，保留正在选择的文件
    items.forEach(({i}) => updateSlotStatus(i, slots[String(i)]));
    return;
  }
  slotSig = sig;
  el.textContent = "";
  items.forEach(({q, i}) => {
    const info = slots[String(i)];
    const card = document.createElement("div");
    card.className = "card";
    card.style.background = "#fbfcff";
    const h = document.createElement("b");
    h.style.fontSize = "16px";
    h.textContent = "第 " + (i + 1) + " 题 [" + (DOMAIN_LABELS[q.domain] || q.domain || "—")
      + "] " + (q.title || "");
    card.appendChild(h);
    const dir = slotDir(q);
    if (dir) {
      const line = document.createElement("div");
      line.className = "ws-line";
      line.appendChild(document.createTextNode("本题工作区：" + humanPath(dir) + "（"));
      const code = document.createElement("span");
      code.className = "ws-abs";
      code.textContent = absPath(dir);
      line.appendChild(code);
      line.appendChild(document.createTextNode("）"));
      const cp = document.createElement("button");
      cp.className = "copy-btn";
      cp.textContent = "复制路径";
      cp.onclick = () => copyPath(absPath(dir), cp);
      line.appendChild(cp);
      card.appendChild(line);
    }
    const st = document.createElement("div");
    st.style.margin = "6px 0";
    st.id = "slot-status-" + i;
    card.appendChild(st);
    const row = document.createElement("div");
    const file = document.createElement("input");
    file.type = "file";
    file.accept = ".json,.zip";
    file.id = "slot-file-" + i;
    file.style.cssText = "max-width:430px;font-size:15px;padding:8px";
    row.appendChild(file);
    const cleanBtn = document.createElement("button");
    cleanBtn.className = "big";
    cleanBtn.style.maxWidth = "240px";
    cleanBtn.id = "slot-cleanbtn-" + i;
    cleanBtn.textContent = info && info.base ? "重新清洗本题" : "清洗本题";
    cleanBtn.onclick = () => cleanSlotFile(i);
    row.appendChild(cleanBtn);
    card.appendChild(row);
    const row2 = document.createElement("div");
    row2.style.marginTop = "6px";
    const scan = document.createElement("button");
    scan.className = "secondary";
    scan.textContent = "在本题工作区帮我找";
    scan.onclick = () => scanSlotSessions(i);
    row2.appendChild(scan);
    const det = document.createElement("details");
    det.style.marginTop = "6px";
    const sum = document.createElement("summary");
    sum.style.cssText = "cursor:pointer;color:#2563eb;font-size:14px";
    sum.textContent = "没有文件？粘贴会话 JSON";
    det.appendChild(sum);
    const ta = document.createElement("textarea");
    ta.rows = 5;
    ta.id = "slot-paste-" + i;
    ta.style.cssText = "width:100%;max-width:640px;font-size:13px;font-family:Consolas,monospace";
    ta.placeholder = "把 opencode export 输出的 JSON 全文粘贴到这里";
    det.appendChild(ta);
    const pb = document.createElement("button");
    pb.className = "secondary";
    pb.textContent = "清洗我粘贴的内容";
    pb.onclick = () => cleanSlotPasted(i);
    det.appendChild(pb);
    row2.appendChild(det);
    card.appendChild(row2);
    const scanBox = document.createElement("div");
    scanBox.id = "slot-scan-" + i;
    scanBox.className = "hidden";
    card.appendChild(scanBox);
    el.appendChild(card);
  });
  items.forEach(({i}) => updateSlotStatus(i, slots[String(i)]));
}

const FREE_PICK = "__free__";
let wizardDomain = "";    // 向导第 1 小步已选领域 key
let wizardPick = "";      // 向导第 2 小步已选：题目 id 或 FREE_PICK
const wizardCache = {};   // 领域 key -> 该领域题目列表（避免每次渲染重拉题库）

function renderWizard() {
  maybeSyncMySessions();
  const subs = state.submissions || {};
  const keys = state.domain_keys || [];
  const prog = document.getElementById("wizard-progress");
  if (prog) {
    const doneCount = keys.filter(k => subs[k]).length;
    prog.textContent = keys.length
      ? "进度：已提交 " + doneCount + "/" + keys.length + " 个领域（每个领域独立交卷一次）"
      : "";
  }
  // 当前选中的领域刚交完卷（或被对账标记已提交）：退回第 1 小步
  if (wizardDomain && subs[wizardDomain]) { wizardDomain = ""; wizardPick = ""; }
  const dbox = document.getElementById("wizard-domains");
  dbox.textContent = "";
  keys.forEach((k, i) => {
    const label = (state.domains || [])[i] || DOMAIN_LABELS[k] || k;
    const dir = (state.domain_dirs || [])[i] || "";
    const info = subs[k];
    const b = document.createElement("button");
    b.className = "track";
    if (wizardDomain === k) b.style.outline = "3px solid #1e8449";
    b.textContent = label;
    const badge = document.createElement("span");
    badge.className = "pool-badge " + (info ? "t2" : "t3");
    badge.style.marginLeft = "10px";
    badge.textContent = info ? "已提交 ✓" : "待作答";
    b.appendChild(badge);
    const sub = document.createElement("div");
    sub.style.fontSize = "13px";
    sub.style.opacity = "0.85";
    sub.textContent = "工作区：" + humanPath(dir);
    b.appendChild(sub);
    if (info && info.repo_url) {
      const repo = document.createElement("div");
      repo.style.fontSize = "13px";
      repo.style.opacity = "0.85";
      repo.textContent = "仓库：" + info.repo_url;
      b.appendChild(repo);
    }
    if (info) b.disabled = true;
    else b.onclick = () => wizardChooseDomain(k);
    dbox.appendChild(b);
  });
  if (!keys.length) {
    const none = document.createElement("div");
    none.className = "err";
    none.textContent = "没有拿到你报名时选择的领域，请重新登录；仍不行请联系组织者。";
    dbox.appendChild(none);
  }
  const step2 = document.getElementById("wizard-step2");
  if (!wizardDomain) { step2.classList.add("hidden"); return; }
  step2.classList.remove("hidden");
  document.getElementById("wizard-domain-head").textContent =
    "领域：" + (DOMAIN_LABELS[wizardDomain] || wizardDomain);
  const qbox = document.getElementById("wizard-questions");
  qbox.textContent = "";
  const qs = wizardCache[wizardDomain];
  if (!qs) {
    const loading = document.createElement("div");
    loading.className = "muted";
    loading.textContent = "正在加载这个领域的题目…";
    qbox.appendChild(loading);
    return;
  }
  if (!qs.length) {
    const none = document.createElement("div");
    none.className = "muted";
    none.textContent = "这个领域暂时没有已开放的题目——可以直接选下面的「自由发挥」。";
    qbox.appendChild(none);
  }
  qs.forEach((q, i) => qbox.appendChild(wizardQuestionItem(q, i)));
  qbox.appendChild(wizardFreeItem());
  updateWizardHint();
}

function updateWizardHint() {
  const err = wizardValidate(wizardDomain, wizardPick);
  document.getElementById("wizard-hint").textContent = err ? ""
    : (wizardPick === FREE_PICK ? "已选：自由发挥（不选题）" : "已选：1 道题");
}

function wizardQuestionItem(q, i) {
  const item = document.createElement("div");
  item.className = "pool-item";
  const label = document.createElement("label");
  label.className = "pool-row";
  const rb = document.createElement("input");
  rb.type = "radio";
  rb.name = "wizard-pick";
  rb.value = q.id;
  rb.checked = wizardPick === q.id;
  rb.addEventListener("change", () => { wizardPick = q.id; updateWizardHint(); });
  label.appendChild(rb);
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
  return item;
}

function wizardFreeItem() {
  const item = document.createElement("div");
  item.className = "pool-item";
  item.style.borderColor = "#b8860b";
  const label = document.createElement("label");
  label.className = "pool-row";
  const rb = document.createElement("input");
  rb.type = "radio";
  rb.name = "wizard-pick";
  rb.value = FREE_PICK;
  rb.checked = wizardPick === FREE_PICK;
  rb.addEventListener("change", () => { wizardPick = FREE_PICK; updateWizardHint(); });
  label.appendChild(rb);
  const badge = document.createElement("span");
  badge.className = "pool-badge t3";
  badge.textContent = "自由发挥";
  label.appendChild(badge);
  const title = document.createElement("span");
  title.className = "qtitle";
  title.textContent = "自由发挥（不选题，在这个领域里自定题目创作）";
  label.appendChild(title);
  item.appendChild(label);
  const tip = document.createElement("div");
  tip.className = "muted";
  tip.style.marginTop = "6px";
  tip.textContent = "适合想现场自由创作、或在已有项目上做增量的同学；产出通过会话轨迹评估。";
  item.appendChild(tip);
  return item;
}

async function wizardChooseDomain(k) {
  showErr("");
  if ((state.submissions || {})[k]) { showErr("该领域已提交，无需重复交卷。"); return; }
  wizardDomain = k;
  wizardPick = "";
  renderWizard();
  if (!wizardCache[k]) {
    try {
      const questions = await api("/api/exam_questions");
      wizardCache[k] = wizardQuestionsForDomain(questions, k);
    } catch (e) {
      showErr(e.message);
      wizardDomain = "";
    }
    renderWizard();
  }
}

function wizardBack() {
  wizardDomain = "";
  wizardPick = "";
  renderWizard();
}

function wizardConfirm() {
  const err = wizardValidate(wizardDomain, wizardPick);
  if (err) { showErr(err); return; }
  if (wizardPick === FREE_PICK) enroll("track3", null, wizardDomain);
  else enroll("track3", [wizardPick]);
}

async function maybeSyncMySessions(force) {
  // 完成状态以云端 my_sessions 为准：wizard（logged_in）节流对账；
  // 完成页（uploaded/finished）在进入页面时强制对账——云端作废后本地降级回可重考。
  // 云端不可达时本地状态兜底（synced=false，不动 stage）。
  if (!state.auth_token) return;
  const doneStage = state.stage === "uploaded" || state.stage === "finished";
  const wizardStage = state.exam_mode === "free_creation" && state.stage === "logged_in";
  if (!doneStage && !wizardStage) return;
  const now = Date.now();
  if (!force && now - lastMySessionsSync < 15000) return;
  lastMySessionsSync = now;
  try {
    const data = await api("/api/my_sessions");
    if (data && data.synced) { state = data; render(); }
  } catch (e) {}
}

function renderDoneSummary() {
  const box = document.getElementById("done-summary");
  if (!box) return;
  box.textContent = "";
  const subs = state.submissions || {};
  if (state.exam_mode === "free_creation") {
    const keys = state.domain_keys || [];
    if (!keys.length) return;
    const h = document.createElement("h2");
    h.textContent = "各领域提交汇总";
    box.appendChild(h);
    keys.forEach((k, i) => {
      const label = (state.domains || [])[i] || DOMAIN_LABELS[k] || k;
      const info = subs[k];
      const item = document.createElement("div");
      item.className = "pool-item";
      const row = document.createElement("div");
      row.className = "pool-row";
      const title = document.createElement("span");
      title.className = "qtitle";
      title.textContent = label;
      row.appendChild(title);
      const badge = document.createElement("span");
      badge.className = "pool-badge " + (info ? "t2" : "t3");
      badge.textContent = info ? "已提交 ✓" : "待作答";
      row.appendChild(badge);
      item.appendChild(row);
      if (info && info.repo_url) {
        const repo = document.createElement("div");
        repo.className = "muted";
        repo.textContent = "仓库：" + info.repo_url;
        item.appendChild(repo);
      }
      if (info && info.pending_base) {
        const dl = document.createElement("button");
        dl.className = "secondary";
        dl.textContent = "下载清洗结果留档";
        dl.onclick = () => downloadCleaned(info.pending_base);
        item.appendChild(dl);
      }
      box.appendChild(item);
    });
    return;
  }
  const tlabel = SESSION_TRACK_LABELS[state.track || ""] || state.track_name || "";
  if (tlabel) {
    const tl = document.createElement("div");
    tl.className = "muted";
    tl.textContent = "作答方式：" + tlabel;
    box.appendChild(tl);
  }
  if (state.repo_url) {
    const repo = document.createElement("div");
    repo.className = "muted";
    repo.textContent = "仓库：" + state.repo_url;
    box.appendChild(repo);
  }
  if (state.pending_base) {
    const dl = document.createElement("button");
    dl.className = "secondary";
    dl.textContent = "下载清洗结果留档";
    dl.onclick = () => downloadCleaned(state.pending_base);
    box.appendChild(dl);
  }
}

function downloadCleaned(base) {
  const b = base || lastPreviewBase || state.pending_base || "";
  if (!b) { showErr("暂无可下载的清洗结果。"); return; }
  window.location.href = "/api/download_cleaned?base=" + encodeURIComponent(b);
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
  try { state = await api("/api/state"); render(); maybeSyncMySessions(true); }
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
    preferredView = ""; poolOpen = false;  // 重登录回到 exam_mode 推荐落页
    render();
  } catch (e) { showErr(e.message); }
}

async function enroll(track, questionIds, domain) {
  showErr("");
  const body = {};
  if (track) body.track = track;  // 题库自选不带 track（云端会话落 "selected"）
  if (questionIds && questionIds.length) body.question_ids = questionIds;
  if (domain) body.domain = domain;
  try { state = await api("/api/enroll", body); repoDirty = false; render(); }
  catch (e) { showErr(e.message); refreshSilently(); }
}

function renderModeChoice() {
  // 作答方式二选一入口卡；poolOpen 时保持题库块展开（5s 轮询不收回去）
  document.getElementById("mode-cards").classList.toggle("hidden", poolOpen);
  document.getElementById("selfselect-block").classList.toggle("hidden", !poolOpen);
}

function enterPoolMode() {
  showErr("");
  poolOpen = true;
  renderModeChoice();
  loadQuestionPool();
}

function backToModeChoice() {
  showErr("");
  poolOpen = false;
  renderModeChoice();
}

function enterProjectMode() {
  showErr("");
  preferredView = "project";
  render();
}

function backToQuestionMode() {
  showErr("");
  // free_creation 的「做题」是分领域向导，其余回作答方式选择页
  preferredView = state.exam_mode === "free_creation" ? "wizard" : "track";
  render();
}

function checkedPoolBoxes() {
  return [...document.querySelectorAll("#question-pool input[type=checkbox]:checked")];
}

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

function onPoolChange(ev) {
  // 防御：其他领域的题只允许浏览（正常 UI 里 checkbox 已 disabled）
  const ownKeys = state.domain_keys || [];
  if (ownKeys.indexOf(ev.target.dataset.domain || "") === -1) {
    ev.target.checked = false;
    alert("只能选问卷里报过的领域");
    return;
  }
  const checked = checkedPoolBoxes();
  if (checked.length > POOL_MAX) {
    ev.target.checked = false;
    alert("最多自选 " + POOL_MAX + " 道题");
    return;
  }
  updatePoolCount();
}

function renderQuestionPool(questions) {
  const box = document.getElementById("question-pool");
  box.classList.remove("hidden");
  box.textContent = "";
  const split = poolSplitByDomains(questions, state.domain_keys || []);
  const h1 = document.createElement("div");
  h1.className = "pool-track-head";
  h1.textContent = "你的领域（可作答）";
  box.appendChild(h1);
  if (!split.own.length) {
    const none = document.createElement("div");
    none.className = "muted";
    none.textContent = "题库中暂时没有属于你报名领域的题目，可改交自有项目。";
    box.appendChild(none);
  }
  appendPoolDomainGroups(box, split.own, true);
  const h2 = document.createElement("div");
  h2.className = "pool-track-head";
  h2.textContent = "其他领域（仅浏览，不可作答）";
  box.appendChild(h2);
  if (!split.others.length) {
    const none2 = document.createElement("div");
    none2.className = "muted";
    none2.textContent = "没有其他领域的题目。";
    box.appendChild(none2);
  }
  appendPoolDomainGroups(box, split.others, false);
  const bar = document.createElement("div");
  bar.style.marginTop = "8px";
  bar.innerHTML = '<button onclick="enrollSelfSelected()">确认自选开考</button> ' +
    '<span id="pool-count" class="muted"></span>';
  box.appendChild(bar);
  updatePoolCount();
}

function appendPoolDomainGroups(box, questions, answerable) {
  // 组内再按领域分小节（不按 track 分组/过滤，题面保留领域标签即可）
  const groups = {};
  questions.forEach(q => {
    (groups[q.domain || "未分类"] = groups[q.domain || "未分类"] || []).push(q);
  });
  for (const [domain, qList] of Object.entries(groups)) {
    const dh = document.createElement("div");
    dh.className = "pool-domain-head";
    dh.textContent = "领域：" + (DOMAIN_LABELS[domain] || domain)
      + "（共 " + qList.length + " 题）";
    box.appendChild(dh);
    qList.forEach((q, i) => box.appendChild(poolQuestionItem(q, i, answerable)));
  }
}

function poolQuestionItem(q, i, answerable) {
  const item = document.createElement("div");
  item.className = "pool-item";
  const label = document.createElement("label");
  label.className = "pool-row";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.value = q.id;
  cb.dataset.domain = q.domain || "";
  if (answerable) {
    cb.addEventListener("change", onPoolChange);
  } else {
    cb.disabled = true;
    item.style.opacity = "0.55";
    label.style.cursor = "default";
  }
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
  return item;
}

async function loadQuestionPool() {
  showErr("");
  try {
    const questions = await api("/api/exam_questions");
    renderQuestionPool(questions);
  } catch (e) { showErr(e.message); }
}

async function enrollSelfSelected() {
  const boxes = checkedPoolBoxes();
  const ids = boxes.map(c => c.value);
  const err = poolValidateStandard(ids);
  if (err) { showErr(err); return; }
  // 本地预校验：只能选问卷里报过的领域（服务端同样会 403，双保险）
  const ownKeys = state.domain_keys || [];
  if (boxes.some(c => ownKeys.indexOf(c.dataset.domain || "") === -1)) {
    showErr("只能选问卷里报过的领域");
    return;
  }
  enroll(null, ids);
}

async function openAccountPage() {
  showErr("");
  showNote("");
  try {
    const data = await api("/api/open_account_page", {});
    showNote(data.hint || "已在浏览器打开招新平台账号页。");
  } catch (e) { showErr(e.message); }
}

function renderProjectAttachment() {
  const info = document.getElementById("project-attachment-info");
  if (!info) return;
  info.textContent = state.attachment_name
    ? "已附材料：" + state.attachment_name + "（重新选择可替换）" : "";
}

async function uploadProjectAttachment() {
  const input = document.getElementById("project-attachment-file");
  const f = input && input.files[0];
  if (!f) return;
  showErr("");
  try {
    const res = await fetch("/api/attachment",
      {method: "PUT", headers: {"X-Filename": f.name}, body: f});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("上传失败 HTTP " + res.status));
    state.attachment_name = data.attachment_name;
    input.value = "";
    renderProjectAttachment();
  } catch (e) { showErr(e.message); }
}

async function submitOwnProject() {
  showErr("");
  const repoUrl = document.getElementById("project-repo-url").value.trim();
  if (!repoUrl) { showErr("请填写 GitHub 仓库链接"); return; }
  if (!/^https?:\\/\\//.test(repoUrl)) {
    showErr("仓库链接格式不正确：须以 http:// 或 https:// 开头（如 https://github.com/用户名/仓库名）");
    return;
  }
  if (!confirm("确认提交自有项目？提交成功即完成交卷。")) return;
  const btn = document.getElementById("project-submit-btn");
  btn.disabled = true;
  showNote("正在提交自有项目，请稍候…");
  try {
    state = await api("/api/submit_project", {repo_url: repoUrl});
    showNote("");
    render();
  } catch (e) {
    showNote("");
    showErr(e.message);
  } finally { btn.disabled = false; }
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

function showFinishBusy(text) {
  document.getElementById("finish-busy-text").textContent = text || "正在处理，请勿关闭页面…";
  unhide("finish-busy");
}

async function cleanSlotFile(i) {
  const input = document.getElementById("slot-file-" + i);
  const f = input && input.files[0];
  if (!f) { showErr("请先选择第 " + (i + 1) + " 题导出的会话文件（.json 或 .zip）"); return; }
  await cleanExportBytes(f, f.name, i);
}

async function cleanSlotPasted(i) {
  const ta = document.getElementById("slot-paste-" + i);
  const text = (ta && ta.value || "").trim();
  if (!text) { showErr("请先把第 " + (i + 1) + " 题的会话 JSON 内容粘贴到文本框里"); return; }
  await cleanExportBytes(new Blob([text], {type: "application/json"}), "粘贴的会话.json", i);
}

async function cleanExportBytes(blob, name, slot) {
  showErr("");
  hide("finish-result-fail"); hide("finish-result-success");
  showFinishBusy("正在本地清洗第 " + (slot + 1) + " 题的会话，请勿关闭页面…");
  try {
    const res = await fetch("/api/clean_export", {
      method: "POST",
      headers: {"X-Source-Name": encodeURIComponent(name || "会话导出"),
                "X-Slot": String(slot)},
      body: blob,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ("请求失败 HTTP " + res.status));
    try { state = await api("/api/state"); } catch (e) {}  // 拉回 slots 登记
    renderSlots(true);
    startPreview(data);
  } catch (e) { showFinishFail(e.message); }
  finally { hide("finish-busy"); }
}

function startPreview(data) {
  lastPreviewBase = data.preview_base || "";
  hide("finish-result-fail");
  unhide("finish-step-preview");
  const note = document.getElementById("preview-note");
  const st = data.stats || {};
  const parts = [];
  if (st.user !== undefined) parts.push("用户输入 " + st.user + " 条");
  if (st.assistant !== undefined) parts.push("助手回复 " + st.assistant + " 段");
  if (st.tool !== undefined) parts.push("工具调用 " + st.tool + " 次");
  note.textContent = "清洗完成（" + (parts.join("、") || "已生成清洗结果")
    + "）。下面是清洗后的可读预览，请过一遍，确认没有不该上传的内容：";
  const frame = document.getElementById("preview-frame");
  const text = document.getElementById("preview-text");
  const url = "/api/preview?base=" + encodeURIComponent(data.preview_base) + "&t=" + Date.now();
  if (data.preview_kind === "html") {
    text.classList.add("hidden");
    frame.classList.remove("hidden");
    frame.src = url;
  } else {
    frame.classList.add("hidden");
    frame.src = "about:blank";
    text.classList.remove("hidden");
    text.textContent = "（预览加载中…）";
    fetch(url).then(r => r.text()).then(t => { text.textContent = t; })
      .catch(() => { text.textContent = "（预览加载失败，请重新清洗一次）"; });
  }
  document.getElementById("finish-step-preview").scrollIntoView({behavior: "smooth"});
}

function previewSlot(i) {
  const info = (state.slots || {})[String(i)];
  if (!info || !info.base) return;
  startPreview({preview_base: info.base, preview_kind: "html", stats: info.stats || {}});
}

async function uploadCleaned() {
  const repoUrl = document.getElementById("repo-url").value.trim();
  if (repoUrl && !/^https?:\\/\\//.test(repoUrl)) {
    showErr("仓库链接格式不正确：须以 http:// 或 https:// 开头（如 https://github.com/用户名/仓库名）");
    return;
  }
  const total = slotList().length;
  const cleaned = Object.keys(state.slots || {})
    .filter(k => state.slots[k] && state.slots[k].base).length;
  if (!cleaned) { showErr("请先在第 2 小步至少清洗一道题的会话"); return; }
  if (cleaned < total) {
    if (!confirm("还有 " + (total - cleaned) + " 道题没有清洗会话，"
      + "未清洗的题将视为未作答。\\n确定上传吗？")) return;
  } else if (!confirm("确认上传？只会上传你预览过的清洗结果。")) return;
  showErr("");
  hide("finish-result-fail"); hide("finish-result-success");
  showFinishBusy("正在上传，请勿关闭页面…");
  document.getElementById("upload-btn").disabled = true;
  try {
    state = await api("/api/upload_cleaned", {repo_url: repoUrl});
    if (state.stage === "uploaded" || state.uploaded) {
      // free_creation 全部领域交完：进汇总完成页；其它模式沿用页内成功块
      if (state.exam_mode === "free_creation") render();
      else showFinishSuccess(state);
    } else if (state.stage === "logged_in") {
      // free_creation 分领域交卷：本领域成功，回领域列表继续下一个
      lastPreviewBase = "";
      hide("finish-step-preview");
      showNote("本领域交卷成功！请继续选择下一个「待作答」领域。");
      render();
    }
    else showFinishFail(state.upload_error || "");
  } catch (e) { showFinishFail(e.message); }
  finally {
    document.getElementById("upload-btn").disabled = false;
    hide("finish-busy");
  }
}

async function scanSlotSessions(i) {
  showErr("");
  const box = document.getElementById("slot-scan-" + i);
  if (!box) return;
  box.classList.remove("hidden");
  box.textContent = "正在扫描本题工作区里的会话…";
  try {
    const data = await api("/api/scan_sessions", {});
    const all = data.candidates || [];
    const item = slotList()[i];
    const dir = item ? slotDir(item.q) : "";
    const norm = s => (s || "").replace(/\\\\/g, "/").replace(/\\/+$/, "").toLowerCase();
    let filtered = all;
    if (dir) {
      const want = "/" + norm(dir);
      filtered = all.filter(c => {
        const d = norm(c.directory);
        return d.endsWith(want) || d.indexOf(want + "/") !== -1;
      });
    }
    // 目录筛不出来时兜底列出全部候选（比如作答工具开在根目录）
    renderSlotScanResult(i, filtered.length ? filtered : all,
                         filtered.length ? "" : "（没能按本题目录筛出会话，列出考试工作区全部候选）");
  } catch (e) { box.textContent = e.message; }
}

function renderSlotScanResult(i, cands, noteText) {
  const box = document.getElementById("slot-scan-" + i);
  box.textContent = "";
  if (!cands.length) { box.textContent = "没有在考试工作区找到候选会话。"; return; }
  const tip = document.createElement("div");
  tip.className = "muted";
  tip.textContent = (noteText ? noteText + "；" : "")
    + "找到下面这些「目录属于考试工作区」的会话，点一个清洗为本题答案：";
  box.appendChild(tip);
  cands.forEach(c => {
    const item = document.createElement("div");
    item.className = "pool-item";
    const row = document.createElement("div");
    row.className = "pool-row";
    const b = document.createElement("button");
    b.className = "secondary";
    b.textContent = "选这个";
    b.onclick = () => cleanSlotScanned(i, c);
    row.appendChild(b);
    const when = document.createElement("span");
    when.textContent = c.updated ? new Date(c.updated).toLocaleString() : "时间未知";
    row.appendChild(when);
    const src = document.createElement("span");
    src.className = "muted";
    src.textContent = "（来源：" + (c.source || "") + "）";
    row.appendChild(src);
    item.appendChild(row);
    const dir = document.createElement("div");
    dir.className = "ws-line";
    const code = document.createElement("span");
    code.className = "ws-abs";
    code.textContent = c.directory || "";
    dir.appendChild(code);
    item.appendChild(dir);
    box.appendChild(item);
  });
}

async function cleanSlotScanned(i, cand) {
  showErr("");
  showFinishBusy("正在本地清洗第 " + (i + 1) + " 题的会话，请勿关闭页面…");
  try {
    const data = await api("/api/clean_scanned", {
      kind: cand.kind,
      session_id: cand.session_id,
      file: cand.file || "",
      slot: i,
    });
    try { state = await api("/api/state"); } catch (e) {}  // 拉回 slots 登记
    renderSlots(true);
    startPreview(data);
  } catch (e) { showFinishFail(e.message); }
  finally { hide("finish-busy"); }
}

async function retryUpload() {
  showErr("");
  try {
    state = await api("/api/retry_upload", {});
    render();
  } catch (e) { showErr(e.message); }
}

document.getElementById("attachment-file").addEventListener("change", uploadAttachment);
document.getElementById("project-attachment-file")
  .addEventListener("change", uploadProjectAttachment);
document.getElementById("repo-url").addEventListener("input", () => { repoDirty = true; });
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
            data = load_state()
            data["p2p"] = p2p_status()  # 上传走的链路：connected|fallback|off
            return self._send_json(data)
        if self.path == "/api/exam_questions":
            return self._exam_questions()
        if self.path.startswith("/api/preview"):
            return self._preview()
        if self.path == "/api/my_sessions":
            return self._my_sessions()
        if self.path.startswith("/api/download_cleaned"):
            return self._download_cleaned()
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
            if self.path == "/api/scan_sessions":
                return self._scan_sessions()
            if self.path == "/api/clean_export":
                return self._clean_export()
            if self.path == "/api/clean_scanned":
                return self._clean_scanned()
            if self.path == "/api/upload_cleaned":
                return self._upload_cleaned()
            if self.path == "/api/retry_upload":
                return self._retry_upload()
            if self.path == "/api/open_agent_workspace":
                return self._open_agent_workspace()
            if self.path == "/api/open_account_page":
                return self._open_account_page()
            if self.path == "/api/submit_project":
                return self._submit_project()
            if self.path == "/api/kit_update/download":
                return self._kit_update_download()
            if self.path == "/api/kit_update/open_folder":
                return self._kit_update_open_folder()
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
        state["account_url"] = f"{cfg['cloud_base']}/account"  # own_project 考生提交仓库链接的网页
        state["agent_label"] = AGENT_LABELS.get(str(cfg.get("agent", "kimi")),
                                                str(cfg.get("agent", "kimi")))
        if exam_mode == "free_creation":
            # 按领域循环交卷：登录即以云端 my_sessions 对账各领域完成状态
            # （服务端为准；云端不可达则静默降级，向导页随后自行再对账）
            try:
                _, my = cloud_request(
                    cfg, "GET",
                    f"/api/campaigns/{cfg['campaign_id']}/exam/my_sessions",
                    bearer=token)
                subs = submissions_from_sessions(my)
                old = load_state()
                if old.get("student_id") == student_id:
                    # 同机重登录：保留各领域本地 pending_base（完成页下载留档按钮用）
                    subs = merge_submissions(old.get("submissions") or {}, subs)
                state["submissions"] = subs
                keys = [k for k in domain_keys if k]
                if keys and all(k in subs for k in keys):
                    # 全部领域都已交卷（换机/重开后重新登录）：直接进完成页
                    state["stage"] = "uploaded"
                    state["uploaded"] = True
            except CloudError:
                pass
        # 登录整体重建 state：OTA 版本提示不属于考试会话，跨登录保留
        prev = load_state()
        if prev.get("kit_update"):
            state["kit_update"] = prev["kit_update"]
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
        track = str(body.get("track") or "").strip()
        if track and track not in {t["key"] for t in TRACKS}:
            return self._send_json({"error": "无效轨迹"}, status=400)
        question_ids = body.get("question_ids")
        domain = str(body.get("domain", "")).strip()
        if not track and question_ids is None and not domain:
            return self._send_json(
                {"error": "请先从题库勾选 1~3 道题（或改交自有项目）"}, status=400)
        payload = {}
        if track:
            # track 非空的旧逻辑保留（随机抽题 / track3 向导）
            payload["track"] = track
        self_selected = question_ids is not None or bool(domain)
        if question_ids is not None:
            if (not isinstance(question_ids, list)
                    or not all(isinstance(q, str) and q.strip()
                               for q in question_ids)
                    or not 1 <= len(question_ids) <= QUESTION_IDS_MAX):
                return self._send_json(
                    {"error": f"自选题须勾选 1~{QUESTION_IDS_MAX} 道题"}, status=400)
            payload["question_ids"] = question_ids
        cfg = self.cfg
        if question_ids is not None and not track:
            # 题库自选（不带 track，会话落成 "selected"）：本地预校验所选题目
            # 领域 ⊆ 报名领域（云端另有 403 双保险）；题库拉不下来则跳过本地
            # 校验，以云端为准
            try:
                _, pool = cloud_request(
                    cfg, "GET",
                    f"/api/campaigns/{cfg['campaign_id']}/exam/questions",
                    bearer=auth_token)
            except CloudError as exc:
                if exc.status == 401:
                    return self._session_expired()
                pool = None
            if isinstance(pool, list):
                by_id = {str(q.get("id") or ""): str(q.get("domain") or "")
                         for q in pool if isinstance(q, dict)}
                own = set(state.get("domain_keys") or [])
                for qid in question_ids:
                    if qid not in by_id:
                        return self._send_json(
                            {"error": "题库里没有找到所选题目，"
                                      "请重新打开题库后再勾选"}, status=400)
                    if by_id[qid] not in own:
                        return self._send_json(
                            {"error": "只能选问卷里报过的领域"}, status=400)
        if domain:
            # M12 自由发挥：track3 + domain（不选题，在该领域自定题目创作）
            if track != "track3":
                return self._send_json(
                    {"error": "只有自由创作轨迹支持按领域开考"}, status=400)
            if domain not in (state.get("domain_keys") or []):
                return self._send_json(
                    {"error": "只能在自己报名的领域内开考"}, status=400)
            if domain in (state.get("submissions") or {}):
                # 本地对账已知该领域交过卷：直接拦下，不必打扰云端
                return self._send_json(
                    {"error": "该领域已提交，无需重复交卷；"
                              "如需重交请联系组织者作废后重试"}, status=400)
            payload["domain"] = domain
        path = f"/api/campaigns/{cfg['campaign_id']}/exam/enroll"
        resumed = False
        try:
            _, served = cloud_request(cfg, "POST", path, payload,
                                      bearer=auth_token)
        except CloudError as exc:
            if exc.status == 401:
                return self._session_expired()
            if exc.status == 409 and isinstance(exc.detail, dict) \
                    and exc.detail.get("code") == "already_enrolled":
                served = exc.detail  # 续考：detail 里已带完整会话信息
                resumed = True
            else:
                raise
        track_name = next(
            (t["name"] for t in TRACKS if t["key"] == served["track"]),
            SESSION_TRACK_LABELS.get(str(served["track"]), str(served["track"])))
        if not resumed:
            # 新一轮作答（free_creation 换领域/首次开考）：清掉上一轮的交卷残留，
            # 尤其是 repo_url——每次交卷单独问一次，不带入下一领域
            for k in ("pending_base", "uploaded", "upload_pending", "upload_error",
                      "agent_session_id", "export_method", "finished_at",
                      "attachment_name", "repo_url", "slots"):
                state.pop(k, None)
        state.update({
            "stage": "enrolled",
            "session_id": served["session_id"],
            "session_token": served["session_token"],
            "machine_id": served.get("machine_id", ""),
            "track": served["track"],
            "track_name": track_name,
            "questions": served.get("questions", []),
            "domain_tags": served.get("domain_tags", []),
            "track3_guide_text": served.get("track3_guide_text", ""),
            "rerolls_left": served.get("rerolls_left", 0),
            "opened_at": (state.get("opened_at") if resumed else None) or now_iso(),
        })
        if self_selected or served.get("track") == "selected":
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
        state.pop("slots", None)  # 换题后题目变了，已清洗的槽位作废（磁盘文件保留无害）
        save_state(state)
        self._send_json(state)

    # ---- M12 交卷：手动导出 → 清洗 → 预览 → 确认上传（隐私优先，不默认扫库） ----

    def _exam_state_or_400(self) -> dict | None:
        """交卷相关端点的公共前置：必须在答题/待上传状态且已领云端会话。"""
        state = load_state()
        if state.get("stage") not in ("enrolled", "finished"):
            self._send_json({"error": "当前不在答题状态"}, status=400)
            return None
        if not state.get("session_id"):
            self._send_json({"error": "请先登录并领取题目"}, status=400)
            return None
        return state

    def _cleaner_or_500(self):
        cleaner_mod = load_cleaner()
        if cleaner_mod is None:
            self._send_json(
                {"error": "清洗模块 cleaner.py 缺失，交卷包不完整，请联系组织者处理。"},
                status=500)
            return None
        return cleaner_mod

    def _scan_sessions(self):
        """可选的自动发现：考生点击「在考试工作区帮我找」后才执行，
        只列出目录属于考试工作区的会话，绝不默认扫描全盘。"""
        state = load_state()
        if state.get("stage") not in ("enrolled", "finished"):
            return self._send_json({"error": "当前不在答题状态"}, status=400)
        opened_ms = iso_to_ms(state["opened_at"]) if state.get("opened_at") else 0
        try:
            cands = scan_workdir_candidates(self.cfg, opened_ms)
        except Exception as exc:
            print(f"[exam_server] 扫描考试工作区会话失败: {exc!r}")
            return self._send_json(
                {"error": "扫描会话时出了点问题，请重试一次；仍失败请改用上面的手动导出。"},
                status=500)
        if not cands:
            return self._send_json(
                {"error": "没找到你的作答记录——考试工作区里没有开考之后的会话。"
                          "请确认你是在「考试工作目录」的对应领域文件夹里作答；"
                          "如果你已经在 AI 工具里导出了会话文件，"
                          "直接用上面第 2 小步选择文件即可。"},
                status=404)
        self._send_json({"candidates": cands})

    def _clean_export(self):
        """第 2 小步：清洗考生手动提供的会话导出文件（原始字节 + X-Source-Name 文件名，
        X-Slot 题号槽位，缺省为第 1 题）。

        只清洗并生成预览，不上传；上传由 /api/upload_cleaned 确认触发。"""
        state = self._exam_state_or_400()
        if state is None:
            return
        slot_raw = str(self.headers.get("X-Slot", "") or "").strip()
        try:
            slot = int(slot_raw) if slot_raw else 0
        except ValueError:
            slot = -1
        if not 0 <= slot < _slot_count(state):
            return self._send_json(
                {"error": "题号超出范围，请刷新页面后重试"}, status=400)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._send_json({"error": "文件内容为空"}, status=400)
        if length > EXPORT_MAX_BYTES:
            return self._send_json(
                {"error": "文件太大（超过 2GB），请联系组织者处理"}, status=413)
        name = urllib.parse.unquote(
            self.headers.get("X-Source-Name", "").strip())
        name = Path(name).name or "会话导出"
        cleaner_mod = self._cleaner_or_500()
        if cleaner_mod is None:
            return
        cfg = self.cfg
        base = _slot_base(state, slot)
        PENDING_DIR.mkdir(exist_ok=True)
        # 流式落盘：大导出文件不整读进内存
        head = self.rfile.read(4)
        if len(head) < 4:
            return self._send_json({"error": "文件内容为空"}, status=400)
        suffix = ".zip" if head[:2] == b"PK" else ".json"
        raw_path = PENDING_DIR / f"{base}{suffix}"
        remaining = length - 4
        with raw_path.open("wb") as f:
            f.write(head)
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining > 0:
            raw_path.unlink(missing_ok=True)
            return self._send_json(
                {"error": "文件传输不完整，请重试"}, status=400)
        agent_session_id = _agent_session_id_from_export(raw_path)
        return self._clean_only(
            state, cfg, base, raw_path, f"手动导出的会话文件（{name}）",
            agent_session_id, cleaner_mod, slot=slot)

    def _clean_scanned(self):
        """清洗「在本题工作区帮我找」列表里选中的候选会话（body.slot 为题号槽位）。"""
        state = self._exam_state_or_400()
        if state is None:
            return
        body = self._read_body()
        kind = str(body.get("kind", ""))
        try:
            slot = int(body.get("slot", 0))
        except (TypeError, ValueError):
            slot = -1
        if not 0 <= slot < _slot_count(state):
            return self._send_json(
                {"error": "题号超出范围，请刷新页面后重试"}, status=400)
        cleaner_mod = self._cleaner_or_500()
        if cleaner_mod is None:
            return
        cfg = self.cfg
        base = _slot_base(state, slot)
        PENDING_DIR.mkdir(exist_ok=True)
        opened_ms = iso_to_ms(state["opened_at"]) if state.get("opened_at") else 0
        # 重新扫描并匹配候选：只用服务端扫描结果里的字段，客户端伪造不了任意路径
        match = None
        for c in scan_workdir_candidates(cfg, opened_ms):
            if c["kind"] != kind or c["session_id"] != str(body.get("session_id", "")):
                continue
            if kind == "opencode_json" \
                    and c.get("file", "") != str(body.get("file", "")):
                continue
            match = c
            break
        if match is None:
            return self._send_json(
                {"error": "这个会话找不到了，请重新点「在考试工作区帮我找」刷新列表"},
                status=404)
        if kind == "kimi":
            raw_path = PENDING_DIR / f"{base}.zip"
            method = export_session(
                cfg, {"sessionId": match["session_id"],
                      "sessionDir": match.get("session_dir", "")}, raw_path)
        elif kind == "opencode_db":
            raw_path = PENDING_DIR / f"{base}.json"
            doc, method = _opencode_document(
                cfg, opencode_home(cfg), match["session_id"], raw_path)
            if doc is None:
                return self._send_json(
                    {"error": "导出这个会话的内容失败了，请改用第 2 小步手动导出"},
                    status=500)
        elif kind == "opencode_json":
            raw_path = PENDING_DIR / f"{base}.json"
            file = match.get("file", "")
            if file:
                try:
                    shutil.copyfile(file, raw_path)
                except OSError as exc:
                    return self._send_json(
                        {"error": f"读取会话文件失败：{exc}"}, status=500)
            else:
                doc = None  # storage 布局：重新拼装同构 doc
                for s in iter_opencode_sessions(opencode_home(cfg)):
                    if s["session_id"] == match["session_id"]:
                        doc = s["doc"]
                        break
                if doc is None:
                    return self._send_json(
                        {"error": "导出这个会话的内容失败了，请改用第 2 小步手动导出"},
                        status=500)
                raw_path.write_text(
                    json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            method = "opencode 会话文件"
        else:
            return self._send_json({"error": "未知的会话来源"}, status=400)
        return self._clean_only(
            state, cfg, base, raw_path, method, match["session_id"], cleaner_mod,
            slot=slot)

    def _clean_only(self, state: dict, cfg: dict, base: str, raw_path: Path,
                    method: str, agent_session_id: str, cleaner_mod,
                    slot: int | None = None):
        """清洗 → 落 pending_upload → 返回预览信息（不上传）。

        slot 不为 None 时把本次清洗登记进 state["slots"][slot]（逐题提交槽）；
        多题考试的槽位 meta 记录 slot_index，交卷与自动补传都逐题各自上传。"""
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
            "repo_url": str(state.get("repo_url", "")),
            "attachment_name": state.get("attachment_name", ""),
            "uploaded": False,
            "stats": result.get("stats", {}),
            "finished_at": now_iso(),
        }
        if slot is not None and _slot_count(state) > 1:
            meta["slot_index"] = slot  # 多题考试：逐题各自上传，云端按题各存一份
        save_meta(base, meta)
        if slot is not None:
            slots = dict(state.get("slots") or {})
            slots[str(slot)] = {
                "base": base,
                "agent_session_id": agent_session_id,
                "method": method,
                "stats": meta["stats"],
                "cleaned_at": meta["finished_at"],
            }
            state["slots"] = slots
        state.update({
            "agent_session_id": agent_session_id,
            "export_method": method,
            "finished_at": meta["finished_at"],
            "pending_base": base,
            "uploaded": False,
        })
        state.pop("upload_error", None)
        save_state(state)
        cleaned_html = result.get("cleaned_html")
        has_html = bool(cleaned_html and Path(cleaned_html).exists()) \
            or (out_dir / "cleaned.html").exists()
        self._send_json({
            "ok": True,
            "preview_base": base,
            "preview_kind": "html" if has_html else "md",
            "stats": result.get("stats", {}),
            "agent_session_id": agent_session_id,
        })

    def _preview(self):
        """预览：优先 cleaned.html（内嵌渲染），退化为 cleaned.md 纯文本。
        允许预览本次作答任一题槽位的清洗产物。"""
        state = load_state()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        base = str((query.get("base") or [""])[0])
        # 只允许预览当前考生本次清洗的产物（pending_base 或已登记的题槽位 base）
        allowed = {str(state.get("pending_base", ""))} | _registered_slot_bases(state)
        if not base or base not in allowed:
            return self._send_json(
                {"error": "没有可预览的清洗结果，请先在第 2 小步清洗"}, status=404)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", base):
            return self._send_json({"error": "预览参数不正确"}, status=400)
        out_dir = PENDING_DIR / base
        for name, ctype in (("cleaned.html", "text/html; charset=utf-8"),
                            ("cleaned.md", "text/plain; charset=utf-8")):
            path = out_dir / name
            if path.exists():
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self._send_json({"error": "清洗结果不存在，请重新清洗"}, status=404)

    def _my_sessions(self):
        """对账：拉云端本人会话列表，重建 state.submissions（free_creation 分领域交卷）。

        服务端是完成状态的唯一事实源；云端不可达时回 200 + synced=False +
        本地 submissions 兜底，页面照常可用。401 走 _session_expired 清登录态。
        作废降级：本地显示已交卷（uploaded）但云端已无任何 uploaded 会话
        （或 free_creation 有领域被作废）时，清场回 logged_in 允许重考；
        上传未完成的 finished 态不受影响（本地仍指望重传）。"""
        state = load_state()
        auth_token = state.get("auth_token")
        if not auth_token:
            return self._send_json({"error": "请先登录"}, status=400)
        cfg = self.cfg
        try:
            _, sessions = cloud_request(
                cfg, "GET",
                f"/api/campaigns/{cfg['campaign_id']}/exam/my_sessions",
                bearer=auth_token)
        except CloudError as exc:
            if exc.status == 401:
                return self._session_expired()
            return self._send_json({
                **state, "ok": False, "synced": False,
                "submissions": state.get("submissions") or {},
                "my_sessions": [], "error": str(exc)})
        subs = merge_submissions(state.get("submissions") or {},
                                 submissions_from_sessions(sessions))
        state["submissions"] = subs
        if state.get("exam_mode") == "free_creation":
            keys = [k for k in (state.get("domain_keys") or []) if k]
            if state.get("stage") == "logged_in":
                if keys and all(k in subs for k in keys):
                    # 全部领域都已交卷（如换机后同步）：收敛到完成页
                    state["stage"] = "uploaded"
                    state["uploaded"] = True
            elif state.get("stage") == "uploaded" and state.get("uploaded"):
                # 完成页对账：有领域在云端被作废 → 回领域列表重考该领域
                if keys and not all(k in subs for k in keys):
                    _clear_exam_fields(state)
                    state["uploaded"] = False
                    state["stage"] = "logged_in"
        elif state.get("uploaded") or state.get("stage") == "uploaded":
            still_uploaded = any(
                isinstance(s, dict) and s.get("status") == "uploaded"
                for s in sessions or [])
            if not still_uploaded:
                _clear_exam_fields(state)
                state["uploaded"] = False
                state["stage"] = "logged_in"
        save_state(state)
        self._send_json({**state, "ok": True, "synced": True,
                         "my_sessions": sessions})

    def _download_cleaned(self):
        """留档下载：pending_upload 里本次/历史某领域的清洗结果（cleaned.html 优先，
        无则 cleaned.md；kind=html|md 可显式指定），以附件形式下载，
        文件名 {学号}-{领域中文名}.{后缀}（中文名走 RFC 5987 filename*）。"""
        state = load_state()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        base = str((query.get("base") or [""])[0])
        kind = str((query.get("kind") or [""])[0]).lower()
        if not base or not re.fullmatch(r"[A-Za-z0-9_-]+", base):
            return self._send_json({"error": "下载参数不正确"}, status=400)
        student_id = str(state.get("student_id") or "")
        # 只允许下载本人本次/历史各领域的清洗产物（base 必须在状态里有登记）
        allowed = set()
        if state.get("pending_base"):
            allowed.add(str(state["pending_base"]))
        allowed |= _registered_slot_bases(state)
        for info in (state.get("submissions") or {}).values():
            if isinstance(info, dict) and info.get("pending_base"):
                allowed.add(str(info["pending_base"]))
        if not student_id or base not in allowed \
                or not base.startswith(student_id + "_"):
            return self._send_json({"error": "没有可下载的清洗结果"}, status=404)
        candidates = [("cleaned.html", "html", "text/html; charset=utf-8"),
                      ("cleaned.md", "md", "text/markdown; charset=utf-8")]
        if kind in ("html", "md"):
            candidates = [c for c in candidates if c[1] == kind]
        out_dir = PENDING_DIR / base
        for name, ext, ctype in candidates:
            path = out_dir / name
            if path.exists():
                body = path.read_bytes()
                label = _sanitize_filename(
                    f"{student_id}-{_download_domain_label(state, base)}")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition",
                                 _content_disposition(f"{label}.{ext}"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self._send_json({"error": "清洗结果文件不存在"}, status=404)

    def _upload_cleaned(self):
        """第 3 小步：确认上传。单题考试沿用单件管线；多题考试逐题各自上传
        （_upload_slot_entries，不合并），失败的题记下来由补传 worker/手动重试。"""
        state = self._exam_state_or_400()
        if state is None:
            return
        repo_url = str(self._read_body().get("repo_url", "")).strip()
        if repo_url and not repo_url.startswith(("http://", "https://")):
            return self._send_json(
                {"error": "仓库链接格式不正确：须以 http:// 或 https:// 开头，"
                          "请修改后重新上传（本次未上传，清洗结果未受影响）"},
                status=400)
        if _slot_count(state) > 1:
            return self._upload_all_slots(state, repo_url)
        base = state.get("pending_base", "")
        meta = load_meta(base) if base else None
        if meta is not None and meta.get("uploaded"):  # 幂等：已经传过
            _advance_after_upload(state, meta)
            save_state(state)
            return self._send_json(state)
        if meta is None:
            return self._send_json(
                {"error": "请先在第 2 小步清洗你的会话文件"}, status=400)
        meta["repo_url"] = repo_url
        meta["attachment_name"] = state.get("attachment_name", "")
        save_meta(base, meta)
        state["repo_url"] = repo_url
        try:
            with _upload_lock:
                upload_session(self.cfg, meta)
        except CloudError as exc:
            state.update({"stage": "finished", "uploaded": False,
                          "upload_pending": True,
                          "upload_error": _friendly_upload_error(exc)})
            save_state(state)  # 断网兜底：本地留存，稍后可重试/自动补传
            return self._send_json(state)
        meta["uploaded"] = True
        save_meta(base, meta)
        _advance_after_upload(state, meta)
        save_state(state)
        self._send_json(state)

    def _upload_all_slots(self, state: dict, repo_url: str):
        """多题考试交卷/重试共用：逐题上传，全部成功才进完成页。"""
        n = _slot_count(state)
        slots = state.get("slots") or {}
        cleaned = [k for k in range(n)
                   if isinstance(slots.get(str(k)), dict)
                   and slots[str(k)].get("base")]
        if not cleaned:
            return self._send_json(
                {"error": "还没有清洗任何一道题的会话——"
                          "请先在第 2 小步逐题清洗"}, status=400)
        with _upload_lock:
            failures, last_meta = _upload_slot_entries(state, self.cfg, repo_url)
        if repo_url:
            state["repo_url"] = repo_url
        if failures:
            state.update({"stage": "finished", "uploaded": False,
                          "upload_pending": True,
                          "upload_error": f"第 {'、'.join(failures)} 题上传失败，"
                                          "系统会自动重试，也可点「重试上传」"})
            save_state(state)
            return self._send_json(state)
        _advance_after_upload(state, last_meta or {})
        save_state(state)
        self._send_json(state)

    def _submit_project(self):
        """交自有项目：repo_url（必填，http(s):// 开头）+ 可选 zip 附件（先前经
        /api/attachment 暂存到 pending_upload）组 multipart 直交云端
        POST /api/campaigns/{cid}/applications/me/project（Bearer 考生凭证）。
        任何考生可交；云端 409 = 已有非 voided 会话。成功即完成交卷：
        本地落 track=project / stage=uploaded，页面进完成页。"""
        state = load_state()
        auth_token = state.get("auth_token")
        if not auth_token:
            return self._send_json({"error": "请先登录"}, status=400)
        repo_url = str(self._read_body().get("repo_url", "")).strip()
        if not repo_url:
            return self._send_json({"error": "请填写 GitHub 仓库链接"}, status=400)
        if not repo_url.startswith(("http://", "https://")):
            return self._send_json(
                {"error": "仓库链接格式不正确：须以 http:// 或 https:// 开头"
                          "（如 https://github.com/用户名/仓库名）"}, status=400)
        fields = {"repo_url": repo_url}
        files: list[tuple[str, Path]] = []
        att = self._attachment_path()
        if state.get("attachment_name") and att is not None and att.exists():
            files.append(("attachment", att))
        cfg = self.cfg
        try:
            cloud_request(
                cfg, "POST",
                f"/api/campaigns/{cfg['campaign_id']}/applications/me/project",
                multipart_fields=fields, multipart_files=files,
                bearer=auth_token)
        except CloudError as exc:
            if exc.status == 401:
                return self._session_expired()
            if exc.status == 409:
                return self._send_json(
                    {"error": "已有交卷记录：云端已有你未作废的考试会话，无需重复提交；"
                              "如需重交请联系组织者作废后重试"}, status=409)
            raise
        for k in ("upload_pending", "upload_error", "uploaded"):
            state.pop(k, None)
        state.update({
            "stage": "uploaded",
            "uploaded": True,
            "track": "project",
            "track_name": SESSION_TRACK_LABELS["project"],
            "repo_url": repo_url,
            "finished_at": now_iso(),
        })
        save_state(state)
        self._send_json(state)

    def _open_account_page(self):
        """own_project 考生：在浏览器里打开招新平台账号页（提交仓库链接的入口）。"""
        state = load_state()
        if not state.get("student_id"):
            return self._send_json({"error": "请先登录"}, status=400)
        url = str(state.get("account_url") or f"{self.cfg['cloud_base']}/account")
        if open_browser(url):
            return self._send_json(
                {"ok": True,
                 "hint": "已在浏览器打开招新平台账号页，请登录后提交你的 GitHub 仓库链接。"})
        return self._send_json(
            {"error": f"没能自动打开浏览器，请手动访问：{url}"}, status=500)

    def _kit_update_download(self):
        """「下载新版」：按 state.kit_update 的信息把新安装包下到「下载」文件夹
        （curl 传输 + sha256 校验）。失败走白话错误，绝不影响考试进程。"""
        state = load_state()
        info = state.get("kit_update") or {}
        if not info.get("version"):
            return self._send_json(
                {"error": "当前没有可下载的新版本信息，请刷新页面后再试"},
                status=400)
        try:
            dest = download_kit_installer(self.cfg, info)
        except CloudError as exc:
            return self._send_json(
                {"error": _friendly_download_error(exc)},
                status=exc.status or 502)
        except Exception as exc:  # 兜底：本地落盘失败等
            _log(f"新版安装包下载失败: {exc!r}")
            return self._send_json(
                {"error": _friendly_download_error(CloudError(str(exc)))},
                status=502)
        state["kit_update"] = {**info, "downloaded_path": str(dest)}
        save_state(state)
        self._send_json({
            "ok": True,
            "kit_update": state["kit_update"],
            "hint": f"已下载到「下载」文件夹：{dest}",
        })

    def _kit_update_open_folder(self):
        """「打开所在文件夹」：文件管理器定位到已下载的新安装包。"""
        state = load_state()
        path = str((state.get("kit_update") or {}).get("downloaded_path") or "")
        if not path:
            return self._send_json(
                {"error": "还没有下载完成的安装包，请先点「下载新版」"}, status=400)
        p = Path(path)
        if _open_in_folder(p):
            return self._send_json({"ok": True})
        return self._send_json(
            {"error": f"没能自动打开文件夹，请手动打开：{p.parent}"}, status=500)

    def _retry_upload(self):
        state = load_state()
        if _slot_count(state) > 1:
            return self._upload_all_slots(state, str(state.get("repo_url") or ""))
        base = state.get("pending_base", "")
        if not base:
            return self._send_json({"error": "当前没有可重试的交卷"}, status=400)
        meta = load_meta(base)
        if meta is None:
            return self._send_json({"error": "未找到交卷记录，请联系组织者处理。"}, status=404)
        if meta.get("uploaded"):
            _advance_after_upload(state, meta)
            save_state(state)
            return self._send_json(state)
        try:
            with _upload_lock:
                upload_session(self.cfg, meta)
        except CloudError as exc:
            state["uploaded"] = False
            state["upload_pending"] = True
            state["upload_error"] = _friendly_upload_error(exc)
            save_state(state)
            return self._send_json(state)
        meta["uploaded"] = True
        save_meta(base, meta)
        _advance_after_upload(state, meta)
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
        _log(f"机试包版本: {KIT_VERSION}")
        _log(f"云端: {cfg['cloud_base']}  届次: {cfg['campaign_id']}")
        _log(f"作答工具: {AGENT_LABELS[cfg['agent']]}  Web 指令: {agent_web_cmd(cfg)}")
        _log(f"考试工作目录: {agent_workdir(cfg)}")
        _log(">>> 如果浏览器没有自动打开，请手动在 Edge 地址栏输入：%s" % url)
        start_upload_worker()  # 断网/失败交卷的周期自动补传（每 60s 轮询）
        start_kit_update_worker()  # OTA 新版本检测（后台 daemon，失败静默）
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

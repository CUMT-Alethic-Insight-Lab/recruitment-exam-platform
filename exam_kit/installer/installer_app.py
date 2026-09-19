# -*- coding: utf-8 -*-
"""招新考试系统一键安装器（M11）

tkinter 向导 GUI，纯 Python 标准库 + Win32（ctypes / winreg），
所有子进程只允许 cmd.exe 或直接运行 exe，不依赖 PowerShell。

用法：
    python installer_app.py                # 打开图形向导
    python installer_app.py --debug        # 图形向导，日志里附带子进程原始输出
    python installer_app.py --stage <目录> [--agent kimi|opencode]
                                           # 干跑：只做解压布局 + config + 快捷方式，
                                           # 跳过组件安装与 PATH/注册表，退出码 0 表示成功

组件的文件名 / SHA256 / 静默参数 / 目标目录 / PATH 追加项全部来自同目录
components_spec.json（数值由 R1 线 COMPONENTS.md 回填），本文件不硬编码版本号。
"""

import argparse
import hashlib
import json
import re
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path

IS_FROZEN = getattr(sys, "frozen", False)
APP_BASE = Path(sys._MEIPASS) if IS_FROZEN else Path(__file__).resolve().parent

INSTALL_DIR_NAME = "招新考试系统-删除文件夹即完成卸载"
WORKDIR_NAME = "考试工作目录"
WORKDIR_NOTE_NAME = "请在此文件夹里使用 AI 工具作答.txt"
LOG_NAME = "安装日志.txt"
SHORTCUT_NAME = "启动考试系统.lnk"
SHORTCUT_DESC = "Alethic Insight 社团招新机试平台"

REQUIRED_KIT_FILES = ["exam_server.py", "cleaner.py", "config.json",
                      "启动考试系统.bat", "启动机试服务.bat"]
OPTIONAL_KIT_FILES = ["README.md"]

AGENT_LABELS = {"kimi": "Kimi Code", "opencode": "opencode"}

CREATE_NO_WINDOW = 0x08000000


# --------------------------------------------------------------------------
# 错误与日志
# --------------------------------------------------------------------------

class InstallError(Exception):
    """带白话说明的安装失败。friendly 给用户看，detail 进日志。"""

    def __init__(self, friendly, detail=""):
        super().__init__(friendly)
        self.friendly = friendly
        self.detail = detail or friendly


class Logger:
    """双栏日志：白话（给新生看）+ 技术（给排障的社团成员看）。"""

    def __init__(self, log_path=None, echo=True):
        self.log_path = Path(log_path) if log_path else None
        self.echo = echo
        self._lock = threading.Lock()
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._open("a") as f:
                f.write("\n===== 安装器启动 %s =====\n" % _now())

    def _open(self, mode):
        return open(self.log_path, mode, encoding="utf-8", errors="replace")

    def _write(self, line):
        with self._lock:
            if self.log_path:
                try:
                    with self._open("a") as f:
                        f.write(line + "\n")
                except OSError:
                    pass
            if self.echo:
                try:
                    print(line)
                except OSError:
                    pass

    def say(self, plain, tech=""):
        ts = _now()
        self._write("[%s] 白话: %s" % (ts, plain))
        if tech:
            for ln in str(tech).splitlines() or [""]:
                self._write("[%s] 技术: %s" % (ts, ln))

    def tech(self, text):
        ts = _now()
        for ln in str(text).splitlines() or [""]:
            self._write("[%s] 技术: %s" % (ts, ln))


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# spec 与 payload 资源定位
# --------------------------------------------------------------------------

def load_spec(logger=None):
    path = APP_BASE / "components_spec.json"
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    if logger:
        for note in spec.get("placeholders", {}).values():
            if "TODO" in str(note):
                logger.tech("components_spec.json TODO: %s" % note)
    return spec


class Payload:
    """打包资源定位：frozen 时全部在 sys._MEIPASS/payload/；
    开发模式 kit 文件直接读 exam_kit/ 根目录、组件读 payload_cache/。"""

    def __init__(self, kit_dir, comp_dir, dev=False):
        self.kit_dir = Path(kit_dir)
        self.comp_dir = Path(comp_dir)
        self.dev = dev

    def kit_file(self, name):
        return self.kit_dir / name

    def comp_file(self, comp_key, spec):
        return self.comp_dir / spec["components"][comp_key]["file"]


def locate_payload():
    candidate = APP_BASE / "payload"
    if (candidate / "kit").is_dir() or candidate.is_dir():
        return Payload(candidate / "kit", candidate, dev=False)
    # 开发模式回退：本脚本在 exam_kit/installer/ 下时
    kit_dir = APP_BASE.parent
    comp_dir = APP_BASE / "payload_cache"
    if (kit_dir / "exam_server.py").is_file() and comp_dir.is_dir():
        return Payload(kit_dir, comp_dir, dev=True)
    raise InstallError(
        "没有找到安装包内置文件（payload）。",
        "locate_payload: _MEIPASS=%s, %s 与 %s 都不完整" % (APP_BASE, candidate, comp_dir))


# --------------------------------------------------------------------------
# Windows 系统辅助（ctypes / winreg，全程不用 PowerShell）
# --------------------------------------------------------------------------

def _make_guid(s):
    u = uuid.UUID(s)
    return _GUID(u.fields[0], u.fields[1], u.fields[2],
                 (_c_ubyte * 8)(*u.bytes[8:]))


import ctypes
from ctypes import POINTER, WINFUNCTYPE, byref, c_long, c_ulong, c_void_p, c_wchar_p
from ctypes import c_ubyte as _c_ubyte


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", _c_ubyte * 8)]


def get_desktop_path():
    """SHGetKnownFolderPath(FOLDERID_Desktop)——兼容 OneDrive 重定向。"""
    FOLDERID_Desktop = _make_guid("B4BFCC3A-DB2C-424C-B029-7FE99A87C641")
    p = c_void_p()
    hr = ctypes.windll.shell32.SHGetKnownFolderPath(
        byref(FOLDERID_Desktop), 0, None, byref(p))
    if hr != 0 or not p.value:
        raise InstallError(
            "读取「桌面」文件夹位置失败（错误码 %s）。" % hr,
            "SHGetKnownFolderPath(FOLDERID_Desktop) -> 0x%08X" % hr)
    try:
        return ctypes.wstring_at(p)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(p)


def run_cmd(cmd, timeout=60, extra_path=None, cwd=None):
    """用 cmd.exe /d /c 执行命令并捕获输出。只允许 cmd.exe。"""
    return _run(["cmd.exe", "/d", "/c", cmd], timeout, extra_path, cwd)


def run_argv(argv, timeout=60, extra_path=None, cwd=None):
    """按参数数组直接执行程序（不经 shell、不手工拼引号）。
    .cmd/.bat 必须经 cmd.exe 才能运行，此时每个参数由 subprocess 单独正确转义。"""
    argv = [str(a) for a in argv]
    if argv and argv[0].lower().endswith((".cmd", ".bat")):
        argv = ["cmd.exe", "/d", "/c"] + argv
    return _run(argv, timeout, extra_path, cwd)


def _run(argv, timeout, extra_path, cwd):
    env = None
    if extra_path:
        env = os.environ.copy()
        env["PATH"] = os.pathsep.join(str(p) for p in extra_path) + os.pathsep + env.get("PATH", "")
    try:
        proc = subprocess.run(
            argv,
            capture_output=True, text=True, errors="replace",
            timeout=timeout, env=env, cwd=cwd,
            creationflags=CREATE_NO_WINDOW)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "（命令超时 %d 秒）" % timeout
    except OSError as e:
        return -1, "无法启动 %s: %s" % (argv[0], e)


def _hr(hr, what, friendly):
    if hr != 0:
        raise InstallError(friendly,
                           "%s -> HRESULT 0x%08X" % (what, hr & 0xFFFFFFFF))


def create_shortcut(lnk_path, target, workdir="", description=""):
    """ctypes 直接调 IShellLinkW + IPersistFile 生成 .lnk（不依赖 PowerShell/wscript）。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    CLSID_ShellLink = _make_guid("00021401-0000-0000-C000-000000000046")
    IID_IShellLinkW = _make_guid("000214F9-0000-0000-C000-000000000046")
    IID_IPersistFile = _make_guid("0000010B-0000-0000-C000-000000000046")
    CLSCTX_INPROC_SERVER = 1

    friendly = "创建桌面快捷方式失败。考试系统本身已装好，"
    friendly += "你可以手动进入桌面的「%s」文件夹，双击「%s」启动。" % (INSTALL_DIR_NAME, Path(target).name)
    psl = c_void_p()
    _hr(ole32.CoCreateInstance(byref(CLSID_ShellLink), None, CLSCTX_INPROC_SERVER,
                               byref(IID_IShellLinkW), byref(psl)),
        "CoCreateInstance(ShellLink)", friendly)
    vtbl2 = None
    try:
        vtbl = ctypes.cast(ctypes.cast(psl, POINTER(c_void_p)).contents.value,
                           POINTER(c_void_p * 21)).contents
        # IShellLinkW: 0..2=IUnknown, 7=SetDescription, 9=SetWorkingDirectory, 20=SetPath
        if description:
            WINFUNCTYPE(c_long, c_void_p, c_wchar_p)(vtbl[7])(psl, description)
        if workdir:
            _hr(WINFUNCTYPE(c_long, c_void_p, c_wchar_p)(vtbl[9])(psl, workdir),
                "IShellLink::SetWorkingDirectory", friendly)
        _hr(WINFUNCTYPE(c_long, c_void_p, c_wchar_p)(vtbl[20])(psl, str(target)),
            "IShellLink::SetPath", friendly)
        ppf = c_void_p()
        _hr(WINFUNCTYPE(c_long, c_void_p, POINTER(_GUID), POINTER(c_void_p))(vtbl[0])(
                psl, byref(IID_IPersistFile), byref(ppf)),
            "IShellLink::QueryInterface(IPersistFile)", friendly)
        try:
            vtbl2 = ctypes.cast(ctypes.cast(ppf, POINTER(c_void_p)).contents.value,
                                POINTER(c_void_p * 9)).contents
            # IPersistFile: 0..2=IUnknown, 6=Save(pszFileName, fRemember)
            _hr(WINFUNCTYPE(c_long, c_void_p, c_wchar_p, c_long)(vtbl2[6])(ppf, str(lnk_path), 1),
                "IPersistFile::Save", friendly)
        finally:
            if vtbl2 is not None:
                WINFUNCTYPE(c_long, c_void_p)(vtbl2[2])(ppf)
    finally:
        WINFUNCTYPE(c_long, c_void_p)(vtbl[2])(psl)
    return lnk_path


def read_user_path_raw():
    """读 HKCU\\Environment 的 Path（保持 REG_EXPAND_SZ 未展开原样）。缺失返回 ('', None)。"""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as k:
        try:
            val, typ = winreg.QueryValueEx(k, "Path")
            return str(val), typ
        except FileNotFoundError:
            return "", None


def write_user_path(new_value):
    """写回 HKCU\\Environment 的 Path（一律 REG_EXPAND_SZ）并广播 WM_SETTINGCHANGE。"""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as k:
        winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, new_value)
    broadcast_env_change()


def broadcast_env_change():
    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    res = c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
        SMTO_ABORTIFHUNG, 3000, byref(res))


def append_user_path(dirs_to_add, logger):
    """向用户 PATH 去重追加目录；绝不覆盖已有条目，返回实际追加列表。"""
    raw, _typ = read_user_path_raw()
    entries = [e for e in raw.split(";")]
    seen = {os.path.expandvars(e).rstrip("\\").lower()
            for e in entries if e.strip()}
    added = []
    for d in dirs_to_add:
        key = os.path.expandvars(str(d)).rstrip("\\").lower()
        if key in seen:
            continue
        entries.append(os.path.expandvars(str(d)))
        seen.add(key)
        added.append(str(d))
    if not added:
        logger.say("PATH 已经包含需要的目录，不需要修改。", "append_user_path: nothing to add")
        return []
    new_value = ";".join(entries)
    write_user_path(new_value)
    logger.say("已把新装软件的目录加进你的用户 PATH（没有动任何原有内容）。",
               "HKCU\\Environment Path += %s (REG_EXPAND_SZ, WM_SETTINGCHANGE broadcast)" % added)
    return added


def sha256_of(path, logger=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 组件探测
# --------------------------------------------------------------------------

def check_argv(check, target):
    """spec 的 post_install_check → argv 数组（可直接交给 run_argv）。
    优先用数组格式（路径可含空格）；旧字符串格式按空白切分（路径不得含空格）。"""
    if isinstance(check, list):
        return [os.path.expandvars(str(a).replace("{target}", target)) for a in check]
    return os.path.expandvars(str(check).replace("{target}", target)).split()


def probe_component(key, spec, extra_path=None):
    """返回 (installed: bool, detail: str)。"""
    comp = spec["components"].get(key, {})
    for rel in comp.get("verify_files", []):
        p = os.path.expandvars(rel)
        if os.path.isfile(p):
            return True, p
    regex = comp.get("version_regex") or ""
    for cmd in comp.get("verify_cmds", []):
        rc, out = run_cmd(cmd, timeout=45, extra_path=extra_path)
        if rc == 0:
            if regex and not re.search(regex, out):
                continue
            return True, out.strip().splitlines()[0] if out.strip() else cmd
    return False, ""


def probe_cmd():
    rc, _ = run_cmd("ver", timeout=15)
    return rc == 0


def probe_powershell():
    rc, _ = run_cmd("powershell -NoProfile -Command \"$PSVersionTable.PSVersion.Major\"",
                    timeout=45)
    return rc == 0


# ---------- 浏览器检测与默认关联尽力修复 ----------

BROWSER_ORDER = [
    ("Chrome", [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"]),
    ("Firefox", [r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
                 r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"]),
    ("Edge", [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
              r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"]),
    ("Brave", [r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"]),
    ("Opera", [r"%ProgramFiles%\Opera\launcher.exe"]),
]


def detect_browsers() -> list[tuple[str, str]]:
    """按优先级返回已装浏览器 [(名称, exe路径)]：chrome>firefox>edge>其他。"""
    out, seen = [], set()
    for name, tmpls in BROWSER_ORDER:
        for tmpl in tmpls:
            p = os.path.expandvars(tmpl)
            if p and p not in seen and os.path.isfile(p):
                seen.add(p)
                out.append((name, p))
                break
    return out


def _reg_query(path: str) -> bool:
    rc, _ = run_cmd('reg query "%s"' % path, timeout=15)
    return rc == 0


def repair_http_default() -> tuple[str, str]:
    """尽力让 http/https 默认走一个已装浏览器（chrome>firefox>edge>其他）。
    仅当当前没有 http 默认应用时尝试；Win10+ 的 UserChoice 带保护哈希，写入可能被系统忽略——
    因此这是尽力而为，真正的兜底是考试系统直接拉起浏览器 exe。返回 (说明, 日志详情)。"""
    browsers = detect_browsers()
    if not browsers:
        return "未检测到常见浏览器，将使用系统默认关联。", "no browser exe found"
    name, exe = browsers[0]
    progid = {"Chrome": "ChromeHTML", "Edge": "MSEdgeHTM",
              "Firefox": "FirefoxURL", "Brave": "BraveHTML",
              "Opera": "OperaStable"}.get(name)
    if not progid:
        return "检测到 %s，将直接用它打开考试页。" % name, "no progid for %s" % name
    if _reg_query(r"HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"):
        return "当前已有 http 默认应用，保持不变；考试页将由 %s 直接打开。" % name, \
               "http UserChoice exists, skip"
    import winreg  # 局部导入：非 Windows 环境兜底
    try:
        for proto in ("http", "https"):
            key = (r"HKCU\Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
                   r"\{proto}\UserChoice")
            winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                             key.replace("HKCU\\", "", 1))
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                key.replace("HKCU\\", "", 1), 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "ProgId", 0, winreg.REG_SZ, progid)
                winreg.SetValueEx(k, "Hash", 0, winreg.REG_SZ, "")
        return ("已尝试把 http/https 默认设为 %s（若系统未立即生效，考试页仍会自动用 %s 打开）。"
                % (name, name),
                "set UserChoice http/https -= %s (best-effort; Win10+ may ignore)" % progid)
    except OSError as exc:
        return "检测到 %s，考试页将由它直接打开。" % name, \
               "repair UserChoice failed: %s" % exc


def tools_base(spec):
    return os.path.expandvars(spec.get("tools_base", r"%LOCALAPPDATA%\Programs\AlethicTools"))


# --------------------------------------------------------------------------
# 安装引擎
# --------------------------------------------------------------------------

class Step:
    def __init__(self, key, title, plain, func):
        self.key = key
        self.title = title
        self.plain = plain          # 白话文案（进度页显示）
        self.func = func


class Engine:
    """安装执行器：与 GUI 解耦，GUI 或 --stage CLI 都驱动它。"""

    def __init__(self, spec, payload, target_dir, agent, logger,
                 stage_only=False, debug=False, desktop_override=None):
        self.spec = spec
        self.payload = payload
        self.target = Path(target_dir)
        self.agent = agent if agent in ("kimi", "opencode") else "kimi"
        self.logger = logger
        self.stage_only = stage_only
        self.debug = debug
        self.desktop = desktop_override
        self.temp_dirs = []
        self.steps = self._build_steps()

    # ---- 步骤编排 ----

    def _build_steps(self):
        steps = [
            Step("kit", "解压考试系统文件",
                 "正在把考试系统放进桌面文件夹…", self.step_extract_kit),
            Step("config", "写入考试系统配置",
                 "正在写入考试系统的启动配置…", self.step_write_config),
        ]
        if not self.stage_only:
            steps += [
                Step("pwsh", "安装 PowerShell 7",
                     "正在安装 PowerShell 7（更现代的命令行外壳，送你的；"
                     "Windows 11 的终端可以用它当默认后端）…",
                     lambda: self.step_component("pwsh")),
                Step("python", "安装 Python 3.12",
                     "正在安装 Python（编程语言，考试系统靠它运行，装完可以长期用）"
                     "——这一步最慢，约 1~3 分钟…",
                     lambda: self.step_component("python")),
                Step("node", "安装 Node.js",
                     "正在安装 Node.js（AI 工具的运行底座）…",
                     lambda: self.step_component("node")),
                Step("git", "安装 Git",
                     "正在安装 Git（程序员常用的代码管理工具，送你的）…",
                     lambda: self.step_component("git")),
                Step("agent", "安装 AI 作答工具",
                     "正在安装你选择的 AI 作答工具（%s）…"
                     % AGENT_LABELS[self.agent], self.step_agent_tool),
                Step("path", "更新 PATH 环境变量",
                     "正在把新装的软件登记到系统里（以后在命令行直接可用）…",
                     self.step_path),
                Step("terminal", "注册 Windows 11 终端入口",
                     "正在把 PowerShell 7 登记进 Windows 11 终端（如果没有终端会自动跳过）…",
                     self.step_terminal_fragment),
                Step("browser", "确认默认浏览器",
                     "正在确认默认浏览器（保证考试页面能自动打开）…",
                     self.step_browser),
            ]
        steps += [
            Step("shortcut", "创建桌面快捷方式",
                 "正在创建桌面的「启动考试系统」快捷方式…", self.step_shortcut),
            Step("finish", "收尾清理",
                 "正在清理临时文件并做最后检查…", self.step_finish),
        ]
        return steps

    # ---- 工具函数 ----

    def _component(self, key):
        return self.spec["components"][key]

    def _verify_payload_file(self, key):
        comp = self._component(key)
        src = self.payload.comp_file(key, self.spec)
        if not src.is_file():
            raise InstallError(
                "安装包内置文件不完整（缺 %s）。请重新下载安装程序。" % comp["file"],
                "missing payload file: %s" % src)
        self.logger.say("正在校验 %s 的完整性…" % comp.get("label", key),
                        "sha256 %s" % src)
        actual = sha256_of(src)
        expect = (comp.get("sha256") or "").lower()
        if expect and actual != expect:
            raise InstallError(
                "%s 的安装包校验失败，可能是下载损坏。请重新下载安装程序。" % comp.get("label", key),
                "sha256 mismatch for %s: expect=%s actual=%s" % (src, expect, actual))
        return src

    def _extract_zip(self, src, target, strip_root=False):
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            names = zf.namelist()
            root = None
            if strip_root:
                firsts = {n.replace("\\", "/").split("/", 1)[0] for n in names}
                root = firsts.pop() if len(firsts) == 1 else None
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.endswith("/"):
                    continue
                if root and name.startswith(root + "/"):
                    name = name[len(root) + 1:]
                out = target / name
                if os.path.isabs(name) or ".." in Path(name).parts:
                    raise InstallError(
                        "压缩包内出现异常路径，已中止（安全保护）。",
                        "unsafe zip entry: %s in %s" % (info.filename, src))
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fsrc, open(out, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
        self.logger.tech("extract %s -> %s (strip_root=%s)" % (src, target, strip_root))

    def _wait_for_file(self, path, seconds=60):
        path = os.path.expandvars(str(path))
        deadline = time.time() + seconds
        while time.time() < deadline:
            if os.path.isfile(path):
                return True
            time.sleep(1)
        return os.path.isfile(path)

    def _alethic_dir(self, sub):
        return str(Path(tools_base(self.spec)) / sub)

    # ---- 各步骤 ----

    def step_extract_kit(self):
        self.logger.say("开始安装。目标文件夹：%s" % self.target,
                        "target=%s stage_only=%s debug=%s" % (self.target, self.stage_only, self.debug))
        self.target.mkdir(parents=True, exist_ok=True)
        for name in REQUIRED_KIT_FILES + OPTIONAL_KIT_FILES:
            src = self.payload.kit_file(name)
            if not src.is_file():
                if name in REQUIRED_KIT_FILES:
                    raise InstallError(
                        "安装包内置文件不完整（缺 %s）。请重新下载安装程序。" % name,
                        "missing kit file: %s" % src)
                self.logger.say("说明文件 %s 缺失，跳过（不影响使用）。" % name,
                                "optional kit file missing: %s" % src)
                continue
            shutil.copy2(src, self.target / name)
            self.logger.tech("copy %s -> %s" % (src, self.target / name))
        for name in REQUIRED_KIT_FILES:
            if not (self.target / name).is_file():
                raise InstallError(
                    "考试系统文件没有复制成功（缺 %s）。" % name,
                    "post-copy check failed: %s" % (self.target / name))
        # 考试工作目录（考生 AI 作答工作区）
        workdir = self.target / WORKDIR_NAME
        workdir.mkdir(parents=True, exist_ok=True)
        note = workdir / WORKDIR_NOTE_NAME
        note.write_text(
            "这里是你的「考试工作目录」。\n\n"
            "机试作答方式：\n"
            "1. 双击桌面的「启动考试系统」快捷方式，等待浏览器打开机试页面。\n"
            "2. 考试系统会自动在本文件夹里打开你的 AI 作答工具（网页界面）。\n"
            "3. 请在本文件夹中使用 AI 工具完成答题——考试系统会自动发现这里产生的作答记录，\n"
            "   交卷时自动清洗并上传。\n"
            "4. 请不要移动、重命名或删除本文件夹。\n\n"
            "如有问题请截图联系组织者。\n",
            encoding="utf-8")
        self.logger.say("考试系统文件已就位，并创建了「%s」作答工作文件夹。" % WORKDIR_NAME,
                        "workdir=%s note=%s" % (workdir, note))

    def step_write_config(self):
        src = self.payload.kit_file("config.json")
        with open(src, encoding="utf-8") as f:
            cfg = json.load(f)
        # 只改 agent 与 agent_workdir；web 指令/云端地址/端口等保持 kit 默认
        cfg["agent"] = self.agent
        cfg["agent_workdir"] = str(self.target / WORKDIR_NAME)
        dst = self.target / "config.json"
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        with open(dst, encoding="utf-8") as f:      # 写后回读校验
            back = json.load(f)
        if back.get("agent") != self.agent \
                or back.get("agent_workdir") != str(self.target / WORKDIR_NAME):
            raise InstallError("考试系统配置写入校验失败。",
                               "config.json write-back mismatch: %s" % back)
        self.logger.say("已写入考试系统配置（作答工具：%s）。" % AGENT_LABELS[self.agent],
                        "config.json: agent=%s agent_workdir=%s"
                        % (back.get("agent"), back.get("agent_workdir")))

    def step_component(self, key):
        comp = self._component(key)
        label = comp.get("label", key)
        installed, detail = probe_component(key, self.spec)
        if installed:
            healthy = True
            check = comp.get("post_install_check")
            if check:
                _argv = check_argv(check, os.path.expandvars(comp.get("target_dir", "")))
                _rc, _out = run_argv(_argv, timeout=180,
                                     extra_path=self._probe_extra_path())
                healthy = _rc == 0
            if healthy:
                self.logger.say("检测到 %s 已经装过，跳过安装。" % label,
                                "probe %s -> installed (%s)" % (key, detail))
                return
            self.logger.say("检测到 %s 之前装过，但启动自检没通过，重新安装一遍…"
                            % label,
                            "probe %s installed but post_install_check rc=%s; reinstall"
                            % (key, _rc))
        self.logger.say("开始安装 %s（全新电脑第一次装，请耐心等一会）…" % label,
                        "install %s" % key)
        src = self._verify_payload_file(key)
        kind = comp.get("kind")
        target = os.path.expandvars(comp.get("target_dir", ""))
        ok_codes = comp.get("ok_exit_codes", [0])
        if kind == "exe_installer":
            args = str(comp.get("silent_args", "")).split()
            self.logger.tech("run %s %s" % (src, " ".join(args)))
            try:
                proc = subprocess.run([str(src)] + args, capture_output=True,
                                      text=True, errors="replace", timeout=1800,
                                      creationflags=CREATE_NO_WINDOW)
                rc = proc.returncode
                if self.debug and (proc.stdout or proc.stderr):
                    self.logger.tech("stdout/stderr:\n%s%s"
                                     % (proc.stdout or "", proc.stderr or ""))
            except subprocess.TimeoutExpired:
                raise InstallError("%s 安装超时（30 分钟）。请重试这一步。" % label,
                                   "%s installer timeout" % key)
            except OSError as e:
                raise InstallError("无法启动 %s 的安装程序。" % label, repr(e))
            if rc not in ok_codes:
                raise InstallError(
                    "%s 安装没有成功（内部代码 %s）。可以点「重试」再试一次。" % (label, rc),
                    "%s installer exit code %s" % (key, rc))
        elif kind == "self_extractor":
            args = [str(a).replace("{target}", target) for a in comp.get("extract_args", ["-y"])]
            Path(target).mkdir(parents=True, exist_ok=True)
            self.logger.tech("run %s %s" % (src, " ".join(args)))
            try:
                proc = subprocess.run([str(src)] + args, capture_output=True,
                                      text=True, errors="replace", timeout=1800,
                                      creationflags=CREATE_NO_WINDOW)
                rc = proc.returncode
                if self.debug and (proc.stdout or proc.stderr):
                    self.logger.tech("stdout/stderr:\n%s%s"
                                     % (proc.stdout or "", proc.stderr or ""))
            except subprocess.TimeoutExpired:
                raise InstallError("%s 解压超时。请重试这一步。" % label, "%s extract timeout" % key)
            except OSError as e:
                raise InstallError("无法启动 %s 的解压程序。" % label, repr(e))
            if rc not in ok_codes:
                raise InstallError("%s 解压没有成功（内部代码 %s）。" % (label, rc),
                                   "%s sfx exit code %s" % (key, rc))
        elif kind in ("zip_flat", "zip_strip_root"):
            self._extract_zip(src, target, strip_root=(kind == "zip_strip_root"))
        else:
            raise InstallError("安装器不支持 %s 的安装方式（%s）。" % (label, kind),
                               "unknown component kind %r for %s" % (kind, key))
        # 验证
        verify = comp.get("verify_files", [""])
        if verify and not self._wait_for_file(verify[0], seconds=120):
            raise InstallError(
                "%s 装完了，但没有找到它该出现的文件（%s）。请重试这一步。"
                % (label, verify[0]),
                "verify failed after install: %s" % verify[0])
        # 真实运行自检（spec 的 post_install_check，如 kimi.cmd --version）。
        # 直接以参数数组运行（run_argv），不手工拼引号——嵌套引号曾导致路径尾部残留
        # 引号、自检必然 FILE_NOT_FOUND（沙箱实测 pwsh/opencode 4 连败即此因）。
        # 失败后自动重试数次，兜住杀软对新解压大 exe 的首次扫描延迟。
        check = comp.get("post_install_check")
        if check:
            argv = check_argv(check, target)
            attempts = 4
            for i in range(attempts):
                rc, out = run_argv(argv, timeout=180,
                                   extra_path=self._probe_extra_path())
                if rc == 0:
                    break
                self.logger.say(
                    "%s 启动自检第 %d 次没有跑起来，等 3 秒再试"
                    "（常见原因：杀毒软件在首次扫描新装的大文件）…"
                    % (label, i + 1),
                    "post_install_check attempt %d/%d argv=%s rc=%s out=%s"
                    % (i + 1, attempts, argv, rc, out[:2000]))
                if i < attempts - 1:
                    time.sleep(3)
            if rc != 0:
                raise InstallError(
                    "%s 装好了，但启动自检没有通过（点「重试」可再试一次；"
                    "多次失败请把安装文件夹里的「安装日志.txt」发给组织者）。" % label,
                    "post_install_check %r -> rc=%s out=%s" % (check, rc, out[:2000]))
            self.logger.say("%s 启动自检通过。" % label,
                            "post_install_check ok: %s" % out.strip().splitlines()[:1])
        cmd_ok, out = probe_component(key, self.spec,
                                      extra_path=self._probe_extra_path())
        self.logger.say("%s 安装完成。" % label,
                        "installed %s verify=%s probe=%s"
                        % (key, verify[0] if verify else "-", out or cmd_ok))
        if not cmd_ok:
            self.logger.say("提醒：%s 装好了，但命令行暂时还认不到它（重启电脑后会正常）。"
                            % label, "probe after install failed (PATH not refreshed in this session)")

    def _probe_extra_path(self):
        """本安装器管理的新目录（探测/自检时临时附加进 PATH，不改系统）。"""
        dirs = []
        for comp in self.spec["components"].values():
            for d in comp.get("path_append", []):
                p = os.path.expandvars(d)
                if os.path.isdir(p) and p not in dirs:
                    dirs.append(p)
        return dirs

    def step_agent_tool(self):
        key = self.spec.get("agent_component", {}).get(self.agent, self.agent)
        self.step_component(key)
        comp = self._component(key)
        if comp.get("needs_shim"):
            self._write_kimi_shim(key)

    def _write_kimi_shim(self, key):
        comp = self._component(key)
        shim = comp.get("shim") or {}
        target = os.path.expandvars(comp["target_dir"])
        entry_rel = shim.get("entry_rel", r"node_modules\@moonshot-ai\kimi-code\dist\main.mjs")
        entry = str(Path(target) / entry_rel)
        if not Path(entry).is_file():
            raise InstallError(
                "Kimi Code 的主程序文件缺失（%s）。安装包可能不完整。" % entry_rel,
                "kimi entry missing: %s" % entry)
        node_exe_tpl = shim.get("node_exe", "{tools}\\node\\node.exe")
        node_exe = node_exe_tpl.replace("{tools}", tools_base(self.spec)) \
                               .replace("{target}", target)
        if not Path(os.path.expandvars(node_exe)).is_file():
            fallback = shim.get("node_exe_fallback", "node.exe")
            self.logger.say("没有找到随装的 Node.js，Kimi Code 将改用系统里的 node。",
                            "kimi shim: %s missing, fallback to %s" % (node_exe, fallback))
            node_exe = fallback
        text = shim.get("template", '@echo off\r\n"{node_exe}" "{entry}" %*\r\n')
        text = text.replace("{node_exe}", os.path.expandvars(node_exe)).replace("{entry}", entry)
        shim_path = Path(target) / shim.get("file", "kimi.cmd")
        with open(shim_path, "w", encoding="ascii", errors="strict", newline="") as f:
            f.write(text)
        # 验证 shim 真能跑起来
        rc, out = run_cmd('"%s" --version' % shim_path, timeout=120)
        if rc != 0:
            raise InstallError(
                "Kimi Code 装好了，但启动自检没有通过（点「重试」可再试一次；"
                "多次失败请把安装文件夹里的「安装日志.txt」发给组织者）。",
                "kimi.cmd --version -> rc=%s out=%s" % (rc, out[:2000]))
        self.logger.say("Kimi Code 启动自检通过。",
                        "shim=%s version=%s" % (shim_path, out.strip().splitlines()[:1]))

    def step_path(self):
        dirs = []
        for key, comp in self.spec["components"].items():
            for d in comp.get("path_append", []):
                p = os.path.expandvars(d)
                if os.path.isdir(p) and p not in dirs:
                    dirs.append(p)
        if not dirs:
            self.logger.say("没有需要登记的新软件目录。")
            return
        append_user_path(dirs, self.logger)

    def step_terminal_fragment(self):
        """给 Windows 11 终端写一个 fragment 入口（profiles.entry）。

        只做「可用入口」，不改用户 Terminal 的 settings.json、不动默认配置文件。
        Win10 / 没装终端 / 目录不存在 / 无权限 都是常态：任何失败只记日志，
        绝不中断安装。
        """
        try:
            pwsh_exe = str(Path(self._alethic_dir("pwsh")) / "pwsh.exe")
            if not Path(os.path.expandvars(pwsh_exe)).is_file():
                self.logger.say("没找到 PowerShell 7，跳过终端入口注册（不影响使用）。",
                                "terminal fragment: %s missing, skipped" % pwsh_exe)
                return
            frag_dir = Path(os.path.expandvars(
                r"%LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments\AlethicInsight"))
            frag_dir.mkdir(parents=True, exist_ok=True)
            frag_path = frag_dir / "pwsh.json"
            fragment = {
                "profiles": [
                    {
                        "name": "PowerShell 7 (Alethic)",
                        "commandline": pwsh_exe,
                        "startingDirectory": "%USERPROFILE%",
                        "icon": pwsh_exe,
                        "hidden": False,
                        "elevate": False,
                    }
                ]
            }
            with open(frag_path, "w", encoding="utf-8") as f:
                json.dump(fragment, f, ensure_ascii=False, indent=2)
            with open(frag_path, encoding="utf-8") as f:   # 写后回读校验
                json.load(f)
            self.logger.say(
                "已在 Windows 终端里注册「PowerShell 7 (Alethic)」配置文件"
                "（若你的电脑没有终端，此步自动跳过）。",
                "terminal fragment written: %s" % frag_path)
        except Exception as e:                              # noqa: BLE001 故意吞掉
            self.logger.say(
                "注册 Windows 终端入口没有成功，已跳过（完全不影响考试与使用）。",
                "terminal fragment skipped: %r" % e)

    def step_browser(self):
        """确认默认浏览器：尽力修复 http/https 默认关联 + 报告将使用哪个浏览器。
        任何失败只记日志，绝不中断安装（真正的兜底是 exam_server 直接拉起浏览器 exe）。"""
        try:
            browsers = detect_browsers()
            if not browsers:
                self.logger.say("未检测到常见浏览器，考试页将走系统默认关联。",
                                "browser: none found; fallback to default association")
                return
            banner, detail = repair_http_default()
            self.logger.say(banner, detail)
            used = browsers[0][0]
            self.logger.say(
                "考试页将优先用 %s 打开；若系统没自动弹开，考试系统里也给了手动地址。"
                % used,
                "browser preferred: %s (%s)" % (used, browsers[0][1]))
        except Exception as e:                                 # noqa: BLE001 故意吞掉
            self.logger.say("确认默认浏览器时遇到问题，已跳过（不影响考试）。",
                            "browser step skipped: %r" % e)

    def step_shortcut(self):
        bat = self.target / "启动考试系统.bat"
        if not bat.is_file():
            raise InstallError("没有找到考试系统的启动文件，无法创建快捷方式。",
                               "shortcut target missing: %s" % bat)
        if self.stage_only:
            lnk = self.target / SHORTCUT_NAME
            create_shortcut(lnk, bat, workdir=str(self.target), description=SHORTCUT_DESC)
            self.logger.say("（干跑）快捷方式生成在本目录内：%s" % lnk, "shortcut=%s" % lnk)
            return
        desktop = self.desktop or get_desktop_path()
        lnk = Path(desktop) / SHORTCUT_NAME
        create_shortcut(lnk, bat, workdir=str(self.target), description=SHORTCUT_DESC)
        if not lnk.is_file():
            raise InstallError("桌面快捷方式创建失败。", "lnk not found: %s" % lnk)
        self.logger.say("已在桌面创建「启动考试系统」快捷方式。",
                        "shortcut=%s -> %s" % (lnk, bat))

    def step_finish(self):
        # 清理安装过程中产生的临时目录（成功才走到这里；失败时保留供排障）
        for d in self.temp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        self.temp_dirs = []
        # 组件安装包从不落进考试系统文件夹，这里核对一遍目录纯度
        expected = set(REQUIRED_KIT_FILES + OPTIONAL_KIT_FILES + [WORKDIR_NAME, "config.json"])
        if self.stage_only:
            expected.add(SHORTCUT_NAME)
        expected.add(LOG_NAME)
        unexpected = [p.name for p in self.target.iterdir() if p.name not in expected]
        if unexpected:
            self.logger.say(
                "考试系统文件夹里有安装器不认识的文件，已保留不动（不影响使用）。",
                "unexpected entries in target: %s" % unexpected)
        else:
            self.logger.say("考试系统文件夹干净整洁，只有考试系统本身。",
                            "target dir contents: %s" % sorted(p.name for p in self.target.iterdir()))

    # ---- 驱动 ----

    def run_step(self, index):
        """执行单个步骤（供 GUI 重试）；返回 True=成功。失败抛 InstallError。"""
        step = self.steps[index]
        self.logger.say("【%d/%d】%s" % (index + 1, len(self.steps), step.title), step.plain)
        t0 = time.time()
        step.func()
        cost = time.time() - t0
        self.logger.say("【%d/%d】%s 完成（用时 %.1f 秒）"
                        % (index + 1, len(self.steps), step.title, cost),
                        "step %s done in %.1fs" % (step.key, cost))
        return True

    def run_all(self, start=0, progress=None):
        """顺序执行全部步骤。progress(i) 在每步开始前回调。失败抛 InstallError。"""
        for i in range(start, len(self.steps)):
            if progress:
                progress(i)
            self.run_step(i)


# --------------------------------------------------------------------------
# 图形向导（tkinter）
# --------------------------------------------------------------------------

def _dump_crash(tb_text):
    """安装器自身崩溃时把堆栈落盘（临时目录 + 桌面各一份），返回桌面文件路径或 None。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = "招新考试系统-安装器崩溃-%s.log" % ts
    content = ("时间：%s　运行形态：%s\n\n%s"
               % (_now(), "exe" if IS_FROZEN else "dev", tb_text))
    for base in (Path(tempfile.gettempdir()),):
        try:
            (base / name).write_text(content, encoding="utf-8")
        except Exception:
            pass
    try:
        dest = Path(get_desktop_path()) / name
        dest.write_text(content, encoding="utf-8")
        return dest
    except Exception:
        return None


def run_gui():
    import tkinter as tk
    from tkinter import messagebox, ttk

    FONT_FAMILY = "Microsoft YaHei UI"

    def F(size, bold=False):
        return (FONT_FAMILY, size, "bold" if bold else "normal")

    class Wizard(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("招新考试系统 安装程序 — Alethic Insight 社团")
            self.geometry("760x640")
            self.minsize(700, 600)
            try:
                ttk.Style().theme_use("vista")
            except tk.TclError:
                pass
            self.option_add("*Font", F(10))

            self.spec = load_spec()
            self.payload = locate_payload()
            self.agent = "kimi"
            self.check_results = {}
            self.engine = None
            self.desktop = None
            self.target_dir = None
            self.debug_enabled = "--debug" in sys.argv
            self.log_queue = queue.Queue()
            self.runner = None
            self.current_step = [0]
            self.logger = None
            self._poll_started = False

            self.container = tk.Frame(self)
            self.container.pack(fill="both", expand=True)
            self.pages = {}
            for cls in (WelcomePage, AgentPage, CheckPage, ProgressPage, FinishPage):
                page = cls(self.container, self)
                self.pages[cls.__name__] = page
                page.place(x=0, y=0, relwidth=1, relheight=1)
            self.show("WelcomePage")
            self.after(120, self.poll_queue)

            def _crash_cb(exc, val, tb):
                dump = _dump_crash("".join(traceback.format_exception(exc, val, tb)))
                try:
                    messagebox.showerror(
                        "安装器遇到意外问题",
                        "发生了一个预料之外的错误，安装可能中断。\n%s\n\n"
                        "请把这个文件发给组织者，已装好的部分重开安装器会自动识别并跳过。"
                        % ("错误记录已保存到桌面：%s" % dump if dump
                           else "错误记录已保存到临时目录（%s）。" % tempfile.gettempdir()))
                except Exception:
                    pass
            self.report_callback_exception = _crash_cb

        def dump_failure_log(self, idx, friendly, detail):
            """失败时把完整安装日志复制一份到桌面，返回路径（失败返回 None）。"""
            try:
                ts = time.strftime("%Y%m%d_%H%M%S")
                dest = Path(get_desktop_path()) / ("招新考试系统-安装失败日志-%s.txt" % ts)
                step_title = self.engine.steps[idx].title if self.engine else "?"
                head = ("失败步骤：%s\n白话原因：%s\n技术详情：\n%s\n\n"
                        "===== 以下为完整安装日志 =====\n"
                        % (step_title, friendly, detail))
                src = self.logger.log_path if self.logger else None
                if src and Path(src).is_file():
                    body = Path(src).read_text(encoding="utf-8", errors="replace")
                else:
                    body = "（安装日志不可读）\n"
                dest.write_text(head + body, encoding="utf-8")
                return dest
            except Exception:
                return None

        def show(self, name):
            self.pages[name].on_show()
            self.pages[name].tkraise()

        # ---- 安装执行（后台线程 -> 队列 -> Tk 主线程）----

        def start_engine(self):
            self.engine = Engine(self.spec, self.payload, self.target_dir,
                                 self.agent, self.logger,
                                 stage_only=False, debug=self.debug_enabled,
                                 desktop_override=self.desktop)
            self.run_from(0)

        def run_from(self, index):
            engine = self.engine
            self.current_step = [index]

            def worker():
                q = self.log_queue
                try:
                    def progress(i):
                        q.put(("begin", i))
                    engine.run_all(start=index, progress=progress)
                    q.put(("done",))
                except InstallError as e:
                    q.put(("failed", self.current_step[0], e.friendly, e.detail))
                except Exception as e:
                    q.put(("failed", self.current_step[0],
                           "安装过程中出现问题：%s" % e, traceback.format_exc()))

            self.runner = worker
            threading.Thread(target=worker, daemon=True).start()

        def poll_queue(self):
            try:
                while True:
                    evt = self.log_queue.get_nowait()
                    kind = evt[0]
                    if kind == "begin":
                        idx = evt[1]
                        self.current_step[0] = idx
                        self.pages["ProgressPage"].set_step(idx)
                    elif kind == "done":
                        self.pages["ProgressPage"].set_done()
                        self.show("FinishPage")
                    elif kind == "failed":
                        _, idx, friendly, detail = evt
                        self.pages["ProgressPage"].set_failed()
                        self.logger.tech("step %s failed:\n%s" % (
                            self.engine.steps[idx].key, detail))
                        dump = self.dump_failure_log(idx, friendly, detail)
                        where = ("\n\n为方便排障，完整日志已复制一份到桌面：\n%s"
                                 "（安装文件夹里的「安装日志.txt」也有同样内容）" % dump
                                 if dump else
                                 "\n\n完整日志在安装文件夹的「安装日志.txt」里，请发给组织者。")
                        retry = messagebox.askretrycancel(
                            "安装遇到问题",
                            friendly + where + "\n\n要重试这一步吗？（点「取消」结束安装，"
                                       "已装好的部分不会坏）")
                        if retry:
                            self.pages["ProgressPage"].set_running()
                            self.run_from(idx)
                        else:
                            self.logger.say("安装被中止。可以重新运行安装程序，"
                                            "已装好的部分会被自动识别并跳过。")
            except queue.Empty:
                pass
            self.after(120, self.poll_queue)

    # ---------------- 页面基类 ----------------

    class Page(tk.Frame):
        def __init__(self, parent, wizard):
            super().__init__(parent, bg="#f5f6fa")
            self.wizard = wizard

        def on_show(self):
            pass

        def head(self, title, sub=""):
            top = tk.Frame(self, bg="#f5f6fa")
            top.pack(fill="x", padx=36, pady=(26, 8))
            tk.Label(top, text=title, font=F(20, True), bg="#f5f6fa",
                     fg="#1a1a2e").pack(anchor="w")
            if sub:
                tk.Label(top, text=sub, font=F(11), bg="#f5f6fa",
                         fg="#555").pack(anchor="w", pady=(4, 0))
            return top

        def nav(self, back=None, next_page=None, next_text="下一步",
                next_cmd=None, enabled=True):
            bar = tk.Frame(self, bg="#f5f6fa")
            bar.pack(side="bottom", fill="x", padx=36, pady=18)
            if back:
                tk.Button(bar, text="上一步", font=F(11), width=12, relief="groove",
                          command=back).pack(side="left")
            if next_page or next_cmd:
                cmd = next_cmd or (lambda: self.wizard.show(next_page))
                tk.Button(bar, text=next_text, font=F(11, True), width=16,
                          bg="#2563eb", fg="white", activebackground="#1d4ed8",
                          activeforeground="white", relief="flat",
                          state="normal" if enabled else "disabled",
                          command=cmd).pack(side="right")

    # ---------------- 页 1：欢迎与声明 ----------------

    class WelcomePage(Page):
        def on_show(self):
            for w in self.winfo_children():
                w.destroy()
            self.head("欢迎使用招新考试系统安装器",
                      "Alethic Insight 社团 · 全程自动安装，不需要任何专业知识")
            box = tk.Frame(self, bg="#f5f6fa")
            box.pack(fill="both", expand=True, padx=36)
            tk.Label(box, text="这个安装器会帮你做好三件事：", font=F(12),
                     bg="#f5f6fa").pack(anchor="w", pady=(0, 6))
            for line in [
                "1. 把「考试系统」放到你桌面的一个文件夹里；",
                "2. 帮新电脑装好常用软件：Python、Node.js、Git、PowerShell 7，"
                "以及 AI 作答工具；",
                "3. 在桌面创建「启动考试系统」快捷方式——以后考试双击它就行。",
            ]:
                tk.Label(box, text=line, font=F(11), bg="#f5f6fa",
                         anchor="w").pack(fill="x")

            # 显眼声明块
            decl_outer = tk.Frame(box, bg="#dc2626", bd=0)
            decl_outer.pack(fill="both", expand=True, pady=(14, 6))
            decl = tk.Frame(decl_outer, bg="#fef2f2")
            decl.pack(fill="both", expand=True, padx=3, pady=3)
            tk.Label(decl, text="★ 重要声明（请花 30 秒读一遍）", font=F(12, True),
                     bg="#fef2f2", fg="#b91c1c").pack(anchor="w", padx=14, pady=(10, 4))
            for line in [
                "① 考试系统本身不进注册表、不永久安装。",
                "    卸载方法 = 直接删除桌面的「%s」文件夹，完事。" % INSTALL_DIR_NAME,
                "② Kimi Code 是 Moonshot（月之暗面）出品的第三方软件，不是本社团开发维护的；",
                "    使用它需要你自备模型或会员账号。",
                "③ opencode 同样是第三方软件，自带限时免费模型（OpenCode Zen 提供，模型 $0）——",
                "    首次使用需联网注册并登录 OpenCode Zen 获取 API key，之后用免费模型不收费；",
                "    没有任何模型账号的同学，建议在下一页选择 opencode。",
                "④ 首次运行时如果 Windows 弹出蓝色的「Windows 已保护你的电脑」提示，",
                "    请点「更多信息」→「仍要运行」。（我们自己打包的软件没有买数字签名，属正常现象）",
            ]:
                tk.Label(decl, text=line, font=F(11), bg="#fef2f2", fg="#7f1d1d",
                         anchor="w").pack(fill="x", padx=14)
            tk.Frame(decl, bg="#fef2f2", height=8).pack(fill="x")
            self.nav(next_page="AgentPage")

    # ---------------- 页 2：选 AI 工具 ----------------

    class AgentPage(Page):
        def on_show(self):
            for w in self.winfo_children():
                w.destroy()
            self.head("选择你的 AI 作答工具", "考试时你将用它来和 AI 一起写代码，二选一即可")
            box = tk.Frame(self, bg="#f5f6fa")
            box.pack(fill="both", expand=True, padx=36)
            self.var = tk.StringVar(value=self.wizard.agent)

            def choice(value, title, lines, recommended=False):
                card = tk.Frame(box, bg="white", bd=1, relief="solid")
                card.pack(fill="x", pady=8, ipady=8)
                row = tk.Frame(card, bg="white")
                row.pack(fill="x", padx=16, pady=4)
                tk.Radiobutton(row, text=title, variable=self.var, value=value,
                               font=F(13, True), bg="white", fg="#111",
                               activebackground="white",
                               command=lambda: self.pick(value)).pack(side="left")
                if recommended:
                    tk.Label(row, text="← 没有模型账号？选这个", font=F(10),
                             bg="white", fg="#d97706").pack(side="right")
                for ln in lines:
                    tk.Label(card, text=ln, font=F(10), bg="white", fg="#555",
                             anchor="w").pack(fill="x", padx=34)

            choice("kimi", "Kimi Code（默认）",
                   ["Moonshot（月之暗面）出品的 AI 编程工具，Web 界面作答。",
                    "需要自备 Kimi 会员或模型账号；没有账号的同学请选下面这个。"])
            choice("opencode", "opencode（自带限时免费模型）",
                   ["第三方开源 AI 编程工具，自带 OpenCode Zen 限时免费模型（模型 $0）。",
                    "首次使用需联网注册并登录 OpenCode Zen 获取 API key（opencode auth login），",
                    "之后使用免费模型不收费。推荐给没有任何模型账号的新生。"],
                   recommended=True)

            tk.Label(box, text="选好后进入下一步，安装器会自动检测你的电脑、装好缺的软件。",
                     font=F(11), bg="#f5f6fa", fg="#555").pack(anchor="w", pady=(10, 0))
            self.nav(back=lambda: self.wizard.show("WelcomePage"), next_page="CheckPage")

        def pick(self, value):
            self.wizard.agent = value

    # ---------------- 页 3：环境检查 ----------------

    class CheckPage(Page):
        ROWS = [
            ("cmd", "cmd 命令行（必需）", True),
            ("powershell", "PowerShell（可选）", True),
            ("python", "Python 3.12", False),
            ("node", "Node.js", False),
            ("git", "Git", False),
            ("pwsh", "PowerShell 7", False),
            ("kimi", "Kimi Code", False),
            ("opencode", "opencode", False),
            ("browser", "默认浏览器", False),
        ]

        def on_show(self):
            for w in self.winfo_children():
                w.destroy()
            wz = self.wizard
            self.head("环境检查", "正在了解你的电脑缺什么——缺的等下会自动装，装过的会自动跳过")
            box = tk.Frame(self, bg="#f5f6fa")
            box.pack(fill="both", expand=True, padx=36)
            self.rows = {}
            grid = tk.Frame(box, bg="white", bd=1, relief="solid")
            grid.pack(fill="x")
            for key, label, _ in self.ROWS:
                row = tk.Frame(grid, bg="white")
                row.pack(fill="x", padx=14, pady=5)
                icon = tk.Label(row, text="…", width=3, font=F(11, True), bg="white",
                                fg="#888")
                icon.pack(side="left")
                tk.Label(row, text=label, font=F(11), bg="white").pack(side="left")
                status = tk.Label(row, text="检查中…", font=F(10), bg="white", fg="#888")
                status.pack(side="right")
                self.rows[key] = (icon, status)
            self.target_lbl = tk.Label(box, text="", font=F(11), bg="#f5f6fa",
                                       fg="#333", anchor="w", wraplength=660,
                                       justify="left")
            self.target_lbl.pack(fill="x", pady=(12, 0))
            self.hint = tk.Label(box, text="", font=F(10), bg="#f5f6fa", fg="#b45309",
                                 anchor="w", wraplength=660, justify="left")
            self.hint.pack(fill="x", pady=(6, 0))
            wz.desktop = get_desktop_path()
            wz.target_dir = Path(wz.desktop) / INSTALL_DIR_NAME
            self.target_lbl.configure(text="考试系统将安装到：%s" % wz.target_dir)
            self.nav(back=lambda: self.wizard.show("AgentPage"),
                     next_cmd=self.go_next, next_text="开始安装",
                     enabled=True)
            self.start_checks()

        def go_next(self):
            wz = self.wizard
            if not wz.check_results:
                messagebox.showinfo("请稍候", "环境检查还没完成，请等几秒再点「开始安装」。")
                return
            if not wz.check_results.get("cmd"):
                messagebox.showerror(
                    "无法继续",
                    "你的电脑缺少 cmd 命令行（Windows 核心组件），安装器无法工作。\n"
                    "请联系组织者。")
                return
            if wz.target_dir.exists():
                go_on = messagebox.askyesno(
                    "已存在安装文件夹",
                    "桌面上已经有「%s」文件夹（可能之前安装过）。\n\n"
                    "选「是」：进入修复模式——重新解压覆盖考试系统文件，已装组件自动跳过；\n"
                    "选「否」：退出安装器（什么都不会改动）。"
                    % INSTALL_DIR_NAME)
                if not go_on:
                    self.wizard.destroy()
                    return
                wz.logger and wz.logger.say("检测到安装文件夹已存在，进入修复模式（重新解压覆盖）。",
                                            "repair mode: target exists")
            wz.show("ProgressPage")

        def on_hide(self):
            pass

        def start_checks(self):
            wz = self.wizard
            q = wz.check_queue = queue.Queue()

            def worker():
                results = {}
                results["cmd"] = probe_cmd()
                results["powershell"] = probe_powershell()
                for key in ("python", "node", "git", "pwsh"):
                    results[key] = probe_component(key, wz.spec)
                if wz.agent in ("kimi", "opencode"):
                    results[wz.agent] = probe_component(wz.agent, wz.spec)
                results["browser"] = detect_browsers()
                q.put(results)

            threading.Thread(target=worker, daemon=True).start()
            self.after(150, lambda: self.poll(q))

        def poll(self, q):
            try:
                results = q.get_nowait()
            except queue.Empty:
                self.after(150, lambda: self.poll(q))
                return
            wz = self.wizard
            wz.check_results = results
            marks = {"ok": ("√", "#16a34a", "已安装，将自动跳过"),
                     "missing": ("○", "#9ca3af", "未安装，稍后自动安装"),
                     "skip": ("！", "#d97706", "未检测到（不影响安装，仅提示）"),
                     "unselected": ("—", "#9ca3af", "本次不安装（上一页选了另一种工具）"),
                     "fail": ("×", "#dc2626", "不可用！安装无法继续")}
            for key, label, _ in self.ROWS:
                icon, status = self.rows[key]
                detail = ""
                if key == "cmd":
                    ok = bool(results.get("cmd"))
                    st = "ok" if ok else "fail"
                    text = "可用" if ok else "不可用！安装无法继续"
                elif key == "powershell":
                    ok = bool(results.get("powershell"))
                    st = "ok" if ok else "skip"
                    text = "可用" if ok else "未检测到——完全不影响安装，忽略即可"
                elif key in ("kimi", "opencode") and key != wz.agent:
                    st = "unselected"
                    text = marks[st][2]
                elif key == "browser":
                    b = results.get("browser") or []
                    if b:
                        st = "ok"
                        names = "、".join(n for n, _ in b)
                        text = "已检测到 %s（考试页将优先用它打开）" % names
                    else:
                        st = "skip"
                        text = "未检测到常见浏览器（考试页将走系统默认关联）"
                else:
                    ok, detail = results.get(key, (False, ""))
                    ok = bool(ok)
                    st = "ok" if ok else "missing"
                    text = marks[st][2]
                if st == "ok" and detail:
                    detail = str(detail).strip().splitlines()[0][:60]
                    text = "已安装，将自动跳过（检测到 %s）" % detail
                icon.configure(text=marks[st][0], fg=marks[st][1])
                status.configure(text=text, fg=marks[st][1])
            self.target_lbl.configure(
                text="考试系统将安装到：%s" % wz.target_dir)
            extra = ""
            py_ok = bool(results.get("python", (False, ""))[0])
            node_ok = bool(results.get("node", (False, ""))[0])
            if not py_ok and not node_ok:
                extra = "\n你的电脑是新电脑也没关系，缺的软件都会自动装好（约需几分钟，请耐心等待）。"
            self.hint.configure(
                text=("作答工具：%s（在上一页选择；装哪个就只装哪个）。%s"
                      % (AGENT_LABELS[wz.agent], extra)))

    # ---------------- 页 4：安装进度 ----------------

    class ProgressPage(Page):
        def on_show(self):
            for w in self.winfo_children():
                w.destroy()
            wz = self.wizard
            if wz.logger is None:
                wz.logger = Logger(wz.target_dir / LOG_NAME, echo=False)
                wz.logger.say("安装目标：%s" % wz.target_dir,
                              "frozen=%s payload_dev=%s debug=%s agent=%s"
                              % (IS_FROZEN, wz.payload.dev, wz.debug_enabled, wz.agent))
            self.head("正在安装…", "请保持电脑开机，不要关掉本窗口；全程自动，一般几分钟内完成")
            box = tk.Frame(self, bg="#f5f6fa")
            box.pack(fill="both", expand=True, padx=36)
            self.total = len(Engine(wz.spec, wz.payload, wz.target_dir, wz.agent,
                                    wz.logger, stage_only=True).steps)
            self.bar = ttk.Progressbar(box, maximum=100, value=0, length=200)
            self.bar.pack(fill="x", pady=(24, 10))
            self.step_lbl = tk.Label(box, text="准备中…", font=F(12, True),
                                     bg="#f5f6fa", fg="#1a1a2e", anchor="w",
                                     wraplength=660, justify="left")
            self.step_lbl.pack(fill="x")
            self.detail_lbl = tk.Label(box, text="", font=F(10), bg="#f5f6fa",
                                       fg="#666", anchor="w", wraplength=660,
                                       justify="left")
            self.detail_lbl.pack(fill="x", pady=(8, 0))
            self.counter_lbl = tk.Label(box, text="", font=F(10), bg="#f5f6fa",
                                        fg="#888", anchor="w")
            self.counter_lbl.pack(fill="x", pady=(14, 0))
            self.wait_lbl = tk.Label(
                box, font=F(10), bg="#f5f6fa", fg="#2563eb", anchor="w",
                wraplength=660, justify="left",
                text="安装已在进行。有的步骤（尤其 Python）要一两分钟没有新动静，"
                     "只要下面的秒数在走就是在正常安装，请勿关闭窗口。")
            self.wait_lbl.pack(fill="x", pady=(8, 0))
            self._spinner = ("◐", "◓", "◑", "◒")
            self._spin_idx = 0
            self._busy = False
            self._ticking = False
            self._step_t0 = None
            self._base_text = ""
            wz.start_engine()

        def tick(self):
            if not (self.winfo_exists() and self._busy):
                self._ticking = False
                return
            self._spin_idx = (self._spin_idx + 1) % len(self._spinner)
            elapsed = int(time.time() - (self._step_t0 or time.time()))
            extra = "，这一步要几分钟，属正常" if elapsed >= 45 else ""
            self.step_lbl.configure(
                text="%s %s（已进行 %d 秒%s）"
                     % (self._spinner[self._spin_idx], self._base_text,
                        elapsed, extra))
            self.after(180, self.tick)

        def set_step(self, idx):
            wz = self.wizard
            steps = wz.engine.steps
            step = steps[idx]
            self.bar.configure(value=100.0 * idx / len(steps))
            self._base_text = step.plain
            self._step_t0 = time.time()
            self._busy = True
            self.step_lbl.configure(text=step.plain, fg="#1a1a2e")
            self.counter_lbl.configure(text="步骤 %d / %d：%s"
                                       % (idx + 1, len(steps), step.title))
            if not self._ticking:
                self._ticking = True
                self.tick()

        def set_running(self):
            self.step_lbl.configure(fg="#1a1a2e")
            self.detail_lbl.configure(text="正在重试这一步…", fg="#2563eb")

        def set_failed(self):
            self._busy = False
            self.step_lbl.configure(fg="#dc2626")

        def set_done(self):
            self._busy = False
            self.bar.configure(value=100)
            self.step_lbl.configure(text="✔ 全部完成，正在打开完成页面…")

    # ---------------- 页 5：完成 ----------------

    class FinishPage(Page):
        def on_show(self):
            for w in self.winfo_children():
                w.destroy()
            wz = self.wizard
            self.head("安装完成！", "接下来的事只有一件：考试的时候双击桌面的「启动考试系统」")
            box = tk.Frame(self, bg="#f5f6fa")
            box.pack(fill="both", expand=True, padx=36)
            text = tk.Text(box, font=F(11), bg="white", relief="flat",
                           wrap="word", padx=16, pady=12, height=16)
            text.pack(fill="both", expand=True)

            def block(title, lines):
                text.insert("end", title + "\n", ("h",))
                for ln in lines:
                    text.insert("end", "  " + ln + "\n")
                text.insert("end", "\n")

            block("如何开始考试", [
                "1. 双击桌面的「启动考试系统」快捷方式；",
                "2. 等浏览器自动打开机试页面，同时你的 AI 工具（%s）会自动打开；"
                % AGENT_LABELS[wz.agent],
                "3. 在机试页面登录学号、看题，在 AI 工具里作答；",
                "4. 答完回到机试页面，点大按钮「清洗并上传」就完成交卷。"])
            block("想单独使用 AI 工具时", [
                "按键盘 Win + R，输入 cmd 回车，在黑窗口里输入：%s"
                % ("kimi" if wz.agent == "kimi" else "opencode"),
                "（安装时已把工具登记进系统，任何位置都能直接用。）"])
            block("如何卸载考试系统", [
                "直接删除桌面的「%s」文件夹即可，" % INSTALL_DIR_NAME,
                "考试系统不进注册表、没有其他残留。"])
            block("长期送你的软件", [
                "Python / Node.js / Git / PowerShell 7 已经作为正常软件装进你的电脑，"
                "可以长期使用；",
                "它们与考试系统无关，删考试文件夹不会影响它们。",
                "想让 Windows 11 终端默认用 PowerShell 7：打开「终端」→ 设置 → 启动 →",
                "默认配置文件 → 选「PowerShell 7 (Alethic)」（不改也完全不影响考试）。"])
            block("遇到问题？", [
                "把桌面「%s」文件夹里的「安装日志.txt」发给社团，我们帮你看。" % INSTALL_DIR_NAME,
                "也可以重新运行本安装器：已装好的部分会自动识别并跳过。"])
            text.configure(state="disabled")
            text.tag_configure("h", font=F(12, True), foreground="#1d4ed8",
                               spacing1=6)

            bar = tk.Frame(self, bg="#f5f6fa")
            bar.pack(side="bottom", fill="x", padx=36, pady=16)
            tk.Button(bar, text="打开安装文件夹", font=F(11), width=16,
                      relief="groove",
                      command=lambda: os.startfile(str(wz.target_dir))).pack(side="left")
            tk.Button(bar, text="查看安装日志", font=F(11), width=14,
                      relief="groove",
                      command=lambda: os.startfile(str(wz.target_dir / LOG_NAME))
                      ).pack(side="left", padx=8)
            tk.Button(bar, text="完成", font=F(11, True), width=12,
                      bg="#2563eb", fg="white", activebackground="#1d4ed8",
                      activeforeground="white", relief="flat",
                      command=self.wizard.destroy).pack(side="right")

    wizard = Wizard()
    wizard.mainloop()


# --------------------------------------------------------------------------
# --stage 干跑模式（自动化验证，无 GUI）
# --------------------------------------------------------------------------

def run_stage(stage_dir, agent, debug):
    stage = Path(stage_dir).resolve()
    logger = Logger(echo=True)
    spec = load_spec(logger)
    payload = locate_payload()
    engine = Engine(spec, payload, stage, agent, logger,
                    stage_only=True, debug=debug, desktop_override=stage)
    errors = 0
    try:
        engine.run_all(progress=lambda i: None)
    except InstallError as e:
        logger.say("干跑失败：%s" % e.friendly, e.detail)
        errors += 1
    except Exception as e:
        logger.say("干跑异常：%s" % e, traceback.format_exc())
        errors += 1

    # 布局自检
    logger.say("---- 干跑产物清单 ----")
    ok = errors == 0
    required = REQUIRED_KIT_FILES + [
        WORKDIR_NAME, "config.json", SHORTCUT_NAME,
        str(Path(WORKDIR_NAME) / WORKDIR_NOTE_NAME)]
    for name in dict.fromkeys(required):
        p = stage / name
        exists = p.exists()
        logger.say(("  [√] %s" if exists else "  [×] 缺失 %s") % name,
                   "" if exists else "missing staged file: %s" % p)
        ok = ok and exists
    cfg_path = stage / "config.json"
    if cfg_path.is_file():
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        good = cfg.get("agent") == agent \
            and cfg.get("agent_workdir") == str(stage / WORKDIR_NAME)
        logger.say("  [%s] config.json (agent=%s, agent_workdir=%s)"
                   % ("√" if good else "×", cfg.get("agent"), cfg.get("agent_workdir")),
                   "config.json content check")
        ok = ok and good
    # 干跑绝不能碰系统 PATH / 注册表（本模式不调用这些函数，这里仅提示核对）
    logger.say("干跑结束%s。目标目录：%s" % ("，全部通过" if ok else "，存在问题", stage),
               "stage_only=True：未执行组件安装、未读写注册表、未改 PATH、未写桌面")
    return 0 if ok else 1


def main():
    parser = argparse.ArgumentParser(description="招新考试系统一键安装器")
    parser.add_argument("--stage", metavar="目录",
                        help="干跑模式：只做解压布局+config+快捷方式到指定目录，"
                             "跳过组件安装与 PATH/注册表")
    parser.add_argument("--agent", choices=["kimi", "opencode"], default="kimi",
                        help="干跑模式下模拟的作答工具选择（默认 kimi）")
    parser.add_argument("--debug", action="store_true",
                        help="在日志中显示子进程原始输出")
    args = parser.parse_args()

    if args.stage:
        try:
            sys.exit(run_stage(args.stage, args.agent, args.debug))
        except Exception:
            _dump_crash(traceback.format_exc())
            raise
    try:
        run_gui()
    except Exception:
        _dump_crash(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()

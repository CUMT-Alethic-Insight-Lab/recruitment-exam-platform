# -*- coding: utf-8 -*-
"""招新考试系统安装器构建脚本（维护机用，维护机可联网，目标机全程离线）。

流程：
  1. 校验 payload_cache/ 五个离线包在位且 SHA256 与 components_spec.json 一致；
  2. 组装 payload/ 目录（payload/kit/ ← exam_kit 根目录 kit 文件 +
     payload/ ← 五个离线包原样 + components_spec.json）；
  3. 检查 PyInstaller（缺失时自动 pip install）；
  4. 在本目录执行 pyinstaller installer.spec --noconfirm；
  5. 构建后自检：产物 exe 存在并记录大小。

用法：
    python build_installer.py               # 全流程：校验→组装→打包→自检
    python build_installer.py --kit-only    # 只做校验+组装 payload/，不打包
    python build_installer.py --no-kit      # 跳过 kit 组装，直接打包（payload/ 已在位时用）

离线包缺失或哈希不符时脚本**不会自动下载**，按提示的 URL 手动下载放到
payload_cache/（URL 即 COMPONENTS.md 里 R1 线实测来源）后重跑。
"""

import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

INSTALLER_DIR = Path(__file__).resolve().parent
EXAM_KIT_DIR = INSTALLER_DIR.parent
PAYLOAD_CACHE = INSTALLER_DIR / "payload_cache"
PAYLOAD_DIR = INSTALLER_DIR / "payload"
SPEC_JSON = INSTALLER_DIR / "components_spec.json"
INSTALLER_APP = INSTALLER_DIR / "installer_app.py"
SPEC_FILE = INSTALLER_DIR / "installer.spec"
EXE_NAME = "招新考试系统-安装程序.exe"
EXE_PATH = INSTALLER_DIR / "dist" / EXE_NAME

# 各离线包的官方下载 URL（与 COMPONENTS.md 一致，仅作缺失时的手工下载提示）
DOWNLOAD_HINTS = {
    "python-3.12.10-amd64.exe":
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe",
    "node-v24.20.0-win-x64.zip":
        "https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip",
    "PortableGit-2.55.0.5-64-bit.7z.exe":
        "https://github.com/git-for-windows/git/releases/download/"
        "v2.55.0.windows.5/PortableGit-2.55.0.5-64-bit.7z.exe",
    "opencode-windows-x64.zip":
        "https://github.com/anomalyco/opencode/releases/download/"
        "v1.18.29/opencode-windows-x64.zip",
    "PowerShell-7.6.5-win-x64.zip":
        "https://github.com/PowerShell/PowerShell/releases/download/"
        "v7.6.5/PowerShell-7.6.5-win-x64.zip",
    "kimi-code-0.40.1-offline.zip":
        "（非下载件）构建机执行：npm install --prefix <tmp> @moonshot-ai/kimi-code@0.40.1 "
        "后对整个 node_modules/ 打 zip，重制步骤见 installer/COMPONENTS.md §5",
}


def log(msg):
    print(msg, flush=True)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def kit_file_names():
    """从 installer_app.py 解析 kit 文件清单（与干跑产物清单保持逐项对齐）。"""
    src = INSTALLER_APP.read_text(encoding="utf-8")
    names = []
    for var in ("REQUIRED_KIT_FILES", "OPTIONAL_KIT_FILES"):
        m = re.search(r"^%s\s*=\s*(\[.*?\])" % var, src, re.M | re.S)
        if not m:
            raise SystemExit("无法从 installer_app.py 解析 %s，请人工核对 kit 清单。" % var)
        names += ast.literal_eval(m.group(1))
    return names


def verify_payload_cache(spec):
    log("== 步骤 1/4：校验 payload_cache/ 离线包 ==")
    if not PAYLOAD_CACHE.is_dir():
        raise SystemExit("缺少目录 %s——离线包未下载。" % PAYLOAD_CACHE)
    bad = []
    for key, comp in spec["components"].items():
        name = comp["file"]
        path = PAYLOAD_CACHE / name
        if not path.is_file():
            log("  [缺] %s（%s）" % (name, comp.get("label", key)))
            bad.append(name)
            continue
        actual = sha256_of(path)
        if actual != comp["sha256"].lower():
            log("  [哈希不符] %s\n      期望 %s\n      实际 %s"
                % (name, comp["sha256"], actual))
            bad.append(name)
        else:
            log("  [OK] %s (%d bytes)" % (name, path.stat().st_size))
    if bad:
        log("\n以下离线包缺失或哈希不符，请按 URL 手动下载/重下后放到 %s："
            % PAYLOAD_CACHE)
        for name in bad:
            log("  %s\n    <- %s" % (name, DOWNLOAD_HINTS.get(name, "见 COMPONENTS.md")))
        raise SystemExit("payload_cache 校验未通过（%d 个文件有问题）。" % len(bad))
    total = len(spec["components"])
    log("payload_cache 校验通过（%d/%d）。\n" % (total, total))


def assemble_payload():
    log("== 步骤 2/4：组装 payload/ ==")
    if PAYLOAD_DIR.exists():
        shutil.rmtree(PAYLOAD_DIR)
    kit_dir = PAYLOAD_DIR / "kit"
    kit_dir.mkdir(parents=True)

    missing_required = []
    for name in kit_file_names():
        src = EXAM_KIT_DIR / name
        if not src.is_file():
            log("  [缺] kit 文件 %s" % name)
            missing_required.append(name)
            continue
        shutil.copy2(src, kit_dir / name)
        log("  [kit] %s (%d bytes)" % (name, src.stat().st_size))
    if missing_required:
        raise SystemExit("exam_kit/ 根目录缺 kit 文件：%s" % missing_required)

    spec = json.loads(SPEC_JSON.read_text(encoding="utf-8"))
    for comp in spec["components"].values():
        src = PAYLOAD_CACHE / comp["file"]
        shutil.copy2(src, PAYLOAD_DIR / comp["file"])
        log("  [组件] %s 原样拷入 payload/" % comp["file"])
    shutil.copy2(SPEC_JSON, PAYLOAD_DIR / SPEC_JSON.name)
    log("  [spec] components_spec.json 拷入 payload/（打包时 installer.spec "
        "会另放一份到 _MEIPASS 根，供 installer_app.load_spec 读取）")

    total = sum(p.stat().st_size for p in PAYLOAD_DIR.rglob("*") if p.is_file())
    log("payload/ 组装完成，总体积 %.1f MB\n" % (total / 1048576))


def ensure_pyinstaller():
    log("== 步骤 3/4：依赖检查 ==")
    try:
        import PyInstaller  # noqa: F401
        ver = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True, text=True, timeout=120).stdout.strip()
        log("PyInstaller 已安装：%s" % ver)
        return
    except ImportError:
        pass
    log("未安装 PyInstaller，执行 pip install ...")
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "pyinstaller"])
    if rc != 0:
        raise SystemExit("pip install pyinstaller 失败（退出码 %d），请手动安装后重跑。" % rc)
    log("PyInstaller 安装完成。")


def run_pyinstaller():
    log("== 步骤 4/4：PyInstaller 打包（onefile + ~220MB payload，需数分钟）==")
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC_FILE),
           "--noconfirm", "--distpath", "dist", "--workpath", "build"]
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=str(INSTALLER_DIR))
    elapsed = time.time() - t0
    if rc != 0:
        raise SystemExit("pyinstaller 失败（退出码 %d），耗时 %.1f 秒。" % (rc, elapsed))

    if not EXE_PATH.is_file():
        raise SystemExit("未找到产物 %s" % EXE_PATH)
    size = EXE_PATH.stat().st_size
    log("\n== 构建后自检 ==")
    log("产物：%s" % EXE_PATH)
    log("大小：%d bytes (%.1f MB)" % (size, size / 1048576))
    log("耗时：%.1f 秒" % elapsed)
    log("\n下一步验证（干跑，不碰系统）：")
    log('  "%s" --stage %%TEMP%%\\m11_stage_kimi --agent kimi' % EXE_PATH)
    log('  "%s" --stage %%TEMP%%\\m11_stage_opencode --agent opencode' % EXE_PATH)


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="招新考试系统安装器构建脚本")
    ap.add_argument("--kit-only", action="store_true",
                    help="只校验 payload_cache 并组装 payload/，不打包")
    ap.add_argument("--no-kit", action="store_true",
                    help="跳过校验与 payload/ 组装，直接打包（payload/ 已在位时用）")
    args = ap.parse_args()

    if args.no_kit and args.kit_only:
        raise SystemExit("--kit-only 与 --no-kit 不能同时使用。")

    spec = json.loads(SPEC_JSON.read_text(encoding="utf-8"))
    if not args.no_kit:
        verify_payload_cache(spec)
        assemble_payload()
    if args.kit_only:
        log("--kit-only：跳过打包。")
        return
    ensure_pyinstaller()
    run_pyinstaller()


if __name__ == "__main__":
    main()

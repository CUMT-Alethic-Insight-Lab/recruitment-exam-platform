"""edge 暂存 → lab 权威库转发（M21，幂等 CLI）。

用法：
    python -m app.staging_forward
由 systemd timer 每 30min 调用（deploy/alethic-recruitment-forward.timer，
Persistent=true 补跑错过的触发）。

流程：先探 lab 可达（GET {LAB_ORIGIN_URL}/api/health；穿 CF Access 的服务
令牌头走 CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET 环境变量）→ 逐个
staging 会话目录 POST {LAB_ORIGIN_URL}/api/internal/ingest-staged（Header
X-Staging-Secret）→ lab 应答 2xx 才删除对应 staging 目录（云端不留数据）；
lab 不可达本轮整体跳过，目录全留待下轮。

HTTP 走 curl 子进程：CF 按 TLS 指纹拦 python-urllib（docs/官网服务器连接
文档.md 已记录该坑），curl 与 nginx 回源同口径可通过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, staging

HTTP_TIMEOUT = 300  # 清洗件/附件可能几十 MB，对齐考生端长超时


def _curl_exe() -> str:
    return shutil.which("curl") or "curl"


def _curl(argv: list[str], timeout: int = HTTP_TIMEOUT) -> tuple[int, str]:
    """curl -sS 执行，返回 (http_code, body)。网络错误 code=0。"""
    proc = subprocess.run(
        [_curl_exe(), "-sS", "--max-time", str(timeout),
         "-w", "\n%{http_code}", *argv],
        capture_output=True, text=True, timeout=timeout + 30)
    body, sep, code = proc.stdout.rpartition("\n")
    if not sep:
        return 0, proc.stdout + proc.stderr
    try:
        return int(code.strip()), body
    except ValueError:
        return 0, proc.stdout + proc.stderr


def _cf_headers() -> list[str]:
    headers = []
    if config.CF_ACCESS_CLIENT_ID:
        headers.append(f"CF-Access-Client-Id: {config.CF_ACCESS_CLIENT_ID}")
    if config.CF_ACCESS_CLIENT_SECRET:
        headers.append(f"CF-Access-Client-Secret: {config.CF_ACCESS_CLIENT_SECRET}")
    return headers


def _http_get(url: str, timeout: int = 15) -> tuple[int, str]:
    argv: list[str] = []
    for h in _cf_headers():
        argv += ["-H", h]
    argv.append(url)
    return _curl(argv, timeout=timeout)


def _http_post_ingest(url: str, fields: dict[str, str],
                      files: list[tuple[str, Path, str]],
                      timeout: int = HTTP_TIMEOUT) -> tuple[int, str]:
    """multipart POST 到 lab ingest-staged；files 为 (表单名, 本地路径, 原始文件名)。"""
    argv: list[str] = ["-X", "POST",
                       "-H", f"X-Staging-Secret: {config.STAGING_SECRET}",
                       "-H", "Accept: application/json"]
    for h in _cf_headers():
        argv += ["-H", h]
    for name, value in fields.items():
        argv += ["-F", f"{name}={value}"]
    for part, path, filename in files:
        argv += ["-F", f"{part}=@{path};filename={filename}"
                 ";type=application/octet-stream"]
    argv.append(url)
    return _curl(argv, timeout=timeout)


def lab_reachable() -> bool:
    if not config.LAB_ORIGIN_URL:
        return False
    try:
        code, _ = _http_get(f"{config.LAB_ORIGIN_URL}/api/health")
    except Exception as exc:
        print(f"[staging_forward] lab 健康探测异常: {exc}")
        return False
    return code == 200


def forward_session(sdir: Path) -> bool:
    """单个 staging 目录 → lab ingest-staged。2xx 视为收口成功。"""
    meta = staging.read_meta(sdir)
    if not meta or not meta.get("uploads"):
        print(f"[staging_forward] {sdir.name}: meta.json 缺失/无内容，跳过")
        return False
    fields = {
        "campaign_id": str(meta.get("campaign_id") or ""),
        "session_id": str(meta.get("session_id") or sdir.name),
        "student_id": str(meta.get("student_id") or ""),
        "repo_url": str(meta.get("repo_url") or ""),
        "manifest": json.dumps({"uploads": meta["uploads"]},
                               ensure_ascii=False),
    }
    files: list[tuple[str, Path, str]] = []
    for entry in meta["uploads"].values():
        for f in (entry or {}).get("files", []):
            path = sdir / f["path"]
            if not path.is_file():
                print(f"[staging_forward] {sdir.name}: 缺文件 {f['path']}，跳过")
                return False
            files.append((f"file:{f['path']}", path, f.get("name") or f["path"]))
    try:
        code, body = _http_post_ingest(
            f"{config.LAB_ORIGIN_URL}/api/internal/ingest-staged", fields, files)
    except Exception as exc:
        print(f"[staging_forward] {sdir.name}: 转发异常: {exc}")
        return False
    if 200 <= code < 300:
        return True
    print(f"[staging_forward] {sdir.name}: lab 应答 {code}: {body[:200]}")
    return False


def run() -> dict:
    root = staging.staging_root()
    dirs = ([d for d in sorted(root.iterdir())
             if d.is_dir() and staging.SESSION_ID_RE.match(d.name)]
            if root.is_dir() else [])
    if not dirs:
        return {"pending": 0, "forwarded": [], "kept": []}
    if not config.LAB_ORIGIN_URL:
        print("[staging_forward] LAB_ORIGIN_URL 未配置，本轮跳过")
        return {"pending": len(dirs), "forwarded": [], "kept": [d.name for d in dirs]}
    if not lab_reachable():
        print("[staging_forward] lab 不可达，本轮跳过（暂存保留）")
        return {"pending": len(dirs), "forwarded": [], "kept": [d.name for d in dirs]}
    forwarded: list[str] = []
    kept: list[str] = []
    for sdir in dirs:
        if forward_session(sdir):
            shutil.rmtree(sdir, ignore_errors=True)  # 云端不留数据
            forwarded.append(sdir.name)
        else:
            kept.append(sdir.name)
    return {"pending": len(dirs), "forwarded": forwarded, "kept": kept}


def main() -> int:
    summary = run()
    print("[staging_forward] " + json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

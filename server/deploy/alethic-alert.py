#!/usr/bin/env python3
"""Alethic Insight 告警发送器：状态翻转时经腾讯企业邮 SMTP 发信。

用法:
  alethic-alert.py check <名称> <ok|bad> <主题> [正文]
    - ok→bad / bad→ok 翻转时立即发信
    - 持续 bad 时每 2 小时重提一次
    - 状态存于 /var/lib/alethic/alert-state.json

邮件为 MIME multipart/alternative（纯文本 + HTML 卡片），
HTML 采用 table 布局 + 内联样式以兼容 Gmail 渲染。

配置: /etc/alethic-alert.env (chmod 600, 不进仓库)
  ALERT_SMTP_USER=alert@alethicinsight.org
  ALERT_SMTP_PASS=<客户端授权码>
  ALERT_TO=qq@example.com,school@example.edu.cn,gmail@example.com
"""
import html
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path

CONF = Path("/etc/alethic-alert.env")
STATE = Path("/var/lib/alethic/alert-state.json")
REALERT_SECS = 2 * 3600
SMTP_HOST = "smtp.exmail.qq.com"
SMTP_PORT = 465
SYSTEM_FOOTER = "Alethic Insight Lab · 招新考试平台监控"

try:
    from zoneinfo import ZoneInfo
    TZ_CN = ZoneInfo("Asia/Shanghai")
except Exception:  # 无 tzdata 时退化为固定 UTC+8
    TZ_CN = timezone(timedelta(hours=8), "Asia/Shanghai")

# 各检查项的 first-response 排查建议（HTML 转义前书写）
ADVICE = {
    "lab-origin": (
        "云端 nginx 已回落到热备副本（GET 只读，POST 全部 503）。",
        "检查 lab 小主机（192.168.3.18）电源与网络是否在线。",
        "上机执行: systemctl status cloudflared; journalctl -u cloudflared -n 100 --no-pager",
        "确认云端 nginx 热备仍在正常应答 GET（curl -H 'Host: alethicinsight.com' http://127.0.0.1/recruitment/api/health）。",
    ),
    "cloudflared": (
        "上机执行: journalctl -u cloudflared -n 100 --no-pager 查看掉线原因。",
        "尝试 systemctl restart cloudflared，并确认服务器出站网络与隧道凭据有效。",
        "隧道恢复前公网访问将由云端热备应答，写操作返回 503。",
    ),
    "recruitment": (
        "上机执行: journalctl -u alethic-recruitment -n 100 --no-pager 定位崩溃原因。",
        "尝试 systemctl restart alethic-recruitment，并确认 127.0.0.1:8901 重新监听。",
        "顺带检查磁盘水位与数据库文件权限，避免因磁盘满导致反复退出。",
    ),
    "disk": (
        "上机执行: df -h 与 du -xh / --max-depth=1 | sort -h 定位占用大户。",
        "优先清理 journal 日志（journalctl --vacuum-size=200M）与过期缓存/备份。",
        "确认招新平台上传目录无异常膨胀。",
    ),
}
DEFAULT_ADVICE = (
    "上机执行 systemctl status 与 journalctl -xe 初步定位异常服务。",
    "如确认为误报，请检查对应检查脚本的阈值与判定逻辑。",
)


def load_conf() -> dict:
    conf = {}
    if CONF.is_file():
        for line in CONF.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip()
    return conf


def load_state() -> dict:
    if STATE.is_file():
        try:
            return json.loads(STATE.read_text())
        except ValueError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def now_cn() -> str:
    return datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S") + " (Asia/Shanghai)"


def build_html(check: str, status: str, subject: str, body: str,
               host: str, ts: str) -> str:
    bad = status == "bad"
    color = "#d93025" if bad else "#188038"
    banner = "🔴 故障告警" if bad else "🟢 恢复通知"
    esc = html.escape
    advice = ADVICE.get(check, DEFAULT_ADVICE)
    advice_rows = "".join(
        f'<li style="margin:4px 0;">{esc(a)}</li>' for a in advice)
    body_html = esc(body).replace("\n", "<br>")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f2f4f8;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2f4f8;padding:24px 0;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:8px;overflow:hidden;font-family:'Segoe UI',Arial,'Microsoft YaHei',sans-serif;border:1px solid #e0e3e8;">
  <tr><td style="background:{color};color:#ffffff;padding:16px 24px;font-size:18px;font-weight:bold;">{banner}</td></tr>
  <tr><td style="padding:24px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;color:#333333;">
      <tr>
        <td style="padding:6px 0;color:#888888;width:88px;vertical-align:top;">检查项</td>
        <td style="padding:6px 0;font-weight:bold;">{esc(subject)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#888888;vertical-align:top;">服务器</td>
        <td style="padding:6px 0;">{esc(host)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#888888;vertical-align:top;">时间</td>
        <td style="padding:6px 0;">{esc(ts)}</td>
      </tr>
      <tr>
        <td style="padding:6px 0;color:#888888;vertical-align:top;">监控项 ID</td>
        <td style="padding:6px 0;font-family:Consolas,monospace;">{esc(check)}</td>
      </tr>
    </table>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">
      <tr><td style="background:#f7f8fa;border-left:4px solid {color};padding:12px 16px;font-size:14px;color:#333333;line-height:1.6;">{body_html}</td></tr>
    </table>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">
      <tr><td style="font-size:14px;color:#333333;font-weight:bold;padding-bottom:6px;">建议排查动作</td></tr>
      <tr><td style="font-size:13px;color:#555555;line-height:1.6;"><ul style="margin:0;padding-left:20px;">{advice_rows}</ul></td></tr>
    </table>
  </td></tr>
  <tr><td style="background:#f7f8fa;color:#999999;font-size:12px;padding:12px 24px;border-top:1px solid #e0e3e8;">{esc(SYSTEM_FOOTER)} · 本邮件由监控脚本自动发出，请勿直接回复</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


def build_text(status: str, subject: str, body: str, host: str,
               ts: str, check: str) -> str:
    mark = "🔴 故障" if status == "bad" else "🟢 恢复"
    advice = "\n".join(f"  - {a}" for a in ADVICE.get(check, DEFAULT_ADVICE))
    return (f"[{mark}] {subject}\n\n"
            f"检查项: {subject}\n"
            f"服务器: {host}\n"
            f"时间: {ts}\n"
            f"监控项 ID: {check}\n\n"
            f"详情:\n{body}\n\n"
            f"建议排查动作:\n{advice}\n\n"
            f"— {SYSTEM_FOOTER}")


def send_mail(conf: dict, subject: str, body: str, html_body: str = None) -> None:
    user = conf.get("ALERT_SMTP_USER", "")
    password = conf.get("ALERT_SMTP_PASS", "")
    recipients = [r.strip() for r in conf.get("ALERT_TO", "").split(",") if r.strip()]
    if not user or not password or not recipients:
        print("alert: 未配置 SMTP 凭据或收件人，仅记录日志", file=sys.stderr)
        return
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    if html_body:
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid("alethic-alert")
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
        smtp.login(user, password)
        smtp.sendmail(user, recipients, msg.as_string())
    print(f"alert: 已发送给 {len(recipients)} 人: {subject}")


def main() -> int:
    if len(sys.argv) < 5 or sys.argv[1] != "check":
        print(__doc__)
        return 2
    name, status, subject = sys.argv[2], sys.argv[3], sys.argv[4]
    body = sys.argv[5] if len(sys.argv) > 5 else subject
    if status not in ("ok", "bad"):
        print("status 须为 ok 或 bad", file=sys.stderr)
        return 2

    state = load_state()
    prev = state.get(name, {})
    prev_status = prev.get("status")
    now = time.time()
    flipped = prev_status is not None and prev_status != status
    realert = status == "bad" and now - prev.get("alerted_at", 0) >= REALERT_SECS
    first_seen = prev_status is None

    state[name] = {"status": status, "updated_at": now,
                   "alerted_at": prev.get("alerted_at", 0)}

    if flipped or realert or (first_seen and status == "bad"):
        host = os.uname().nodename
        mark = "🔴" if status == "bad" else "🟢"
        ts = now_cn()
        full_subject = f"[Alethic告警] {mark} {subject} - {host}"
        send_mail(load_conf(), full_subject,
                  build_text(status, subject, body, host, ts, name),
                  build_html(name, status, subject, body, host, ts))
        state[name]["alerted_at"] = now

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())

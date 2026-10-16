import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ALE_THIC_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("ALE_THIC_DB_PATH", DATA_DIR / "alethic_insight.db"))

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = "dev-secret-key"
    print("[WARN] SECRET_KEY 未设置，使用开发默认值 'dev-secret-key'，生产请通过环境变量设置")

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "00000000")

SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "7"))

STUDENT_ID_REGEX = os.environ.get("STUDENT_ID_REGEX", r"^\d{8}$")

DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))

# 反向代理路径前缀（如 '/recruitment'），nginx strip 后应用自身路由不变，
# 仅用于替换页面里的 __PREFIX__ 占位符和 / 的重定向目标
PUBLIC_PREFIX = os.environ.get("PUBLIC_PREFIX", "").rstrip("/")

# 管理端公网入口（生产 nginx 把 /admin/recruitment 映射到应用内部 /admin），
# 仅用于替换页面里的 __ADMIN_PATH__ 占位符；不设则回退 PUBLIC_PREFIX + '/admin'
ADMIN_PUBLIC_PATH = os.environ.get("ADMIN_PUBLIC_PATH", "").rstrip("/")

# 图形验证码（M16）：护注册与报名问卷提交两个匿名写接口。
# 设 ALE_THIC_CAPTCHA_ENABLED=0 关闭（本地开发/测试），关闭时接口跳过校验。
CAPTCHA_ENABLED = os.environ.get("ALE_THIC_CAPTCHA_ENABLED", "1").strip().lower() \
    not in ("0", "false", "no", "off")

# Headscale/Tailscale P2P 入网：考生机试包凭短时 preauthkey 直连实验室主机上传。
# HEADSCALE_API_KEY 或 LAB_TAILNET_IP 未配置时下发接口返回 503，客户端回退 HTTPS 上传。
HEADSCALE_URL = os.environ.get(
    "HEADSCALE_URL", "https://headscale.alethicinsight.com").rstrip("/")
HEADSCALE_API_KEY = os.environ.get("HEADSCALE_API_KEY", "")
HEADSCALE_USER_ID = int(os.environ.get("HEADSCALE_USER_ID", "2"))
LAB_TAILNET_IP = os.environ.get("LAB_TAILNET_IP", "")

# 实例角色（M21）：authoritative=lab 小主机（权威库）；edge=云端 VPS 只读热备。
# edge 下交卷端点不写主库，multipart 原样暂存 data/staging/{session_id}/，
# 由 staging_forward（systemd timer）转发 lab 收口成功后删除（云端不留数据）。
ROLE = os.environ.get("ALE_THIC_ROLE", "authoritative").strip().lower()
IS_EDGE = ROLE == "edge"

# cloud→lab 暂存转发（edge）：lab 回源地址（含路径前缀，如
# https://lab-origin.alethicinsight.com/recruitment）与共享密钥；
# 穿越 CF Access 的服务令牌头可选（部署细节见 org 私有仓 infra-handoff）。
LAB_ORIGIN_URL = os.environ.get("LAB_ORIGIN_URL", "").rstrip("/")
STAGING_SECRET = os.environ.get("STAGING_SECRET", "")
CF_ACCESS_CLIENT_ID = os.environ.get("CF_ACCESS_CLIENT_ID", "")
CF_ACCESS_CLIENT_SECRET = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")

# exam-archive 即时备份（M22，仅 authoritative）：会话转 uploaded 后把清洗件
# md / 仓库链接 / 附件归档进 org 私有仓的本地克隆并 push。默认关，缺 env 静默跳过；
# push 失败追加 data/archive_queue.jsonl，由 lifespan 后台任务定期补推。
ARCHIVE_ENABLED = os.environ.get("ARCHIVE_ENABLED", "0").strip().lower() \
    in ("1", "true", "yes", "on")
ARCHIVE_REPO_DIR = os.environ.get("ARCHIVE_REPO_DIR", "")
ARCHIVE_GITHUB_TOKEN = os.environ.get("ARCHIVE_GITHUB_TOKEN", "")

# 机试安装包下载签发（R2 转私有 + HMAC 签名 URL，Cloudflare Worker 验签，
# 见 docs/PROPOSAL_r2_download_gating.md）：dl 域名只认 lab 签的短时签名链接。
# KIT_SIGNING_SECRET 与 Worker secret 同值（256-bit 随机），未配置时签发端点 503；
# KIT_DOWNLOAD_OPEN=0 关闭发放窗口（签发 403），替代手工改 HTML 的「暂停下载」。
KIT_SIGNING_SECRET = os.environ.get("KIT_SIGNING_SECRET", "")
KIT_DL_BASE = os.environ.get(
    "KIT_DL_BASE", "https://dl.alethicinsight.com").rstrip("/")
KIT_OBJECT_KEY = os.environ.get(
    "KIT_OBJECT_KEY", "AlethicInsight-ExamKit-2026-setup.exe")
KIT_URL_TTL_SECONDS = int(os.environ.get("KIT_URL_TTL_SECONDS", "21600"))  # 6h
KIT_DOWNLOAD_OPEN = os.environ.get("KIT_DOWNLOAD_OPEN", "1").strip().lower() \
    not in ("0", "false", "no", "off")


def effective_admin_path() -> str:
    return ADMIN_PUBLIC_PATH or (PUBLIC_PREFIX + "/admin")

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


def effective_admin_path() -> str:
    return ADMIN_PUBLIC_PATH or (PUBLIC_PREFIX + "/admin")

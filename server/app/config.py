import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ALE_THIC_DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.environ.get("ALE_THIC_DB_PATH", DATA_DIR / "alethic_insight.db"))

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
if not ADMIN_TOKEN:
    ADMIN_TOKEN = "dev-admin-token"
    print("[WARN] ADMIN_TOKEN 未设置，使用开发默认值 'dev-admin-token'，生产请通过环境变量设置")

STUDENT_ID_REGEX = os.environ.get("STUDENT_ID_REGEX", r"^0826\d{4}$")

DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "30"))

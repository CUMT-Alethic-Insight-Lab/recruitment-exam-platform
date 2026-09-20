from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles

from . import auth, config, db, maintenance
from .routes import accounts, applications, auth as auth_routes, campaigns, exam, interview, questions, review, uploads

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

PREFIX_PLACEHOLDER = "__PREFIX__"
ADMIN_PATH_PLACEHOLDER = "__ADMIN_PATH__"
TOPBAR_PLACEHOLDER = "__TOPBAR__"


def apply_public_prefix(html: str, prefix: str) -> str:
    return html.replace(PREFIX_PLACEHOLDER, prefix)


def apply_admin_path(html: str, admin_path: str) -> str:
    return html.replace(ADMIN_PATH_PLACEHOLDER, admin_path)


def _static_version(name: str) -> str:
    # 发版即重启，启动时按 mtime 生成版本号，拼到静态资源 URL 后，
    # 换版即换 URL，浏览器/CDN 旧缓存直接失效
    try:
        return str(int((STATIC_DIR / name).stat().st_mtime))
    except OSError:
        return "0"


CSS_VERSION = _static_version("css/style.css")
TOPBAR_JS_VERSION = _static_version("js/topbar.js")
MARK_VERSION = _static_version("mark.svg")


def _version_static_urls(html: str) -> str:
    html = html.replace("static/css/style.css", f"static/css/style.css?v={CSS_VERSION}")
    html = html.replace("static/js/topbar.js", f"static/js/topbar.js?v={TOPBAR_JS_VERSION}")
    html = html.replace("static/mark.svg", f"static/mark.svg?v={MARK_VERSION}")
    return html


def render_topbar() -> str:
    path = STATIC_DIR / "_topbar.html"
    if not path.is_file():
        return ""
    html = path.read_text(encoding="utf-8")
    html = apply_public_prefix(html, config.PUBLIC_PREFIX)
    return _version_static_urls(apply_admin_path(html, config.effective_admin_path()))


def render_page(name: str) -> Response:
    path = STATIC_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="页面不存在")
    html = path.read_text(encoding="utf-8")
    html = html.replace(TOPBAR_PLACEHOLDER, render_topbar())
    html = _version_static_urls(html)
    return Response(
        content=apply_admin_path(apply_public_prefix(html, config.PUBLIC_PREFIX), config.effective_admin_path()),
        media_type="text/html; charset=utf-8",
        # 页面 HTML 永不缓存：内联脚本随发版即变，旧 HTML 会复活已修掉的 bug
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


def seed_admin() -> None:
    with db.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM account WHERE role = 'admin'"
        ).fetchone()["c"]
        if count:
            return
        username = config.ADMIN_USERNAME
        salt = auth.new_salt()
        client_hash = auth.compute_client_hash(username, salt)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO account"
            " (student_id, role, nickname, pw_salt, pw_hash, pw_changed, created_at, updated_at)"
            " VALUES (?, 'admin', '', ?, ?, 0, ?, ?)",
            (username, salt, auth.store_hash(client_hash), now, now),
        )
        print(f"[init] 已播种管理员账号 {username}（初始密码为学号，请尽快修改）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    seed_admin()
    try:
        summary = maintenance.purge_expired()
        if summary["purged"]:
            print(f"[maintenance] 启动回收过期批次: {', '.join(summary['purged'])}")
    except Exception as exc:
        print(f"[WARN] 过期批次回收失败（不影响启动）: {exc}")
    yield


app = FastAPI(title="Alethic Insight 招新平台", version="0.1.0", lifespan=lifespan)


# 必须先于 campaigns.router 注册，否则会被 /api/campaigns/{cid} 抢先匹配
@app.get("/api/campaigns/latest-open")
def latest_open_campaign():
    with db.connection() as conn:
        row = conn.execute(
            "SELECT id, name FROM campaign WHERE status = 'open'"
            " ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="no open campaign")
    return {"id": row["id"], "title": row["name"]}


app.include_router(auth_routes.router)
app.include_router(accounts.router)
app.include_router(accounts.admin_router)
app.include_router(campaigns.router)
app.include_router(applications.router)
app.include_router(interview.router)
app.include_router(questions.router)
app.include_router(exam.router)
app.include_router(uploads.router)
app.include_router(review.router)


@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}


@app.get("/", include_in_schema=False)
def index_page():
    return render_page("index.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return render_page("login.html")


@app.get("/account", include_in_schema=False)
def account_page():
    return render_page("account.html")


@app.get("/apply/{cid}", include_in_schema=False)
def apply_page(cid: str):
    return render_page("apply.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return render_page("admin.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .auth import require_admin, verify_ping
from .routes import applications, campaigns, exam, questions

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Alethic Insight 招新平台", version="0.1.0", lifespan=lifespan)


app.include_router(campaigns.router)
app.include_router(applications.router)
app.include_router(questions.router)
app.include_router(exam.router)


@app.post("/api/admin/verify")
def admin_verify(body: dict, request: Request):
    token = str(body.get("token", ""))
    if not token:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
    if not verify_ping(token):
        raise HTTPException(status_code=401, detail="管理员 Token 无效")
    return {"ok": True}


@app.get("/api/admin/verify")
def admin_verify_get(_: None = Depends(require_admin)):
    return {"ok": True}


@app.get("/apply/{cid}", include_in_schema=False)
def apply_page(cid: str):
    return FileResponse(STATIC_DIR / "apply.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/admin")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

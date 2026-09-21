import os
import sys
from pathlib import Path

import pytest

os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ALE_THIC_DB_PATH"] = str(Path(__file__).parent / "test.db")
# 验证码默认关闭：存量用例直发写接口不签名；test_m16 内部按用例局部开启
os.environ["ALE_THIC_CAPTCHA_ENABLED"] = "0"

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, config  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def clean_db():
    db_path = Path(os.environ["ALE_THIC_DB_PATH"])
    if db_path.exists():
        db_path.unlink()
    yield
    if db_path.exists():
        db_path.unlink()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


# 播种管理员初始密码=学号（pw_changed=0），首次使用时统一收敛为该测试密码
ADMIN_TEST_PASSWORD = "Admin#2026pw"


def ensure_admin_password_changed(client):
    """强制改密门禁下，管理接口要求 pw_changed=1：若播种管理员仍是初始密码，
    先走一遍 change_password 流程把密码收敛为 ADMIN_TEST_PASSWORD（幂等）。
    稳态下先试测试密码，避免失败登录计入限流。"""
    res = client.get("/api/auth/salt", params={"student_id": config.ADMIN_USERNAME})
    assert res.status_code == 200, res.text
    salt = res.json()["salt"]
    res = client.post("/api/auth/login", json={
        "student_id": config.ADMIN_USERNAME,
        "client_hash": auth.compute_client_hash(ADMIN_TEST_PASSWORD, salt),
    })
    if res.status_code == 200:
        return  # 稳态：已是测试密码
    assert res.status_code == 401, res.text
    # 首次（播种态）：默认密码=学号，登录后改密收敛
    client_hash = auth.compute_client_hash(config.ADMIN_USERNAME, salt)
    res = client.post("/api/auth/login", json={
        "student_id": config.ADMIN_USERNAME,
        "client_hash": client_hash,
    })
    assert res.status_code == 200, res.text
    assert res.json()["pw_changed"] is False
    new_salt = auth.new_salt()
    res = client.post("/api/auth/change_password", json={
        "old_client_hash": client_hash,
        "new_salt": new_salt,
        "new_client_hash": auth.compute_client_hash(ADMIN_TEST_PASSWORD, new_salt),
    }, headers={"Authorization": f"Bearer {res.json()['token']}"})
    assert res.status_code == 200, res.text


def admin_login(client):
    """管理员统一登录（前置自动收敛密码），返回登录响应。"""
    ensure_admin_password_changed(client)
    res = client.get("/api/auth/salt", params={"student_id": config.ADMIN_USERNAME})
    assert res.status_code == 200, res.text
    client_hash = auth.compute_client_hash(ADMIN_TEST_PASSWORD, res.json()["salt"])
    res = client.post("/api/auth/login", json={
        "student_id": config.ADMIN_USERNAME,
        "client_hash": client_hash,
    })
    assert res.status_code == 200, res.text
    return res


@pytest.fixture(scope="session")
def admin_headers(client):
    # 统一登录：管理员同样走 /api/auth/salt + /api/auth/login（旧 /api/admin/* 已移除）
    res = admin_login(client)
    assert res.json()["role"] == "admin"
    return {"Authorization": f"Bearer {res.json()['token']}"}


@pytest.fixture(scope="session")
def cid(client, admin_headers):
    res = client.post(
        "/api/campaigns",
        json={"id": "2026", "name": "2026 春季招新", "year": 2026},
        headers=admin_headers,
    )
    assert res.status_code in (201, 409)
    return "2026"


def applicant_login(client, cid, student_id, password=None):
    """报名后默认密码 = 学号；返回登录响应 dict（token/student_id/name/pw_changed）。"""
    password = password if password is not None else student_id
    res = client.get("/api/auth/salt", params={"cid": cid, "student_id": student_id})
    assert res.status_code == 200, res.text
    client_hash = auth.compute_client_hash(password, res.json()["salt"])
    res = client.post(f"/api/campaigns/{cid}/auth/login", json={
        "student_id": student_id,
        "client_hash": client_hash,
    })
    assert res.status_code == 200, res.text
    return res.json()


def applicant_headers(client, cid, student_id, password=None):
    data = applicant_login(client, cid, student_id, password)
    return {"Authorization": f"Bearer {data['token']}"}


def change_password(client, student_id, old_password, new_password, token):
    """走一遍改密流程（取盐 → change_password），返回响应。"""
    res = client.get("/api/auth/salt", params={"student_id": student_id})
    assert res.status_code == 200, res.text
    old_hash = auth.compute_client_hash(old_password, res.json()["salt"])
    new_salt = auth.new_salt()
    return client.post("/api/auth/change_password", json={
        "old_client_hash": old_hash,
        "new_salt": new_salt,
        "new_client_hash": auth.compute_client_hash(new_password, new_salt),
    }, headers={"Authorization": f"Bearer {token}"})

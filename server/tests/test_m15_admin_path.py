from pathlib import Path

from app import config
from app.main import ADMIN_PATH_PLACEHOLDER, apply_admin_path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def test_apply_admin_path_replaces_placeholder():
    html = '<a href="__ADMIN_PATH__">管理端</a>'
    assert apply_admin_path(html, "/admin/recruitment") == '<a href="/admin/recruitment">管理端</a>'
    assert apply_admin_path(html, "/admin") == '<a href="/admin">管理端</a>'


def test_effective_admin_path_env_wins(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PUBLIC_PATH", "/admin/recruitment")
    monkeypatch.setattr(config, "PUBLIC_PREFIX", "/recruitment")
    assert config.effective_admin_path() == "/admin/recruitment"


def test_effective_admin_path_fallback(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_PUBLIC_PATH", "")
    monkeypatch.setattr(config, "PUBLIC_PREFIX", "/recruitment")
    assert config.effective_admin_path() == "/recruitment/admin"
    monkeypatch.setattr(config, "PUBLIC_PREFIX", "")
    assert config.effective_admin_path() == "/admin"


def test_pages_use_admin_public_path(client, monkeypatch):
    # 生产形态：设置 ADMIN_PUBLIC_PATH 后各页面管理端入口全部指向新公网地址
    monkeypatch.setattr(config, "ADMIN_PUBLIC_PATH", "/admin/recruitment")
    monkeypatch.setattr(config, "PUBLIC_PREFIX", "/recruitment")
    for path in ("/login", "/admin"):
        res = client.get(path)
        assert res.status_code == 200
        assert 'href="/admin/recruitment">管理端</a>' in res.text   # 顶栏链接
        assert 'data-admin-path="/admin/recruitment"' in res.text  # 顶栏 header 属性
        assert "__PREFIX__/admin" not in res.text
        assert ADMIN_PATH_PLACEHOLDER not in res.text
    # login 的两处 JS 跳转、首页末页 entry slide 的管理端入口按钮
    assert client.get("/login").text.count("'/admin/recruitment'") >= 2
    # 首页是全屏翻页 deck：无顶栏注入；末页 entry slide 不再放管理端入口按钮
    #（3d11c9b 起管理端走 /admin/recruitment 直达，页面不留公开入口），
    # 仅保留报名/登录入口 + 机试安装包下载入口（R2 分发，2026-09-30 恢复下载）
    home = client.get("/")
    assert home.status_code == 200
    assert 'class="slide s-entry"' in home.text
    assert "管理端入口" not in home.text
    assert "机试包更新中 · 暂停下载" not in home.text
    assert "https://dl.alethicinsight.com/AlethicInsight-ExamKit-2026-setup.exe" in home.text
    assert "__PREFIX__/admin" not in home.text
    assert ADMIN_PATH_PLACEHOLDER not in home.text
    # 磁盘文件保持占位符不变（顶栏模板）；index.html 不再内嵌管理端入口占位符
    assert ADMIN_PATH_PLACEHOLDER in (STATIC_DIR / "_topbar.html").read_text(encoding="utf-8")
    assert ADMIN_PATH_PLACEHOLDER not in (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_pages_fallback_to_prefix_admin(client, monkeypatch):
    # 未设置 ADMIN_PUBLIC_PATH：回退 PUBLIC_PREFIX + /admin（本地开发默认即 /admin）
    monkeypatch.setattr(config, "ADMIN_PUBLIC_PATH", "")
    monkeypatch.setattr(config, "PUBLIC_PREFIX", "")
    for path in ("/login", "/admin"):
        res = client.get(path)
        assert res.status_code == 200
        assert 'href="/admin">管理端</a>' in res.text
        assert 'data-admin-path="/admin"' in res.text
        assert "__PREFIX__/admin" not in res.text
    # 首页 deck 末页 entry：管理端入口按钮已移除（直达设计），不应出现
    home = client.get("/")
    assert home.status_code == 200
    assert "管理端入口" not in home.text
    # 生产未设 ADMIN_PUBLIC_PATH 时保持旧行为：PUBLIC_PREFIX + /admin
    monkeypatch.setattr(config, "PUBLIC_PREFIX", "/recruitment")
    res = client.get("/login")
    assert 'href="/recruitment/admin">管理端</a>' in res.text

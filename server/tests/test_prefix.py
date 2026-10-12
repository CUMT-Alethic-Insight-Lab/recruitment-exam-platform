from pathlib import Path
import re

from fastapi.testclient import TestClient

from app.main import ADMIN_PATH_PLACEHOLDER, PREFIX_PLACEHOLDER, apply_public_prefix

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def test_apply_public_prefix_replaces_all_placeholders():
    html = '<a href="__PREFIX__/static/x.css">`__PREFIX__/api/x`</a>'
    assert apply_public_prefix(html, "/recruitment") == '<a href="/recruitment/static/x.css">`/recruitment/api/x`</a>'
    assert apply_public_prefix(html, "") == '<a href="/static/x.css">`/api/x`</a>'


def test_page_routes_serve_html(client):
    for path in ("/", "/login", "/account"):
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 200, f"{path} should serve its page"
        assert PREFIX_PLACEHOLDER not in res.text


def test_home_is_fullscreen_deck_with_entry_slide(client):
    # 首页 = 2026 招新演示全屏翻页 deck：无共享 CSS、无顶栏注入，末页为机试入口
    res = client.get("/")
    assert res.status_code == 200
    assert 'class="slide s-entry"' in res.text
    assert 'id="apply-btn"' in res.text and 'id="apply-hint"' in res.text
    assert 'href="/login">立即报名 / 登录</a>' in res.text  # 占位符已替换
    assert "/api/campaigns/latest-open" in res.text         # 报名按钮动态指向逻辑保留
    assert "ArrowRight" in res.text and "function go(" in res.text  # 键盘/点击翻页（终版无滚轮）
    assert 'data-prefix' not in res.text                    # 不注入顶栏
    assert PREFIX_PLACEHOLDER not in res.text and ADMIN_PATH_PLACEHOLDER not in res.text


def test_deck_fallback_page_served(client):
    # 兜底直出：演示原文件可经 StaticFiles 独立打开，不含占位符
    res = client.get("/static/deck-2026.html")
    assert res.status_code == 200
    assert PREFIX_PLACEHOLDER not in res.text
    assert 'class="slide s-cover"' in res.text


def test_kit_version_json_served(client):
    # 机试包 OTA 版本源：纯静态文件，CF 缓存友好，考生端按此提示新版
    res = client.get("/static/kit-version.json")
    assert res.status_code == 200
    data = res.json()
    assert {"version", "url", "sha256", "notes"} <= set(data)
    assert re.fullmatch(r"\d{4}\.\d{1,2}\.\d{1,2}-\d+", data["version"])


def test_pages_without_prefix_have_no_placeholder(client, cid):
    for path in (f"/apply/{cid}", "/admin"):
        res = client.get(path)
        assert res.status_code == 200
        assert PREFIX_PLACEHOLDER not in res.text
        assert 'href="/static/css/style.css?v=' in res.text
        assert '"/api/' in res.text or "`/api/" in res.text


def test_pages_with_prefix_rewritten(monkeypatch, client, cid):
    monkeypatch.setattr("app.main.config.PUBLIC_PREFIX", "/recruitment")
    for path in (f"/apply/{cid}", "/admin"):
        res = client.get(path)
        assert res.status_code == 200
        assert PREFIX_PLACEHOLDER not in res.text
        assert 'href="/recruitment/static/css/style.css?v=' in res.text
        assert "/recruitment/api/" in res.text
        assert '`/api/' not in res.text and '"/api/' not in res.text
    # 磁盘文件保持占位符不变
    for name in ("apply.html", "admin.html"):
        assert PREFIX_PLACEHOLDER in (STATIC_DIR / name).read_text(encoding="utf-8")

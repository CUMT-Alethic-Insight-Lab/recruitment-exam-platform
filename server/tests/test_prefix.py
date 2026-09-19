from pathlib import Path

from fastapi.testclient import TestClient

from app.main import PREFIX_PLACEHOLDER, apply_public_prefix

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

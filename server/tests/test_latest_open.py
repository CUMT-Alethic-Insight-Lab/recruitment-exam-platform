"""GET /api/campaigns/latest-open：最新 open 批次的公开查询接口。"""


def _create_campaign(client, admin_headers, cid, name):
    res = client.post(
        "/api/campaigns",
        json={"id": cid, "name": name, "year": 2026},
        headers=admin_headers,
    )
    assert res.status_code in (201, 409), res.text


def test_latest_open_404_when_no_campaigns(client):
    # 库中还没有任何批次（cid fixture 尚未触发）
    res = client.get("/api/campaigns/latest-open")
    assert res.status_code == 404
    assert res.json() == {"detail": "no open campaign"}


def test_latest_open_returns_newest_open(client, admin_headers):
    _create_campaign(client, admin_headers, "t2old", "早批次")
    _create_campaign(client, admin_headers, "t2new", "新批次")

    res = client.get("/api/campaigns/latest-open")
    assert res.status_code == 200
    assert res.json() == {"id": "t2new", "title": "新批次"}


def test_latest_open_ignores_closed(client, admin_headers):
    _create_campaign(client, admin_headers, "t2newest", "最新批次")

    res = client.get("/api/campaigns/latest-open")
    assert res.json()["id"] == "t2newest"

    # 关闭最新批次后，应回退到仍为 open 的最新批次，closed 不算
    res = client.patch(
        "/api/campaigns/t2newest", json={"status": "closed"}, headers=admin_headers
    )
    assert res.status_code == 200, res.text
    res = client.get("/api/campaigns/latest-open")
    assert res.status_code == 200
    assert res.json()["id"] == "t2new"

    # 全部关闭后恢复 404
    for cid in ("t2old", "t2new"):
        res = client.patch(
            f"/api/campaigns/{cid}", json={"status": "closed"}, headers=admin_headers
        )
        assert res.status_code == 200, res.text
    res = client.get("/api/campaigns/latest-open")
    assert res.status_code == 404
    assert res.json() == {"detail": "no open campaign"}

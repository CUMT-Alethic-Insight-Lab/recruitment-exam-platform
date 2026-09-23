"""报名通道临时开关 exam_config.apply_closed：
入口 latest-open 隐藏、报名写入（submit/register）拒绝；批次收口同样拒绝；
机试 enroll 链路不受 apply_closed 影响。"""
import sys

from conftest import applicant_headers

sys.path.insert(0, ".")


def _create_campaign(client, admin_headers, cid):
    res = client.post("/api/campaigns",
                      json={"id": cid, "name": "报名开关测试", "year": 2026},
                      headers=admin_headers)
    assert res.status_code in (201, 409), res.text


def _set_apply_closed(client, admin_headers, cid, closed=True):
    res = client.patch(f"/api/campaigns/{cid}",
                       json={"exam_config": {"apply_closed": closed}},
                       headers=admin_headers)
    assert res.status_code == 200, res.text


def _close_campaign(client, admin_headers, cid):
    res = client.patch(f"/api/campaigns/{cid}", json={"status": "closed"},
                       headers=admin_headers)
    assert res.status_code == 200, res.text


def _app_payload(student_id, name):
    return {
        "student_id": student_id,
        "name": name,
        "gender": "female",
        "class_name": "计科244",
        "qq": "600600601",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "research",
        "expectation": "报名开关测试",
        "exam_mode": "standard",
        "overwrite": False,
    }


def test_latest_open_hides_apply_closed(client, admin_headers):
    # 自备一个更早的可报名批次作回退底，保证用例独立运行时也可判定
    _create_campaign(client, admin_headers, "ac17base")
    _create_campaign(client, admin_headers, "ac17a")
    res = client.get("/api/campaigns/latest-open")
    assert res.status_code == 200, res.text
    assert res.json()["id"] == "ac17a"

    # 临时关闭报名后：入口对考生隐藏，回退到上一个可报名批次
    _set_apply_closed(client, admin_headers, "ac17a")
    res = client.get("/api/campaigns/latest-open")
    assert res.status_code == 200, res.text
    assert res.json()["id"] == "ac17base"

    # 恢复后回到最新
    _set_apply_closed(client, admin_headers, "ac17a", closed=False)
    res = client.get("/api/campaigns/latest-open")
    assert res.json()["id"] == "ac17a"

    _close_campaign(client, admin_headers, "ac17a")
    _close_campaign(client, admin_headers, "ac17base")


def test_apply_write_rejected_when_apply_closed(client, admin_headers):
    _create_campaign(client, admin_headers, "ac17b")
    _set_apply_closed(client, admin_headers, "ac17b")

    res = client.post("/api/campaigns/ac17b/applications",
                      json=_app_payload("08261711", "关通道提交"))
    assert res.status_code == 403, res.text
    assert "暂时关闭" in res.json()["detail"]

    res = client.post("/api/campaigns/ac17b/applications/register",
                      json={"student_id": "08261712", "name": "关通道注册", "gender": "male"})
    assert res.status_code == 403, res.text
    assert "暂时关闭" in res.json()["detail"]

    _close_campaign(client, admin_headers, "ac17b")


def test_apply_write_rejected_when_campaign_closed(client, admin_headers):
    _create_campaign(client, admin_headers, "ac17c")
    _close_campaign(client, admin_headers, "ac17c")

    res = client.post("/api/campaigns/ac17c/applications",
                      json=_app_payload("08261713", "收口提交"))
    assert res.status_code == 403, res.text
    assert "已收口" in res.json()["detail"]

    res = client.post("/api/campaigns/ac17c/applications/register",
                      json={"student_id": "08261714", "name": "收口注册", "gender": "male"})
    assert res.status_code == 403, res.text
    assert "已收口" in res.json()["detail"]


def test_exam_enroll_unaffected_by_apply_closed(client, admin_headers):
    _create_campaign(client, admin_headers, "ac17d")
    res = client.post("/api/campaigns/ac17d/applications",
                      json=_app_payload("08261715", "照常开考"))
    assert res.status_code == 200, res.text
    res = client.post("/api/questions", json={
        "domain": "llm_arch", "track": "track1", "title": "开关测试题",
        "body": "开关测试题题面", "active": True,
    }, headers=admin_headers)
    assert res.status_code == 201, res.text

    _set_apply_closed(client, admin_headers, "ac17d")
    # 报名已关：新提交被拒
    res = client.post("/api/campaigns/ac17d/applications",
                      json=_app_payload("08261716", "关门后提交"))
    assert res.status_code == 403, res.text
    # 但已有报名记录的考生照常开考
    headers = applicant_headers(client, "ac17d", "08261715")
    res = client.post("/api/campaigns/ac17d/exam/enroll",
                      json={"track": "track1"}, headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["session_id"]

    _close_campaign(client, admin_headers, "ac17d")

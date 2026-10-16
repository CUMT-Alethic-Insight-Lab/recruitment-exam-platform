import csv
import io

from conftest import applicant_login


def test_apply_page_served(client, cid):
    res = client.get(f"/apply/{cid}")
    assert res.status_code == 200
    assert "报名问卷" in res.text


def test_submit_application(client, cid):
    payload = {
        "student_id": "08261234",
        "name": "张三",
        "gender": "male",
        "class_name": "计科244",
        "qq": "123456789",
        "lab_intent": True,
        "domains": ["llm_arch", "agent"],
        "focus_next_year": "research",
        "expectation": "想学东西",
    }
    res = client.post(f"/api/campaigns/{cid}/applications", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["student_id"] == "08261234"
    assert data["domains"] == ["llm_arch", "agent"]
    assert data["status"] == "applied"


def test_duplicate_409_then_overwrite(client, cid):
    payload = {
        "student_id": "08260001",
        "name": "李四",
        "gender": "female",
        "class_name": "软工243",
        "qq": "987654321",
        "lab_intent": False,
        "domains": ["cv_arch"],
        "focus_next_year": "grades",
        "expectation": "",
    }
    res = client.post(f"/api/campaigns/{cid}/applications", json=payload)
    assert res.status_code == 200

    res2 = client.post(f"/api/campaigns/{cid}/applications", json=payload)
    assert res2.status_code == 409
    assert res2.json()["detail"]["existing_name"] == "李四"

    # 已完成注册的报名：匿名/他人覆盖一律拒绝（M26），仅本人登录态可改
    payload["name"] = "李四（改）"
    res_anon = client.post(f"/api/campaigns/{cid}/applications",
                           json={**payload, "overwrite": True})
    assert res_anon.status_code == 401

    login = applicant_login(client, cid, "08260001")
    own = {"Authorization": f"Bearer {login['token']}"}
    other = {"Authorization": f"Bearer {applicant_login(client, cid, '08261234')['token']}"}
    res_other = client.post(f"/api/campaigns/{cid}/applications",
                            json={**payload, "overwrite": True}, headers=other)
    assert res_other.status_code == 403

    res3 = client.post(f"/api/campaigns/{cid}/applications",
                       json={**payload, "overwrite": True}, headers=own)
    assert res3.status_code == 200
    assert res3.json()["name"] == "李四（改）"


def test_student_id_format(client, cid):
    base = {
        "student_id": "08261234",
        "name": "王五",
        "gender": "male",
        "class_name": "计科244",
        "qq": "111222333",
        "lab_intent": True,
        "domains": ["rl"],
        "focus_next_year": "project",
        "expectation": "",
    }
    bad = {"0826": "xxx", "082612345": "08261234", "08260000x": "08260000"}
    for bad_id in bad:
        payload = {**base, "student_id": bad_id}
        res = client.post(f"/api/campaigns/{cid}/applications", json=payload)
        assert res.status_code == 422, f"should reject {bad_id}"


def test_domains_limit(client, cid):
    base = {
        "student_id": "08261235",
        "name": "赵六",
        "gender": "female",
        "class_name": "计科244",
        "qq": "111222333",
        "lab_intent": True,
        "domains": ["rl", "agent", "memory", "infra"],
        "focus_next_year": "project",
        "expectation": "",
    }
    res = client.post(f"/api/campaigns/{cid}/applications", json=base)
    assert res.status_code == 422


def test_list_requires_admin(client, cid):
    res = client.get(f"/api/campaigns/{cid}/applications")
    assert res.status_code == 401


def test_list_and_filter(client, cid, admin_headers):
    res = client.get(f"/api/campaigns/{cid}/applications", headers=admin_headers)
    assert res.status_code == 200
    assert len(res.json()) >= 2

    res = client.get(f"/api/campaigns/{cid}/applications?lab_intent=true", headers=admin_headers)
    assert all(a["lab_intent"] for a in res.json())

    res = client.get(f"/api/campaigns/{cid}/applications?q=张三", headers=admin_headers)
    assert all("张三" in (a["name"] + a["student_id"]) for a in res.json())


def test_resolve_endpoint(client, cid, admin_headers):
    res = client.get(f"/api/campaigns/{cid}/applications/08261234", headers=admin_headers)
    assert res.status_code == 200
    assert res.json()["name"] == "张三"
    res = client.get(f"/api/campaigns/{cid}/applications/08269999", headers=admin_headers)
    assert res.status_code == 404


def test_csv_export(client, cid, admin_headers):
    res = client.get(f"/api/campaigns/{cid}/applications/export.csv", headers=admin_headers)
    assert res.status_code == 200
    text = res.content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0][0] == "学号"
    assert any(r[0] == "08261234" for r in rows)


def test_email_domain_values_rejected(client, cid):
    base = {
        "student_id": "08261236",
        "name": "钱七",
        "gender": "male",
        "class_name": "计科244",
        "qq": "111222333",
        "lab_intent": True,
        "domains": ["llm_arch"],
        "focus_next_year": "project",
        "expectation": "",
    }
    for bad in ["llm", "cv", "aixx"]:
        res = client.post(f"/api/campaigns/{cid}/applications", json={**base, "domains": [bad]})
        assert res.status_code == 422, f"should reject domain {bad}"
    for bad_gender in ["男", "M", ""]:
        res = client.post(f"/api/campaigns/{cid}/applications", json={**base, "gender": bad_gender,
                                                                      "student_id": "08261237"})
        assert res.status_code == 422, f"should reject gender {bad_gender!r}"

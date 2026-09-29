"""/api/scenarios/draft 接口测试。"""
import pytest

from app import app

TPL = ("{g, select, male{He} female{She} other{They}} "
       "{n, plural, =0{none} one{# one} other{# many}}")


@pytest.fixture()
def client():
    app.testing = True
    return app.test_client()


def post(client, **over):
    body = {"template": TPL, "locale": "en", "path_key": "n1:male/n2:=0",
            "variables": {}}
    body.update(over)
    return client.post("/api/scenarios/draft", json=body)


def test_draft_success(client):
    resp = post(client, variables={"g": "female", "n": 3})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["scenario"]["variables"] == {"g": "male", "n": 0}
    assert data["verification"]["output"] == "He none"
    assert data["verification"]["covered"] is True
    assert {b["variable"] for b in data["basis"]} == {"g", "n"}


def test_draft_category_with_samples(client):
    resp = post(client, path_key="n1:female/n2:one",
                variables={"g": "female", "n": 1})
    data = resp.get_json()
    assert data["ok"] is True
    assert data["scenario"]["variables"] == {"g": "female", "n": 1}
    assert data["verification"]["output"] == "She 1 one"


def test_draft_no_solution_reason(client):
    # 同一变量同时要求 =1 与 =2
    tpl = "{n, plural, =1{a} other{b}}-{n, plural, =2{c} other{d}}"
    resp = client.post("/api/scenarios/draft", json={
        "template": tpl, "locale": "en",
        "path_key": "n1:=1/n2:=2", "variables": {},
    })
    data = resp.get_json()
    assert resp.status_code == 200
    assert data["ok"] is False
    assert data["reason"] == "no_solution"
    assert data["error"]


def test_draft_unreachable_reason(client):
    resp = client.post("/api/scenarios/draft", json={
        "template": "{n, plural, one{A} other{B}}",
        "locale": "zh-Hans", "path_key": "n1:one", "variables": {},
    })
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "unreachable"


def test_draft_path_not_found_reason(client):
    resp = post(client, path_key="n1:male/n9:one")
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "path_not_found"


def test_draft_invalid_path_key_reason(client):
    resp = post(client, path_key="garbage")
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "invalid_path_key"


def test_draft_template_error_reason(client):
    resp = client.post("/api/scenarios/draft", json={
        "template": "{n, plural, one{a}}",
        "locale": "en", "path_key": "n1:one", "variables": {},
    })
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "template_error"


def test_draft_enum_limit_reason(client):
    tpl = " ".join("{v%d, select, a{x} b{y} other{z}}" % i for i in range(6))
    key = "/".join(["n1:other"] + ["n%d:a" % i for i in range(2, 7)])
    resp = client.post("/api/scenarios/draft", json={
        "template": tpl, "locale": "en", "path_key": key, "variables": {},
    })
    data = resp.get_json()
    assert data["ok"] is False
    assert data["reason"] == "enum_limit"


def test_draft_rejects_non_dict_body(client):
    resp = client.post("/api/scenarios/draft",
                       data="[]", content_type="application/json")
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_draft_rejects_bad_template_type(client):
    resp = post(client, template=123)
    assert resp.status_code == 400


def test_draft_rejects_bad_locale(client):
    resp = post(client, locale="")
    assert resp.status_code == 400


def test_draft_rejects_non_dict_variables(client):
    resp = post(client, variables=[])
    assert resp.status_code == 400


def test_draft_default_name_and_custom_name(client):
    data = post(client).get_json()
    assert data["scenario"]["name"] == "草稿：male/=0"
    data2 = post(client, name="自定义").get_json()
    assert data2["scenario"]["name"] == "自定义"


def test_draft_does_not_mutate_scenario_set(client):
    # 草稿接口无状态：连续调用不依赖、也不改变任何已保存场景
    r1 = post(client).get_json()
    r2 = post(client, path_key="n1:female/n2:other",
              variables={"g": "female", "n": 2}).get_json()
    assert r1["ok"] and r2["ok"]
    assert r1["scenario"]["variables"] != r2["scenario"]["variables"]

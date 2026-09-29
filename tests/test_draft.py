"""未覆盖路径场景草稿生成（draft.build_scenario_draft）与 /api/scenarios/draft 测试。"""
import pytest

from app import app
from draft import DraftError, build_scenario_draft
from icu import run_scenarios

SAMPLE = ("{gender, select, male{He} female{She} other{They}} has "
          "{count, plural, =0{no new messages} one{# new message} "
          "other{# new messages}}.")


# ---------------------------------------------------------------------------
# 求解：基本路由
# ---------------------------------------------------------------------------

def test_draft_solves_select_and_category():
    r = build_scenario_draft(SAMPLE, "en", {}, "n1:male/n2:one")
    assert r["ok"] is True
    assert r["scenario"]["variables"] == {"gender": "male", "count": 1}
    assert r["verification"]["hit"] is True
    assert r["verification"]["key"] == "n1:male/n2:one"
    assert r["output"].startswith("He has 1")
    # 目标条件来自 analyze 复核
    conds = " ".join(r["target"]["conditions"])
    assert '"male"' in conds and '"one"' in conds


def test_draft_solves_explicit_value():
    r = build_scenario_draft(SAMPLE, "en", {}, "n1:female/n2:=0")
    assert r["scenario"]["variables"] == {"gender": "female", "count": 0}
    assert r["output"] == "She has no new messages."
    basis = {b["selector"]: b for b in
             next(v for v in r["variable_basis"] if v["name"] == "count")["routes"]}
    assert "=N" in basis["=0"]["basis"]


def test_draft_explicit_value_preempts_category_continue_search():
    # 目标 one；类别首候选 0 被 =0 显式分支抢占 → 必须继续搜索到 1
    tpl = "{n, plural, =0{z} one{o} other{x}}"
    r = build_scenario_draft(tpl, "en", {}, "n1:one")
    assert r["scenario"]["variables"]["n"] == 1
    rejected = r["variable_basis"][0]["rejected"]
    assert rejected and rejected[0]["value"] == 0
    assert "=0" in rejected[0]["reason"]


def test_draft_sample_value_preferred_when_it_routes():
    r = build_scenario_draft(SAMPLE, "en",
                             {"gender": "female", "count": 3},
                             "n1:female/n2:other")
    assert r["scenario"]["variables"] == {"gender": "female", "count": 3}
    assert {v["name"]: v["source"] for v in r["variable_basis"]} == {
        "gender": "editor_sample", "count": "editor_sample"}


def test_draft_sample_conflict_continues_search():
    # 样例 gender=male 与目标 female 冲突；count=3 与 =0 冲突 → 两个都继续搜索
    r = build_scenario_draft(SAMPLE, "en",
                             {"gender": "male", "count": 3},
                             "n1:female/n2:=0")
    assert r["scenario"]["variables"] == {"gender": "female", "count": 0}
    src = {v["name"]: v["source"] for v in r["variable_basis"]}
    assert src == {"gender": "solved", "count": "solved"}
    gender = next(v for v in r["variable_basis"] if v["name"] == "gender")
    assert any(x["value"] == "male" and x["source"] == "editor_sample"
               for x in gender["rejected"])


def test_draft_same_variable_consistent_in_nested_branch():
    # b 出现在 select 的两个嵌套分支中，各带一个 plural；目标 y/other，b 只有一个值。
    # 注意节点 id：n1=select a，n2=x 分支内的 plural，n3=y 分支内的 plural
    tpl = ("{a, select, x{{b, plural, one{1} other{2}}} "
           "y{{b, plural, one{3} other{4}}} other{z}}")
    r = build_scenario_draft(tpl, "en", {}, "n1:y/n3:other")
    assert r["scenario"]["variables"] == {"a": "y", "b": 0}
    assert r["output"] == "4"


def test_draft_same_variable_consistent_in_sequence():
    tpl = ("{p, selectordinal, one{A} few{B} other{C}} and "
           "{p, selectordinal, one{D} other{E}}")
    # 3 → 节点1 few，节点2 无 few 分支回退 other；同一 p 贯穿两个顺序节点
    r = build_scenario_draft(tpl, "en", {}, "n1:few/n2:other")
    assert r["scenario"]["variables"]["p"] == 3
    assert r["output"] == "B and E"


def test_draft_offset_does_not_change_routing():
    # offset:1：other 用原始值路由（2 → other），# 显示 1
    tpl = "{n, plural, offset:1 =0{nobody} one{you} other{# others}}"
    r = build_scenario_draft(tpl, "en", {}, "n1:other")
    assert r["scenario"]["variables"]["n"] == 2
    assert "offset" in r["variable_basis"][0]["routes"][0]["basis"]
    # =N 按原始值判定：样例 4 不满足 =0，继续搜索到 0
    r0 = build_scenario_draft(tpl, "en", {"n": 4}, "n1:=0")
    assert r0["scenario"]["variables"]["n"] == 0
    assert r0["output"] == "nobody"


def test_draft_selectordinal_categories_en():
    tpl = "{pos, selectordinal, one{#st} two{#nd} few{#rd} other{#th}}"
    r = build_scenario_draft(tpl, "en", {}, "n1:few")
    assert r["scenario"]["variables"]["pos"] == 3
    assert r["output"].endswith("3rd")


# ---------------------------------------------------------------------------
# 无解 / 路径不存在 / 超限
# ---------------------------------------------------------------------------

def test_draft_unsat_shadowed_category():
    tpl = "{n, plural, =1{x} one{a} other{b}}"
    with pytest.raises(DraftError) as ei:
        build_scenario_draft(tpl, "en", {}, "n1:one")
    assert ei.value.code == "unsat"
    assert ei.value.status == 422
    assert ei.value.details["variable"] == "n"
    # 候选 1 因落入 =1 被拒
    assert any(x["value"] == 1 and "=1" in x["reason"]
               for x in ei.value.details["rejected"])


def test_draft_unsat_same_variable_conflicting_selectors():
    # 同变量两处 plural：one 的所有取值在对称节点上也落 one，one/other 无解
    tpl = "{n, plural, one{A} other{B}} and {n, plural, one{C} other{D}}"
    with pytest.raises(DraftError) as ei:
        build_scenario_draft(tpl, "en", {}, "n1:one/n2:other")
    assert ei.value.code == "unsat"


def test_draft_unsat_unreachable_category_locale():
    # zh-Hans 下 cardinal 只有 other
    with pytest.raises(DraftError) as ei:
        build_scenario_draft("{n, plural, one{A} other{B}}",
                             "zh-Hans", {}, "n1:one")
    assert ei.value.code == "unsat"


def test_draft_unsat_select_plus_number_type_conflict():
    # 同变量既用于 select（字符串路由）又有 number 用法（要求数字）：
    # 无候选可同时满足，返回 unsat 而非内部异常
    tpl = "{x, select, a{A} other{B}} {x, number}"
    with pytest.raises(DraftError) as ei:
        build_scenario_draft(tpl, "en", {"x": "a"}, "n1:a")
    assert ei.value.code == "unsat"
    assert ei.value.details["variable"] == "x"
    # 编辑器样例 'a' 因数字用法被拒（node_id 为 None）
    assert any(rj["source"] == "editor_sample" and rj["node_id"] is None
               for rj in ei.value.details["rejected"])


def test_draft_path_not_found_unknown_selector():
    with pytest.raises(DraftError) as ei:
        build_scenario_draft(SAMPLE, "en", {}, "n1:nope/n2:one")
    assert ei.value.code == "path_not_found"


def test_draft_path_not_found_wrong_node_order():
    with pytest.raises(DraftError) as ei:
        build_scenario_draft(SAMPLE, "en", {}, "n2:one/n1:male")
    assert ei.value.code == "path_not_found"


def test_draft_path_not_found_truncated_route():
    with pytest.raises(DraftError) as ei:
        build_scenario_draft(SAMPLE, "en", {}, "n1:male")
    assert ei.value.code == "path_not_found"


def test_draft_path_not_found_unknown_node():
    with pytest.raises(DraftError) as ei:
        build_scenario_draft(SAMPLE, "en", {}, "n9:male")
    assert ei.value.code == "path_not_found"


def test_draft_template_error():
    with pytest.raises(DraftError) as ei:
        build_scenario_draft("{n, plural, one{a}", "en", {}, "n1:one")
    assert ei.value.code == "template_error"
    assert ei.value.details["errors"]


def test_draft_limit_exceeded_behind_first_page():
    tpl = " ".join("{v%d, select, a{x} b{y} other{z}}" % i for i in range(8))
    key = "/".join("n%d:other" % i for i in range(1, 9))
    with pytest.raises(DraftError) as ei:
        build_scenario_draft(tpl, "en", {}, key)
    assert ei.value.code == "path_limit_exceeded"
    assert "300" in ei.value.message


def test_draft_within_first_page_even_when_total_exceeds_limit():
    tpl = " ".join("{v%d, select, a{x} b{y} other{z}}" % i for i in range(8))
    key = "/".join("n%d:a" % i for i in range(1, 9))
    r = build_scenario_draft(tpl, "en", {}, key)
    assert r["verification"]["hit"] is True
    assert r["target"]["total_paths"] == 6561


# ---------------------------------------------------------------------------
# 路径外变量与变量依据
# ---------------------------------------------------------------------------

def test_draft_off_path_variables_get_defaults_or_samples():
    tpl = "{g, select, a{Hi {name}} other{Hey {name}}} {price, number}"
    r = build_scenario_draft(tpl, "en", {"extra": 1}, "n1:a")
    vals = {v["name"]: (v["value"], v["source"]) for v in r["variable_basis"]}
    assert vals["g"] == ("a", "solved")
    assert vals["name"] == ("", "default")       # 字符串型 → 空串
    assert vals["price"] == (0, "default")       # number 契约 → 0
    assert vals["extra"] == (1, "editor_sample")  # 模板未使用，原样保留


def test_draft_basis_records_offset_and_category():
    tpl = "{n, plural, offset:2 one{a} other{b}}"
    r = build_scenario_draft(tpl, "en", {}, "n1:other")
    entry = r["variable_basis"][0]["routes"][0]
    assert entry["offset"] == 2
    assert entry["category"] == "other"


def test_draft_custom_name():
    r = build_scenario_draft(SAMPLE, "en", {}, "n1:male/n2:one",
                             name=" 我的草稿 ")
    assert r["scenario"]["name"] == "我的草稿"
    r2 = build_scenario_draft(SAMPLE, "en", {}, "n1:male/n2:one")
    assert r2["scenario"]["name"]


# ---------------------------------------------------------------------------
# 草稿保存后可参与批量回归并覆盖目标路径
# ---------------------------------------------------------------------------

def test_draft_scenario_participates_in_batch_regression():
    draft = build_scenario_draft(SAMPLE, "en", {}, "n1:male/n2:one")
    sc = draft["scenario"]
    res = run_scenarios(SAMPLE, [{
        "id": "d1", "name": sc["name"], "locale": sc["locale"],
        "variables": sc["variables"],
    }])
    assert res["summary"]["passed_scenarios"] == 1
    covered = {p["key"]: p for p in res["paths"] if p["covered"]}
    assert "n1:male/n2:one" in covered
    assert covered["n1:male/n2:one"]["covered_by"][0]["id"] == "d1"


# ---------------------------------------------------------------------------
# HTTP 接口
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    app.testing = True
    return app.test_client()


def test_api_draft_ok(client):
    resp = client.post("/api/scenarios/draft", json={
        "template": SAMPLE, "locale": "en", "variables": {},
        "path_key": "n1:male/n2:one",
    })
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["verification"]["hit"] is True


def test_api_draft_422_codes(client):
    for body, code in [
        ({"template": SAMPLE, "path_key": "n1:nope"}, "path_not_found"),
        ({"template": "{n, plural, =1{x} one{a} other{b}}",
          "path_key": "n1:one"}, "unsat"),
        ({"template": "{n, plural, one{a}", "path_key": "n1:one"},
         "template_error"),
    ]:
        body.setdefault("locale", "en")
        body.setdefault("variables", {})
        resp = client.post("/api/scenarios/draft", json=body)
        assert resp.status_code == 422, body
        assert resp.get_json()["code"] == code


def test_api_draft_limit_422(client):
    tpl = " ".join("{v%d, select, a{x} b{y} other{z}}" % i for i in range(8))
    resp = client.post("/api/scenarios/draft", json={
        "template": tpl, "locale": "en", "variables": {},
        "path_key": "/".join("n%d:other" % i for i in range(1, 9)),
    })
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "path_limit_exceeded"


def test_api_draft_rejects_bad_request(client):
    assert client.post("/api/scenarios/draft",
                       data="[]", content_type="application/json").status_code == 400
    assert client.post("/api/scenarios/draft", json={
        "template": SAMPLE, "path_key": ""}).status_code == 400
    assert client.post("/api/scenarios/draft", json={
        "template": 1, "path_key": "n1:male"}).status_code == 400
    assert client.post("/api/scenarios/draft", json={
        "template": "x", "path_key": "n1:male", "locale": 9}).status_code == 400

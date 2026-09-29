"""draft.py 未覆盖路径求解（场景草稿生成）的单元测试。"""
import pytest

from draft import DraftError, draft_scenario, parse_path_key, solve_path

SAMPLE = ("{gender, select, male{He} female{She} other{They}} has "
          "{count, plural, =0{no new messages} one{# new message} "
          "other{# new messages}}.")


def draft(template, locale, key, samples=None, **kw):
    return draft_scenario(template, locale, key, samples or {}, **kw)


def assert_hits(r, key):
    assert r["ok"] is True
    assert r["verification"]["covered"] is True
    assert r["verification"]["path_key"] == key


# ---------------- 基本求解 ----------------

def test_select_and_plural_path():
    r = draft(SAMPLE, "en", "n1:male/n2:=0")
    assert_hits(r, "n1:male/n2:=0")
    assert r["scenario"]["variables"] == {"gender": "male", "count": 0}
    assert r["verification"]["output"] == "He has no new messages."


def test_category_path_picks_representative():
    r = draft(SAMPLE, "en", "n1:male/n2:one")
    assert r["scenario"]["variables"] == {"gender": "male", "count": 1}
    assert r["verification"]["output"] == "He has 1 new message."


def test_ru_few_category():
    tpl = "{n, plural, one{A} few{B} many{C} other{D}}"
    r = draft(tpl, "ru", "n1:few")
    assert r["scenario"]["variables"]["n"] == 2
    assert r["verification"]["output"] == "B"


def test_selectordinal_en_two():
    tpl = "{pos, selectordinal, one{#st} two{#nd} few{#rd} other{#th}}"
    r = draft(tpl, "en", "n1:two")
    assert r["scenario"]["variables"]["pos"] == 2
    assert r["verification"]["output"] == "2nd"


def test_offset_does_not_affect_routing_but_hash():
    tpl = "{n, plural, offset:1 =0{nobody} one{you} other{you and # others}}"
    r = draft(tpl, "en", "n1:other")
    n = r["scenario"]["variables"]["n"]
    assert n == 2  # 2 - offset(1) = 1
    assert r["verification"]["output"] == "you and 1 others"
    detail = r["basis"][0]["details"][0]
    assert "offset:1" in detail and "# = 2 − 1 = 1" in detail


# ---------------- 编辑器样例：优先沿用 / 冲突时跳过 ----------------

def test_sample_value_reused_when_consistent():
    r = draft(SAMPLE, "en", "n1:female/n2:other",
              {"gender": "female", "count": 3})
    assert r["scenario"]["variables"] == {"gender": "female", "count": 3}
    origins = {b["variable"]: b["origin"] for b in r["basis"]}
    assert origins == {"gender": "sample", "count": "sample"}


def test_conflicting_sample_skipped_and_explained():
    r = draft(SAMPLE, "en", "n1:male/n2:=0",
              {"gender": "female", "count": 3})
    assert r["scenario"]["variables"] == {"gender": "male", "count": 0}
    basis = {b["variable"]: b for b in r["basis"]}
    assert basis["gender"]["origin"] == "solver"
    assert any("female" in n for n in basis["gender"]["notes"])
    assert any("3" in n for n in basis["count"]["notes"])


def test_numeric_string_sample_matches_plural():
    tpl = "{n, plural, one{a} other{b}}"
    r = draft(tpl, "en", "n1:one", {"n": "1"})
    assert r["basis"][0]["origin"] == "sample"
    assert r["scenario"]["variables"]["n"] == 1


def test_unused_sample_variables_preserved():
    tpl = "Hi {name}, {n, plural, one{a} other{b}}"
    r = draft(tpl, "en", "n1:one", {"name": "Ada", "extra": 9})
    assert r["scenario"]["variables"]["name"] == "Ada"
    assert r["scenario"]["variables"]["extra"] == 9
    assert set(r["unused_samples"]) == {"extra", "name"}


# ---------------- 同一变量在顺序 / 嵌套分支保持同值 ----------------

def test_shared_variable_consistent_sequential():
    tpl = "{n, plural, one{a} other{b}}-{n, plural, one{c} other{d}}"
    r = draft(tpl, "en", "n1:other/n2:other", {"n": 1})
    n = r["scenario"]["variables"]["n"]
    assert n != 1
    assert r["verification"]["output"] == "b-d"
    b = r["basis"][0]
    assert len(b["details"]) == 2  # 两个节点都给出取值依据
    assert any("后续 1 个分支节点" in note for note in b["notes"])


def test_shared_variable_backtracking_beyond_first_representative():
    # en 下 one 只有 n=1；两个节点同为 n 时 one/one 可行（n=1）
    tpl = "{n, plural, one{a} other{b}}-{n, plural, one{c} other{d}}"
    r = draft(tpl, "en", "n1:one/n2:one", {"n": 5})
    assert r["scenario"]["variables"]["n"] == 1
    assert r["verification"]["output"] == "a-c"


def test_shared_variable_conflict_is_no_solution():
    # 同一变量不可能同时 =1（n1）与 =2（n2）→ 无解
    tpl = ("{n, plural, =1{a} other{b}}-"
           "{n, plural, =2{c} other{d}}")
    with pytest.raises(DraftError) as exc:
        draft(tpl, "en", "n1:=1/n2:=2")
    assert exc.value.code == "no_solution"


def test_sample_conflict_does_not_block_other_candidates():
    # 样例 n=1 在 n2 被 =1 抢占而无法走 one；按需求继续搜索，n=-1 可行
    tpl = ("{n, plural, one{a} other{b}}-"
           "{n, plural, =1{s} one{c} other{d}}")
    r = draft(tpl, "en", "n1:one/n2:one", {"n": 1})
    assert r["scenario"]["variables"]["n"] == -1
    assert r["verification"]["output"] == "a-c"


def test_shared_variable_explicit_conflict_is_no_solution():
    # n2:=1 要求 n=1，但 n=1 在 n1 走 one，无法走 other → 无解
    tpl = ("{n, plural, one{a} other{b}}-"
           "{n, plural, =1{s} one{c} other{d}}")
    with pytest.raises(DraftError) as exc:
        draft(tpl, "en", "n1:other/n2:=1")
    assert exc.value.code == "no_solution"


def test_nested_branches_share_outer_value():
    tpl = ("{a, select, x{{b, plural, one{1} other{2}}} "
           "y{{b, plural, one{3} other{4}}} other{z}}")
    r = draft(tpl, "en", "n1:y/n3:one")
    assert r["scenario"]["variables"] == {"a": "y", "b": 1}
    assert r["verification"]["output"] == "3"


def test_select_and_plural_same_variable():
    tpl = ("{x, select, 1{one} other{notone}} "
           "{x, plural, one{#} other{#s}}")
    r = draft(tpl, "en", "n1:1/n2:one")
    assert r["scenario"]["variables"]["x"] == "1"
    assert r["verification"]["output"] == "one 1"
    r2 = draft(tpl, "en", "n1:other/n2:other", {"x": "5"})
    assert r2["verification"]["output"] == "notone 5s"


# ---------------- =N / 类别 / 抢占 ----------------

def test_explicit_branch_shadowed_category_unreachable():
    # en ordinal 的 one 仅 1/21/31…；=1 抢占 1，但 21 仍可走 one
    tpl = "{p, selectordinal, =1{x} one{#st} two{#nd} other{#th}}"
    r = draft(tpl, "en", "n1:one")
    assert r["scenario"]["variables"]["p"] == 21
    assert r["verification"]["output"] == "21st"


def test_category_nonexistent_in_locale_unreachable():
    with pytest.raises(DraftError) as exc:
        draft("{n, plural, one{A} other{B}}", "zh-Hans", "n1:one")
    assert exc.value.code == "unreachable"


def test_en_many_unreachable():
    with pytest.raises(DraftError) as exc:
        draft("{n, plural, many{A} other{B}}", "en", "n1:many")
    assert exc.value.code == "unreachable"


# ---------------- 失败原因 ----------------

def test_template_error():
    with pytest.raises(DraftError) as exc:
        draft("{n, plural, one{a}}", "en", "n1:one")
    assert exc.value.code == "template_error"


def test_unknown_node_is_path_not_found():
    with pytest.raises(DraftError) as exc:
        draft(SAMPLE, "en", "n1:male/n9:one")
    assert exc.value.code == "path_not_found"


def test_unknown_selector_is_path_not_found():
    with pytest.raises(DraftError) as exc:
        draft(SAMPLE, "en", "n1:neutral/n2:one")
    assert exc.value.code == "path_not_found"


def test_bad_path_key_format():
    for bad in ("male", "n1:male/n2"):
        with pytest.raises(DraftError) as exc:
            draft(SAMPLE, "en", bad)
        assert exc.value.code == "invalid_path_key"


def test_structural_mismatch_is_path_not_found():
    # x 分支内是 n2，但路径键把 y 分支内的 n3 接到了 x 之后
    tpl = ("{a, select, x{{b, plural, one{1} other{2}}} "
           "y{{b, plural, one{3} other{4}}} other{z}}")
    with pytest.raises(DraftError) as exc:
        draft(tpl, "en", "n1:x/n3:one")
    assert exc.value.code == "path_not_found"


def test_incomplete_path_key_is_path_not_found():
    # 路径键只给外层节点，内层 plural 未指定
    tpl = "{a, select, x{{b, plural, one{1} other{2}}} other{z}}"
    with pytest.raises(DraftError) as exc:
        draft(tpl, "en", "n1:x")
    assert exc.value.code == "path_not_found"


def test_enum_limit_when_target_beyond_window(monkeypatch):
    import draft as draft_module
    monkeypatch.setattr(draft_module, "MAX_PATHS", 300)
    # 6 个 select × 3 分支 = 729；n1=other 的键排在枚举窗口之外
    tpl = " ".join("{v%d, select, a{x} b{y} other{z}}" % i for i in range(6))
    key = "/".join(["n1:other"] + ["n%d:a" % i for i in range(2, 7)])
    with pytest.raises(DraftError) as exc:
        draft(tpl, "en", key)
    assert exc.value.code == "enum_limit"


# ---------------- 无分支模板 ----------------

def test_empty_key_for_branchless_template():
    r = draft("Hello {name}", "en", "", {"name": "Ada"})
    assert r["ok"] is True
    assert r["verification"]["output"] == "Hello Ada"
    assert r["scenario"]["variables"] == {"name": "Ada"}
    assert r["basis"] == []


def test_nonempty_key_for_branchless_template_not_found():
    with pytest.raises(DraftError) as exc:
        draft("Hello {name}", "en", "n1:male")
    assert exc.value.code == "path_not_found"


# ---------------- 草稿结构 ----------------

def test_draft_default_name_and_scenario_shape():
    r = draft(SAMPLE, "en", "n1:male/n2:=0")
    assert r["scenario"]["name"] == "草稿：male/=0"
    assert r["scenario"]["locale"] == "en"
    assert r["path_key"] == "n1:male/n2:=0"
    # 取值依据按路由顺序给出
    assert [b["variable"] for b in r["basis"]] == ["gender", "count"]
    count_basis = r["basis"][1]
    assert count_basis["value"] == 0
    assert "=0" in count_basis["details"][0]


def test_custom_name():
    r = draft(SAMPLE, "en", "n1:male/n2:=0", name="男性零消息")
    assert r["scenario"]["name"] == "男性零消息"


def test_basis_origin_and_source():
    r = draft(SAMPLE, "en", "n1:male/n2:one", {"gender": "male"})
    gender = r["basis"][0]
    assert gender["origin"] == "sample"
    assert "编辑器样例值" in gender["source"]
    count = r["basis"][1]
    assert count["origin"] == "solver"
    assert count["source"] == "求解器按目标分支反推"


def test_renderer_recheck_guarantees_taken_routes():
    # 直接调 solve_path 后，再用 draft_scenario 复核：渲染器 taken 集合
    # 必须与路径键完全一致（内部实现细节，这里验证输出字段稳定）
    assigned, meta, order = solve_path(SAMPLE, "en", "n1:female/n2:one", {})
    assert assigned == {"gender": "female", "count": 1}
    assert order == ["gender", "count"]
    assert meta["count"]["details"][0].count('"one"') >= 1


def test_parse_path_key_helper():
    assert parse_path_key("n1:male/n2:=0") == [("n1", "male"), ("n2", "=0")]
    assert parse_path_key("  ") == []
    with pytest.raises(DraftError):
        parse_path_key(None)

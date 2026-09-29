"""未覆盖路径求解：为一条指定路径反推自洽的变量样例，生成可保存的场景草稿。

在场景集回归合并出的路径覆盖中，用户可挑一条尚未覆盖的路径，给出当前
模板、locale、路径键（path key，如 ``n1:male/n2:one``）以及编辑器中的
变量样例（作为优先取值）。本模块通过带回溯的约束搜索为每个分支变量
找到取值，使渲染器沿该模板路由时**恰好**经过路径键指定的分支序列。

关键约定（与 icu.py 的渲染器保持一致）：

- 同一变量在顺序片段与嵌套分支中共享同一个取值：变量一旦在某条候选
  路径上被赋值，后续节点必须按该值路由，冲突即回溯；
- plural / selectordinal 的 ``=N`` 分支按原始数值精确匹配（先于类别
  判断），类别分支由 ``plural_category`` 决定；``offset`` 仅影响 ``#``
  的输出值，不参与路由；
- 首个代表值（用户样例或最小候选值）与后续分支冲突时，继续尝试其余
  候选值，直到找到全路径自洽的组合或确认无解；
- 求解成功后用现有渲染器（``render_message`` / ``analyze``）复核：
  渲染草稿变量时实际经过的分支序列必须与路径键完全一致，否则不落
  任何场景。

失败原因（reason）：``template_error``（模板有语法错误）、
``invalid_path_key``（路径键格式非法）、``path_not_found``（路径在
当前模板结构中不存在）、``unreachable``（目标分支在该 locale 下不可
达）、``no_solution``（分支约束互相冲突，无自洽取值）、
``enum_limit``（候选搜索超过枚举上限）。
"""
from __future__ import annotations

from icu import (
    MAX_PATHS, Parser, Plural, RenderCtx, Select, analyze, iter_paths,
    path_key, plural_category, render_message, route_plural, route_select,
    _fmt_num, _str_value, _to_number,
)

# 求解器内部允许尝试的候选取值步数（远大于路径枚举上限：同一变量在多个
# 分支节点复用时，冲突候选会被反复验证，需要足够预算穷举无解空间）
MAX_SEARCH_STEPS = 20000
# 数字候选取值范围：覆盖常见 CLDR cardinal/ordinal 规则（含负整数、小数）
INT_CANDIDATES = tuple(range(0, 211)) + tuple(range(-20, 0)) + tuple(range(211, 501))
FRACTION_CANDIDATES = tuple(k + 0.5 for k in range(11))


class DraftError(Exception):
    """求解失败；code 供调用方区分失败原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 路径键解析与结构校验
# ---------------------------------------------------------------------------

def parse_path_key(key: str) -> list[tuple[str, str]]:
    """把 ``n1:male/n2:=0`` 解析为 ``[("n1", "male"), ("n2", "=0")]``。

    空字符串表示不含分支节点的模板的唯一路径（键为空序列）。
    """
    if not isinstance(key, str):
        raise DraftError("invalid_path_key", "path_key 必须是字符串")
    if not key.strip():
        return []
    routes: list[tuple[str, str]] = []
    for part in key.split("/"):
        if ":" not in part:
            raise DraftError(
                "invalid_path_key",
                f"路径键片段 '{part}' 格式无效，应为 '节点ID:选择器'")
        node_id, selector = part.split(":", 1)
        node_id, selector = node_id.strip(), selector.strip()
        if not node_id or not selector:
            raise DraftError(
                "invalid_path_key",
                f"路径键片段 '{part}' 格式无效，节点 ID 与选择器不能为空")
        routes.append((node_id, selector))
    return routes


def index_nodes(parts: list) -> dict[str, object]:
    """node_id -> 节点（Plural / Select）。"""
    out: dict[str, object] = {}

    def walk(frag: list):
        for item in frag:
            if isinstance(item, (Plural, Select)):
                out[item.node_id] = item
                for b in item.branches:
                    walk(b.message)

    walk(parts)
    return out


# ---------------------------------------------------------------------------
# 候选值
# ---------------------------------------------------------------------------

def _routes_to(node, value, selector: str, locale: str) -> bool:
    """给定取值是否会在该节点路由到 selector（直接复用渲染器路由）。"""
    if isinstance(node, Plural):
        num = _to_number(value)
        if num is None:
            return False
        branch, _ = route_plural(node, num, locale)
    else:
        branch = route_select(node, _str_value(value))
    return branch is not None and branch.selector == selector


def numeric_candidates(node: Plural, selector: str, locale: str, preferred):
    """惰性产出能让 node 路由到 selector 的数字候选。

    用户样例值排在最前；=N 分支只有精确值本身；类别分支按整数（0 起，
    与 representative_for_category 同序）、负整数、较大整数、半整数
    枚举，逐个用渲染器真实路由验证，自动跳过被 =N 抢占的取值。
    """
    if selector.startswith("="):
        try:
            target = float(selector[1:])
        except ValueError:
            return  # 结构校验已拦截非法 =N；防御性地不产出候选
        if _routes_to(node, target, selector, locale):
            yield target
        return

    seen: set[float] = set()
    pref_num = _to_number(preferred) if preferred is not None else None
    if pref_num is not None and pref_num not in seen:
        seen.add(pref_num)
        if _routes_to(node, pref_num, selector, locale):
            yield pref_num
    for n in INT_CANDIDATES + FRACTION_CANDIDATES:
        if n in seen:
            continue
        seen.add(n)
        if _routes_to(node, n, selector, locale):
            yield n


def select_candidates(node: Select, selector: str, preferred):
    """select 候选：用户样例（若命中）→ 选择器字面量。

    无显式 selector 分支时字面量会落入 other，因此 other 分支总能命中。
    """
    seen: set[str] = set()
    if preferred is not None:
        key = _str_value(preferred)
        if key not in seen:
            seen.add(key)
            if _routes_to(node, key, selector, None):
                yield key
    if selector not in seen:
        seen.add(selector)
        if _routes_to(node, selector, selector, None):
            yield selector


# ---------------------------------------------------------------------------
# 取值依据描述
# ---------------------------------------------------------------------------

def describe_constraint(name: str, value, node, selector: str,
                        locale: str) -> str:
    """描述「某节点的某分支对变量取值的要求」，作为该变量的取值依据。"""
    if isinstance(node, Select):
        return f'select 分支要求 {name} = "{_str_value(value)}"'
    if selector.startswith("="):
        return (f"{node.kind} 精确匹配分支 {selector} 要求 {name} = "
                f"{_fmt_num(float(selector[1:]))}（=N 先于复数类别判断）")
    ordinal = node.kind == "selectordinal"
    kind_label = "序数" if ordinal else "复数"
    num = _to_number(value)
    cat = plural_category(locale, num, ordinal)
    text = (f"{node.kind} 类别分支 '{selector}' 要求 {name} 在 {locale} "
            f"下的{kind_label}类别为 \"{selector}\"（{name} = "
            f"{_fmt_num(num)} 时类别为 \"{cat}\"）")
    if node.offset:
        text += (f"；offset:{_fmt_num(node.offset)} 不参与路由，"
                 f"仅使该分支内 # = {_fmt_num(num)} − "
                 f"{_fmt_num(node.offset)} = {_fmt_num(num - node.offset)}")
    return text


# ---------------------------------------------------------------------------
# 带回溯的路径求解
# ---------------------------------------------------------------------------

def solve_path(template: str, locale: str, path_key_value: str,
               samples: dict):
    """为目标路径反推变量取值。

    返回 (assigned, meta, order)：
      assigned  {变量名: 最终取值}（仅分支变量）
      meta      {变量名: {origin, preferred, details, constraints}}
                （成功路径上的完整取值依据；回溯分支的依据已随回溯移除）
      order     变量按渲染访问顺序首次出现的名字列表
    失败时抛 DraftError。
    """
    parser = Parser(template)
    parts = parser.parse()
    if parser.errors:
        msg = "；".join(e.get("message", "") for e in parser.errors)
        raise DraftError("template_error", msg or "模板存在语法错误")

    routes = parse_path_key(path_key_value)
    nodes = index_nodes(parts)

    # 结构校验：节点 ID 与选择器都必须存在于模板 AST
    for node_id, selector in routes:
        node = nodes.get(node_id)
        if node is None:
            raise DraftError(
                "path_not_found",
                f"路径键中的节点 '{node_id}' 不存在于当前模板，"
                "路径可能来自其他模板或模板已被修改")
        if not any(b.selector == selector for b in node.branches):
            raise DraftError(
                "path_not_found",
                f"变量 '{node.name}' 的分支节点（{node_id}）不存在分支 "
                f"'{selector}'，路径可能来自其他模板或模板已被修改")

    assigned: dict[str, object] = {}
    meta: dict[str, dict] = {}
    order: list[str] = []
    steps_left = [MAX_SEARCH_STEPS]
    # 新变量在目标类别下连一个候选都没有 → 与搜索顺序无关的不可达
    unreachable_nodes: list[str] = []

    def rec(frag: list, depth: int) -> bool:
        """沿目标路由深度优先搜索；返回是否找到自洽取值。"""
        # 找片段中的第一个分支节点（顺序片段，与渲染器访问顺序一致）
        idx = -1
        node = None
        for k, item in enumerate(frag):
            if isinstance(item, (Plural, Select)):
                idx, node = k, item
                break

        if node is None:
            if depth == len(routes):
                return True
            # 分支已固定，但该分支子树中没有路径键要求的后续节点
            raise DraftError(
                "path_not_found",
                f"路径键第 {depth + 1} 个节点在第 {depth} 个分支内不存在，"
                "路径与当前模板结构不一致")
        if depth >= len(routes):
            raise DraftError(
                "path_not_found",
                "路径键不完整：模板沿该分支还存在后续分支节点 "
                f"{node.node_id}（变量 '{node.name}'）")

        want_id, want_selector = routes[depth]
        if node.node_id != want_id:
            raise DraftError(
                "path_not_found",
                f"路径键第 {depth + 1} 个节点应为 {node.node_id}"
                f"（变量 '{node.name}'），键中为 {want_id}，"
                "路径与当前模板结构不一致")

        rest = frag[idx + 1:]
        next_frag = next(b for b in node.branches
                         if b.selector == want_selector).message + rest
        name = node.name

        if name in assigned:
            # 同一变量全程同值：沿用既有取值，必须恰好路由到目标分支
            value = assigned[name]
            if not _routes_to(node, value, want_selector, locale):
                return False  # 与既有取值冲突 → 上层继续尝试其他候选
            meta[name]["details"].append(
                describe_constraint(name, value, node, want_selector, locale))
            meta[name]["constraints"] += 1
            if rec(next_frag, depth + 1):
                return True
            meta[name]["constraints"] -= 1
            meta[name]["details"].pop()
            return False

        # 新变量：枚举候选并回溯。用户样例天然排在最前，与后续分支
        # 冲突时继续尝试后续候选值。
        preferred = samples.get(name)
        if isinstance(node, Plural):
            candidates = numeric_candidates(
                node, want_selector, locale, preferred)
        else:
            candidates = select_candidates(node, want_selector, preferred)

        tried = 0
        for value in candidates:
            tried += 1
            steps_left[0] -= 1
            if steps_left[0] <= 0:
                raise DraftError(
                    "no_solution",
                    f"已穷举 {MAX_SEARCH_STEPS} 个候选取值仍无法让全部"
                    "分支节点同时满足目标路由，判定为无解")
            if isinstance(node, Plural):
                from_sample = preferred is not None and \
                    _to_number(preferred) == _to_number(value)
            else:
                from_sample = preferred is not None and \
                    _str_value(preferred) == _str_value(value)
            assigned[name] = value
            order.append(name)
            meta[name] = {
                "origin": "sample" if from_sample else "solver",
                "preferred": preferred,
                "details": [describe_constraint(
                    name, value, node, want_selector, locale)],
                "constraints": 0,
            }
            if rec(next_frag, depth + 1):
                return True
            order.pop()
            del assigned[name]
            del meta[name]

        if tried == 0:
            if isinstance(node, Plural) and not want_selector.startswith("="):
                unreachable_nodes.append(node.node_id)
            return False
        return False

    found = rec(parts, 0)
    if not found:
        if unreachable_nodes:
            ids = "、".join(dict.fromkeys(unreachable_nodes))
            raise DraftError(
                "unreachable",
                f"目标复数分支在语言环境 '{locale}' 下不可达（节点 {ids}："
                "该复数类别在当前 locale 不存在，或取值全部被 =N "
                "精确分支抢占）")
        raise DraftError(
            "no_solution",
            "找不到一组自洽的变量取值命中该路径：同一变量在多个分支"
            "节点（顺序或嵌套）的路由要求互相冲突")

    return assigned, meta, order


# ---------------------------------------------------------------------------
# 取值依据整理
# ---------------------------------------------------------------------------

def _json_scalar(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value == int(value) and abs(value) < 1e15:
        return int(value)
    return value


def _literal(value) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    return _str_value(value)


def build_basis(assigned: dict, meta: dict, order: list[str]) -> list[dict]:
    """按路由首次出现顺序输出每个变量的取值依据与搜索说明。"""
    out: list[dict] = []
    for name in order:
        m = meta[name]
        notes: list[str] = []
        if m["origin"] == "sample":
            source = "编辑器样例值（可直接路由到目标分支，沿用）"
        else:
            source = "求解器按目标分支反推"
            if m["preferred"] is not None:
                notes.append(
                    f"编辑器样例值 {_literal(m['preferred'])} 不能路由到"
                    "目标分支，或与该变量后续分支节点的要求冲突，"
                    "已跳过并继续搜索其他候选值")
        if m["constraints"]:
            notes.append(
                f"同一变量在后续 {m['constraints']} 个分支节点中复用，"
                "均按此值路由（顺序与嵌套分支保持同值）")
        out.append({
            "variable": name,
            "value": _json_scalar(assigned[name]),
            "origin": m["origin"],
            "source": source,
            "details": m["details"],
            "notes": notes,
        })
    return out


# ---------------------------------------------------------------------------
# 顶层入口：求解 + 渲染器复核
# ---------------------------------------------------------------------------

def draft_scenario(template: str, locale: str, path_key_value: str,
                   samples: dict | None = None, name: str | None = None):
    """为未覆盖路径生成场景草稿，并用现有渲染器复核确实命中目标路径。

    成功返回：
      {ok, locale, path_key, scenario:{name,locale,variables},
       variables, basis:[...], unused_samples:[...],
       verification:{path_key, conditions, output, covered, renderer}}
    失败抛 DraftError（不会写入任何场景集）。
    """
    locale = (locale or "en").strip() or "en"
    samples = samples or {}
    if not isinstance(samples, dict):
        raise DraftError("invalid_samples", "variables 必须是 JSON 对象")

    routes = parse_path_key(path_key_value)

    # 枚举上限预检：模板路径总数超过 MAX_PATHS 且目标键不在枚举窗口内
    # （可能只是排在截断线之后）时给出明确原因，不进行盲搜。
    probe = Parser(template)
    probe_parts = probe.parse()
    if not probe.errors:
        shown = iter_paths(probe_parts, {}, locale, MAX_PATHS)
        if len(shown) >= MAX_PATHS:
            keys = {path_key(r) for _c, _a, r in shown}
            if path_key_value not in keys:
                raise DraftError(
                    "enum_limit",
                    f"模板可达路径超过枚举上限（{MAX_PATHS} 条），"
                    "目标路径未出现在可枚举窗口内，无法生成草稿；"
                    "可先增加覆盖场景缩小未覆盖范围后重试")

    assigned, meta, order = solve_path(
        template, locale, path_key_value, samples)

    # 草稿变量 = 编辑器样例（含未参与分支的变量）+ 求解结果；
    # 求解结果覆盖与目标路由冲突的同名样例
    variables: dict[str, object] = {}
    for k, v in samples.items():
        variables[k] = _json_scalar(v)
    for k, v in assigned.items():
        variables[k] = _json_scalar(v)

    # ---- 现有渲染器复核：analyze 查模板/变量错误，render_message 比对
    #      实际经过的分支节点集合必须与路径键完全一致 ----
    result = analyze(template, locale, variables)
    if not result.get("ok"):
        errs = "；".join(e.get("message", "")
                         for e in result.get("errors", []))
        raise DraftError(
            "no_solution",
            f"草稿未通过渲染器复核：{errs or '渲染存在错误'}")

    parser = Parser(template)
    parts = parser.parse()
    ctx = RenderCtx(variables, locale)
    output = render_message(parts, ctx)
    wanted = set(routes)
    if ctx.incomplete or ctx.taken != wanted:
        stats = result.get("stats", {})
        if stats.get("truncated") and not any(
                p.get("key") == path_key_value
                for p in result.get("paths", [])):
            raise DraftError(
                "enum_limit",
                f"模板可达路径超过枚举上限（{MAX_PATHS} 条），"
                "目标路径未参与枚举，无法生成草稿")
        raise DraftError(
            "no_solution",
            "草稿经渲染器复核未命中目标路径（实际路由与路径键不一致）")

    basis = build_basis(assigned, meta, order)
    selectors = [sel for _nid, sel in routes]
    draft_name = name or "草稿：" + "/".join(selectors)
    return {
        "ok": True,
        "locale": locale,
        "path_key": path_key_value,
        "scenario": {
            "name": draft_name,
            "locale": locale,
            "variables": variables,
        },
        "variables": variables,
        "basis": basis,
        "unused_samples": sorted(k for k in samples if k not in assigned),
        "verification": {
            "path_key": path_key_value,
            "conditions": list(ctx.conditions),
            "output": output,
            "covered": True,
            "renderer": "render_message + analyze",
        },
    }

"""未覆盖路径 → 场景草稿生成。

给定模板、locale、一条**未覆盖路径**的稳定 key（见 ``icu.path_key``）以及
编辑器中的变量样例，求解一组「确实命中该路径」的变量取值，生成可保存为
现有场景（可直接参与 ``/api/scenarios/run`` 批量回归）的草稿，并给出每个
变量取值的依据。

求解要点：
- 同一变量在顺序片段与嵌套分支中的多个节点共享同一取值（按变量名分组约束）；
- plural / selectordinal 完全按渲染器的实际路由判定：``=N`` 显式分支优先，
  类别按 locale 的 CLDR 规则匹配，offset 只影响分支内 ``#`` 的取值，
  不改变 ``=N`` / 类别的判定；
- 候选值优先采用编辑器样例；样例不满足时继续搜索候选值——首个类别代表值
  可能命中后续同变量分支的错误选择器（如 ru 下 one 的代表 1 在另一处
  plural 落入 many），因此每个候选都要通过该变量在整条路径上的**全部**
  路由约束；
- 草稿必须经现有渲染器（``render_message`` + ``analyze``）复核，确实命中
  目标路径才返回；无解 / 路径不存在 / 超过枚举上限时给出明确原因，
  不产生可保存的场景。
"""
from __future__ import annotations

from contract import derive_contracts
from icu import (
    MAX_PATHS, MAX_TEMPLATE_LEN, Parser, Plural, Select,
    RenderCtx, _fmt_num, _str_value, _to_number, analyze, iter_paths,
    path_key, plural_category, render_message, route_plural, route_select,
)

# 候选值搜索上限：analyze 的展示上限是 MAX_PATHS(300)；iter_paths 的保护上限
SEARCH_CAP = 10000

# 类别候选搜索范围（整数优先，再到半值），覆盖内置 / CLDR 常见规则
_INT_CANDIDATES = range(0, 211)
_FRAC_CANDIDATES = [i + 0.5 for i in range(0, 11)]
MAX_REJECTED_REPORT = 8


class DraftError(Exception):
    """草稿生成失败；code 供前端区分原因，status 为 HTTP 状态码。"""

    def __init__(self, code: str, message: str, status: int = 422,
                 details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_response(self) -> dict:
        body = {"ok": False, "code": self.code, "error": self.message}
        if self.details:
            body["details"] = self.details
        return body


# ---------------------------------------------------------------------------
# 路径 key 解析与结构校验
# ---------------------------------------------------------------------------

def parse_path_key(key: str) -> list[tuple[str, str]]:
    """把 'n1:male/n2:=0' 解析为 [('n1', 'male'), ('n2', '=0')]。"""
    if not isinstance(key, str):
        raise DraftError("invalid_request", "path_key 必须是字符串", 400)
    routes: list[tuple[str, str]] = []
    for seg in key.split("/"):
        if not seg:
            continue
        if ":" not in seg:
            raise DraftError("path_not_found",
                             f"路径键片段 '{seg}' 非法：应为 '节点:选择器'", 400)
        node_id, selector = seg.split(":", 1)
        routes.append((node_id, selector))
    return routes


def _walk_target_path(frag: list, target: list[tuple[str, str]], ti: int,
                      acc: list):
    """沿目标路由序列做结构化遍历，收集 (node, selector) 约束。

    模拟 iter_paths 的展开顺序（当前片段的第一个分支节点 → 选中分支 +
    顺序后续片段）。节点 id / 嵌套次序对不上即判定路径不存在。
    类别是否存在可达取值不在这里判断（交由求解器报告无解）。
    """
    idx = -1
    node = None
    for k, item in enumerate(frag):
        if isinstance(item, (Plural, Select)):
            idx = k
            node = item
            break
    if node is None:
        if ti != len(target):
            raise DraftError("path_not_found",
                             "路径键中的分支节点在模板中未出现（路径不完整或节点不匹配）")
        return
    if ti >= len(target):
        raise DraftError("path_not_found",
                         "目标路径缺少分支节点：模板沿该路径还会经过 "
                         f"{node.node_id}，但路径键到此结束")
    want_id, want_sel = target[ti]
    if node.node_id != want_id:
        raise DraftError(
            "path_not_found",
            f"路径键要求经过节点 {want_id}，但模板在该位置的节点是 {node.node_id}"
            f"（变量 '{node.name}'）")
    branch = next((b for b in node.branches if b.selector == want_sel), None)
    if branch is None:
        raise DraftError(
            "path_not_found",
            f"节点 {node.node_id}（变量 '{node.name}'）不存在分支 '{want_sel}'，"
            f"现有分支：{ '、'.join(b.selector for b in node.branches) }")
    acc.append((node, want_sel))
    _walk_target_path(branch.message + frag[idx + 1:], target, ti + 1, acc)


# ---------------------------------------------------------------------------
# 约束求解
# ---------------------------------------------------------------------------

def _node_label(node) -> str:
    kind = node.kind if isinstance(node, Plural) else "select"
    return f"{node.name} 的 {kind} 节点 {node.node_id}"


def _canon_key(value):
    """候选值去重键：1 与 1.0 视为同一值；数字字符串与数字区分（路由可能不同）。"""
    if isinstance(value, bool):
        return ("s", "true" if value else "false")
    if isinstance(value, (int, float)):
        return ("n", float(value))
    return ("s", _str_value(value))


def _routed_selector(node, value, locale: str):
    """返回给定值在节点上实际路由到的选择器；无法路由（非数字）返回 None。"""
    if isinstance(node, Select):
        return route_select(node, _str_value(value)).selector
    num = _to_number(value)
    if num is None:
        return None
    b, _ = route_plural(node, num, locale)
    return b.selector if b else None


def _candidate_stream(infos: list):
    """按优先级生成候选值：编辑器样例 → =N 值 / select 键 → 0..210 → 半值。"""
    for _node, selector in infos:
        if isinstance(_node, Select):
            yield selector
        elif selector.startswith("="):
            try:
                num = float(selector[1:])
                yield int(num) if num == int(num) else num
            except ValueError:
                pass
    yield from _INT_CANDIDATES
    yield from _FRAC_CANDIDATES


def _route_basis(node, selector: str, value, locale: str) -> tuple[str, str | None]:
    """返回 (依据说明, 实际类别)。只在值已确认命中该选择器时调用。"""
    if isinstance(node, Select):
        return (f"{_node_label(node)}：值 {_str_value(value)!r} 精确匹配 select "
                f"分支键 '{selector}'"), None
    # plural / selectordinal
    num = _to_number(value)
    cat = plural_category(locale, num, node.kind == "selectordinal")
    kind_label = "序数（selectordinal）" if node.kind == "selectordinal" else "复数（plural）"
    off_txt = f"，offset={_fmt_num(node.offset)} 仅作用于分支内 # 的取值" if node.offset else ""
    if selector.startswith("="):
        return (f"{_node_label(node)}：{kind_label} 值 {_fmt_num(num)} 命中显式分支 "
                f"{selector}（=N 按原始值优先匹配，不经过类别判定{off_txt}）"), None
    return (f"{_node_label(node)}：locale '{locale}' 下 {kind_label}值 "
            f"{_fmt_num(num)} 的类别为 '{cat}'，命中类别分支 '{selector}'"
            f"（类别按原始值判定{off_txt}）"), cat


def solve_variable(name: str, infos: list, sample, has_number_usage: bool,
                   locale: str):
    """为一个变量寻找满足其全部路径约束的取值。

    返回 (value, basis_list, rejected_list, seed_used)。
    找不到候选时抛 DraftError('unsat')。
    """
    rejected: list[dict] = []

    def record_reject(value, source, bad_node, reason):
        rejected.append({
            "value": value, "source": source,
            "node_id": bad_node.node_id if bad_node is not None else None,
            "reason": reason,
        })

    def check(value):
        """返回 None 表示通过；否则返回 (未满足的节点或 None, reason)。"""
        if has_number_usage and _to_number(value) is None:
            return None, "变量在模板中还有 number/plural 等数字用法，取值必须是数字"
        for node, selector in infos:
            actual = _routed_selector(node, value, locale)
            if actual is None:
                return node, f"{_node_label(node)} 要求数字取值，{_str_value(value)!r} 无法解析为数字"
            if actual != selector:
                return node, (f"{_node_label(node)} 要求命中 '{selector}'，"
                              f"该值实际路由到 '{actual}'")
        return None, None

    basis: list[dict] = []
    constraints = []
    for node, selector in infos:
        item = {
            "node_id": node.node_id,
            "kind": node.kind if isinstance(node, Plural) else "select",
            "variable": node.name,
            "selector": selector,
        }
        if isinstance(node, Plural):
            item["offset"] = node.offset
        constraints.append(item)

    # 1) 编辑器样例优先
    sample_is_scalar = isinstance(sample, (str, int, float, bool))
    if sample_is_scalar:
        bad_node, reason = check(sample)
        if bad_node is None and reason is None:
            for node, selector in infos:
                text, cat = _route_basis(node, selector, sample, locale)
                basis.append(_basis_entry(node, selector, text, cat))
            return sample, basis, rejected, True
        record_reject(sample, "editor_sample", bad_node, reason)

    # 2) 继续搜索候选值：每个候选都要过该变量在整条路径上的全部约束
    seen = set()
    for cand in _candidate_stream(infos):
        key = _canon_key(cand)
        if key in seen:
            continue
        seen.add(key)
        bad_node, reason = check(cand)
        if bad_node is None and reason is None:
            for node, selector in infos:
                text, cat = _route_basis(node, selector, cand, locale)
                basis.append(_basis_entry(node, selector, text, cat))
            return cand, basis, rejected, False
        if len(rejected) < MAX_REJECTED_REPORT:
            record_reject(cand, "candidate", bad_node, reason)

    unsat_details = {
        "variable": name,
        "constraints": constraints,
        "rejected": rejected,
        "sample": sample if sample_is_scalar else None,
    }
    raise DraftError(
        "unsat",
        f"找不到能让变量 '{name}' 在整条目标路径上按要求路由的取值："
        "同一变量在多个顺序/嵌套分支中的选择器要求无法同时满足"
        + ("（编辑器样例值也不满足）" if rejected and rejected[0].get("source") == "editor_sample"
           else ""),
        details=unsat_details)


def _basis_entry(node, selector: str, text: str, category: str | None) -> dict:
    entry = {
        "node_id": node.node_id,
        "kind": node.kind if isinstance(node, Plural) else "select",
        "selector": selector,
        "basis": text,
    }
    if category is not None:
        entry["category"] = category
    if isinstance(node, Plural):
        entry["offset"] = node.offset
    return entry


# ---------------------------------------------------------------------------
# 顶层：生成草稿
# ---------------------------------------------------------------------------

def build_scenario_draft(template: str, locale: str, variables: dict,
                         path_key_value: str, name: str | None = None) -> dict:
    """求解并复核一条目标路径的场景草稿。失败时抛 DraftError。"""
    template = template or ""
    locale = (locale or "en").strip() or "en"
    variables = variables or {}

    if len(template) > MAX_TEMPLATE_LEN:
        raise DraftError("template_error",
                         f"模板过长（>{MAX_TEMPLATE_LEN} 字符）")

    parser = Parser(template)
    parts = parser.parse()
    if parser.errors:
        raise DraftError(
            "template_error",
            "模板存在语法错误，无法生成草稿："
            + "；".join(e.get("message", "") for e in parser.errors[:3]),
            details={"errors": parser.errors})

    target = parse_path_key(path_key_value)
    target_key = path_key(target)

    # -- 枚举上限判定（与合并面板一致：超过 MAX_PATHS 时仅前 MAX_PATHS 条可见）--
    enumerated = iter_paths(parts, {}, locale, SEARCH_CAP)
    total = len(enumerated)
    if total >= SEARCH_CAP:
        raise DraftError(
            "path_limit_exceeded",
            f"路径枚举已达搜索上限 {SEARCH_CAP} 条，无法可靠判定目标路径，请先精简模板分支")
    visible = iter_paths(parts, {}, locale, MAX_PATHS, prefer=variables)
    visible_keys = {path_key(routes) for _c, _a, routes in visible}
    if total > MAX_PATHS and target_key not in visible_keys:
        raise DraftError(
            "path_limit_exceeded",
            f"模板共枚举到 {total} 条路径，超过路径枚举上限 {MAX_PATHS} 条，"
            "目标路径不在可枚举范围内，无法生成草稿；请先精简分支或用已有场景缩小范围")

    # -- 结构化路径校验 --
    route_infos: list = []
    _walk_target_path(parts, target, 0, route_infos)

    # -- 按变量分组约束（同变量在顺序与嵌套分支中保持同值）--
    by_var: dict[str, list] = {}
    for node, selector in route_infos:
        by_var.setdefault(node.name, []).append((node, selector))

    contracts = derive_contracts(template, parts)

    # -- 求解路由变量 --
    draft_vars: dict = {}
    variable_basis: list[dict] = []
    for vname, infos in by_var.items():
        sample = variables.get(vname)
        has_number_usage = "number" in contracts[vname].constraints if vname in contracts else False
        value, basis, rejected, seed_used = solve_variable(
            vname, infos, sample, has_number_usage, locale)
        draft_vars[vname] = value
        variable_basis.append({
            "name": vname,
            "value": value,
            "source": "editor_sample" if seed_used else "solved",
            "sample": sample if (seed_used is False and sample is not None) else None,
            "routes": basis,
            "rejected": rejected,
        })

    # -- 其余变量：编辑器样例原样保留；模板使用但缺失的变量按契约补占位默认值 --
    used_names = set(by_var)
    for cname in sorted(contracts, key=lambda n: contracts[n].first_pos):
        if cname in used_names:
            continue
        contract = contracts[cname]
        if cname in variables:
            draft_vars[cname] = variables[cname]
            variable_basis.append({
                "name": cname,
                "value": variables[cname],
                "source": "editor_sample",
                "sample": None,
                "routes": [],
                "rejected": [],
                "note": "沿用编辑器样例值；该变量不在目标路径的分支选择上，不影响路径命中",
            })
        else:
            is_number = "number" in contract.constraints
            default = 0 if is_number else ""
            draft_vars[cname] = default
            variable_basis.append({
                "name": cname,
                "value": default,
                "source": "default",
                "sample": None,
                "routes": [],
                "rejected": [],
                "note": ("该变量不在目标路径的分支选择上；按数字用法补默认值 0"
                         if is_number else
                         "该变量不在目标路径的分支选择上；补空字符串占位"),
            })
    # 编辑器提供但模板未使用的变量：原样保留，便于保存后继续编辑模板
    for extra in sorted(set(variables) - set(contracts) - used_names):
        draft_vars[extra] = variables[extra]
        variable_basis.append({
            "name": extra,
            "value": variables[extra],
            "source": "editor_sample",
            "sample": None,
            "routes": [],
            "rejected": [],
            "note": "编辑器样例变量，模板未使用，原样保留",
        })

    # 保证变量依据按模板首次出现顺序排列，额外变量排最后
    order = {n: contracts[n].first_pos for n in contracts}
    variable_basis.sort(key=lambda b: (b["name"] not in order,
                                       order.get(b["name"], 1 << 30),
                                       b["name"]))

    # -- 现有渲染器复核（一）：直接渲染，taken 必须恰好等于目标路由 --
    ctx = RenderCtx(draft_vars, locale)
    output = render_message(parts, ctx)
    if ctx.errors or ctx.incomplete or ctx.taken != set(target):
        raise DraftError(
            "verify_failed",
            "求解结果未能通过渲染器复核（未命中目标路径），不生成草稿",
            details={"target_key": target_key, "taken": sorted(ctx.taken),
                     "errors": ctx.errors})

    # -- 现有渲染器复核（二）：analyze 路径列表中该 key 必须存在且被覆盖 --
    result = analyze(template, locale, draft_vars)
    if not result.get("ok"):
        raise DraftError(
            "verify_failed",
            "草稿经 analyze 复核存在错误，不生成草稿："
            + "；".join(e.get("message", "") for e in (result.get("errors") or [])[:3]),
            details={"target_key": target_key})
    matched = next((p for p in result.get("paths", []) if p["key"] == target_key),
                   None)
    if matched is None or not matched.get("covered"):
        raise DraftError(
            "verify_failed",
            "草稿经 analyze 复核未命中目标覆盖路径，不生成草稿",
            details={"target_key": target_key,
                     "found": matched is not None})

    scenario_name = name.strip() if isinstance(name, str) and name.strip() \
        else f"覆盖路径 {target_key}"
    return {
        "ok": True,
        "target": {
            "key": target_key,
            "conditions": list(matched.get("conditions", [])),
            "total_paths": total,
        },
        "locale": locale,
        "scenario": {
            "name": scenario_name,
            "locale": locale,
            "variables": draft_vars,
        },
        "output": output,
        "variable_basis": variable_basis,
        "verification": {
            "renderer": "analyze",
            "hit": True,
            "key": target_key,
            "covered": True,
            "path_id": matched.get("id"),
            "output": matched.get("output", output),
        },
    }

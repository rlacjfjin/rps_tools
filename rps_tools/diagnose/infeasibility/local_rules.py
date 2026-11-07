"""Local CDU/utility arithmetic inherited from the input diagnosis skill.

Results are candidates until effective-model evidence proves the conflict.
"""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Any, Iterable

from .balance_hints import _collect

EPS=1e-9

@dataclass
class Finding:
    severity: str
    rule: str
    block: str
    case: str
    capacity: str
    summary: str
    evidence: list[str]
    suggestions: list[str]

def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None

def positive_upper(value: Any) -> bool:
    number = finite_number(value)
    return number is None or number > EPS

def effective_upper(*values: Any) -> float:
    finite = [number for value in values if (number := finite_number(value)) is not None]
    return min(finite) if finite else math.inf

def fmt(value: float) -> str:
    if math.isinf(value):
        return "无限"
    return f"{value:.10g}"

def direct_case_requirements(block: dict[str, Any]) -> dict[str, list[tuple[float, dict[str, Any]]]]:
    """Return provable case lower bounds from single-case capacity rows."""
    requirements: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for capacity in block.get("mass_flow_capacity", []) or []:
        lower = finite_number(capacity.get("lb"))
        associations = capacity.get("association", []) or []
        coefficients = capacity.get("association_coef", {}) or {}
        if lower is None or lower <= EPS or len(associations) != 1:
            continue
        case_code = associations[0]
        coefficient = finite_number(coefficients.get(case_code))
        if coefficient is None or coefficient <= EPS:
            continue
        requirements.setdefault(case_code, []).append((lower / coefficient, capacity))
    return requirements

def crude_purchase_index(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        crude.get("code"): crude.get("purchase") or {}
        for crude in data.get("crude", []) or []
        if crude.get("code")
    }

def purchasable_feeds(
    case: dict[str, Any],
    purchases: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    feeds: list[dict[str, Any]] = []
    for usage in case.get("crude_usage", []) or []:
        code = usage.get("crude")
        purchase = purchases.get(code, {})
        if not code:
            continue
        if not positive_upper(usage.get("ub")):
            continue
        if not positive_upper(purchase.get("ub")):
            continue
        feeds.append(
            {
                "code": code,
                "availability": effective_upper(usage.get("ub"), purchase.get("ub")),
                "property": usage.get("property") or {},
            }
        )
    return feeds

def extreme_blend(
    feeds: Iterable[dict[str, Any]],
    prop: str,
    required: float,
    minimize: bool,
) -> tuple[float | None, list[tuple[str, float, float]], list[str]]:
    """Find the best property average at a fixed flow using bounded feed supply."""
    usable: list[tuple[float, str, float]] = []
    missing: list[str] = []
    for feed in feeds:
        value = finite_number(feed["property"].get(prop))
        if value is None:
            missing.append(feed["code"])
            continue
        usable.append((value, feed["code"], feed["availability"]))

    if missing or not usable:
        return None, [], missing

    usable.sort(key=lambda item: item[0], reverse=not minimize)
    remaining = required
    weighted_sum = 0.0
    allocation: list[tuple[str, float, float]] = []
    for value, code, availability in usable:
        take = remaining if math.isinf(availability) else min(remaining, availability)
        if take <= EPS:
            continue
        allocation.append((code, take, value))
        weighted_sum += take * value
        remaining -= take
        if remaining <= EPS:
            break

    if remaining > EPS:
        return None, allocation, missing
    return weighted_sum / required, allocation, missing

def allocation_text(allocation: list[tuple[str, float, float]]) -> str:
    return " + ".join(
        f"{fmt(amount)}*{code}(性质={fmt(value)})"
        for code, amount, value in allocation
    )

def utility_index(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        utility.get("code"): utility
        for utility in data.get("utility", []) or []
        if utility.get("code")
    }

def analyze_required_utility_purchase(
    data: dict[str, Any],
    source_data: dict[str, Any] | None = None,
) -> list[Finding]:
    """Detect forced utility demand with no supply.

    For a capacity row with positive lb:

        sum(a_i * case_i) >= L

    If every associated case consumes utility U with c_i > 0, then U demand is
    at least L * min(c_i / a_i). With strict utility balance, U.purchase.ub=0 and
    no live internal source, that positive minimum demand is infeasible. A sale
    is an outlet, so an open one cannot cover the demand and does not matter.

    `source_data` is the unfiltered request when `data` has inactive cases removed:
    telling a shut-down producer from a live one needs those cases.
    """
    options = ((data.get("configuration") or {}).get("solver_options") or {})
    if options.get("ENABLE_UTILITY_CONSTRAINT") is False:
        return []
    if options.get("RELAX_UTILITY_BALANCE") is True:
        return []

    utilities = utility_index(data)
    sources = _collect(source_data or data, lambda: None)
    findings: list[Finding] = []

    for block_section in ("cduBlock", "manuBlock"):
        for block in data.get(block_section, []) or []:
            utility_by_case: dict[str, dict[str, float]] = {}
            for utility in block.get("utility", []) or []:
                material = utility.get("material")
                case = utility.get("association")
                coefficient = finite_number(utility.get("coefficient"))
                if not material or not case or coefficient is None or coefficient <= EPS:
                    continue
                utility_by_case.setdefault(case, {})[material] = coefficient

            if not utility_by_case:
                continue

            for capacity in block.get("mass_flow_capacity", []) or []:
                lower = finite_number(capacity.get("lb"))
                if lower is None or lower <= EPS:
                    continue
                associations = capacity.get("association", []) or []
                coefficients = capacity.get("association_coef", {}) or {}
                if not associations:
                    continue

                common_utilities = set(utility_by_case.get(associations[0], {}))
                for case in associations[1:]:
                    common_utilities &= set(utility_by_case.get(case, {}))

                for utility_code in sorted(common_utilities):
                    # A live internal source defeats the no-supply fingerprint, be it
                    # a negative usage or a utility_out balance term; a shut-down one
                    # does not. Unknown sources still make this only a candidate.
                    if (sources.get(utility_code) or {}).get("live_out"):
                        continue
                    utility = utilities.get(utility_code)
                    if not utility or utility.get("_status", 1) != 1 or utility.get("dumping"):
                        continue
                    purchase = utility.get("purchase") or {}
                    purchase_ub = finite_number(purchase.get("ub"))
                    if purchase_ub is None or purchase_ub > EPS:
                        continue

                    ratios: list[float] = []
                    for case in associations:
                        assoc_coef = finite_number(coefficients.get(case))
                        utility_coef = utility_by_case.get(case, {}).get(utility_code)
                        if assoc_coef is None or assoc_coef <= EPS or utility_coef is None:
                            ratios = []
                            break
                        ratios.append(utility_coef / assoc_coef)
                    if not ratios:
                        continue

                    min_demand = lower * min(ratios)
                    if min_demand <= EPS:
                        continue

                    block_code = str(block.get("code") or "")
                    capacity_code = str(capacity.get("code") or "")
                    case_text = ", ".join(str(case) for case in associations)
                    findings.append(
                        Finding(
                            severity="conflict",
                            rule="UTILITY_REQUIRED_PURCHASE_UB_ZERO",
                            block=block_code,
                            case=case_text,
                            capacity=capacity_code,
                            summary=(
                                f"{block_code}/{capacity_code} forces at least "
                                f"{fmt(min_demand)} of utility {utility_code}, but "
                                f"{utility_code}.purchase.ub=0 and no live internal source exists."
                            ),
                            evidence=[
                                f"{capacity_code}.lb = {fmt(lower)}",
                                f"associated cases: {case_text}",
                                f"minimum {utility_code} coefficient per capacity = {fmt(min(ratios))}",
                                f"minimum required {utility_code} = {fmt(lower)} * {fmt(min(ratios))} = {fmt(min_demand)}",
                                f"{utility_code}.purchase.ub = {fmt(purchase_ub)}",
                                f"no live producer of {utility_code} in utility or utility_balance",
                                "RELAX_UTILITY_BALANCE is not enabled, so utility balance is strict.",
                            ],
                            suggestions=[
                                f"Open {utility_code} purchase upper bound if this utility can be bought.",
                                f"Reduce the positive capacity lower bound on {capacity_code} if the utility cannot be supplied.",
                                "If the utility should be internally generated, add or enable the corresponding source path.",
                            ],
                        )
                    )

    return findings

def analyze_required_case(
    block: dict[str, Any],
    case: dict[str, Any],
    required: float,
    capacity: dict[str, Any],
    purchases: dict[str, dict[str, Any]],
) -> list[Finding]:
    findings: list[Finding] = []
    block_code = block.get("code", "")
    case_code = case.get("code", "")
    capacity_code = capacity.get("code", "")
    feeds = purchasable_feeds(case, purchases)

    if not feeds:
        findings.append(
            Finding(
                severity="conflict",
                rule="CDU_REQUIRED_CASE_NO_FEED",
                block=block_code,
                case=case_code,
                capacity=capacity_code,
                summary=f"{block_code}/{case_code} 被要求至少加工 {fmt(required)}，但没有可采购原油能够进入。",
                evidence=[
                    f"{capacity_code}.lb 推导出 case 最小负荷 {fmt(required)}",
                    "case允许进入且采购上限大于0的原油数量为0",
                ],
                suggestions=[
                    "检查原油采购上限和 case crude_usage 上限。",
                    "若该方案不要求运行，将对应能力下限设为0。",
                ],
            )
        )
        return findings

    total_available = sum(feed["availability"] for feed in feeds)
    if total_available + EPS < required:
        findings.append(
            Finding(
                severity="conflict",
                rule="CDU_REQUIRED_CASE_FEED_SHORTAGE",
                block=block_code,
                case=case_code,
                capacity=capacity_code,
                summary=(
                    f"{block_code}/{case_code} 最小负荷为 {fmt(required)}，"
                    f"但理论最大可用原油量只有 {fmt(total_available)}。"
                ),
                evidence=[
                    f"可用原油：{', '.join(feed['code'] for feed in feeds)}",
                    f"理论可用量上限合计：{fmt(total_available)}",
                ],
                suggestions=[
                    "增加可采购原油量或允许更多原油进入该 case。",
                    "降低对应能力下限。",
                ],
            )
        )
        return findings

    for defence in case.get("defence", []) or []:
        prop = defence.get("code") or defence.get("prop")
        if not prop:
            continue
        lower = finite_number(defence.get("lb"))
        upper = finite_number(defence.get("ub"))

        if upper is not None:
            best, allocation, missing = extreme_blend(feeds, prop, required, minimize=True)
            if best is not None and best > upper + EPS:
                evidence = [
                    f"{capacity_code}.lb 推导出 {case_code} 最小负荷 {fmt(required)}",
                    f"{prop} 上限：{fmt(upper)}",
                    f"满足最小负荷时理论最低 {prop}：{fmt(best)}",
                    f"最优低性质掺混：{allocation_text(allocation)}",
                    f"可用原油：{', '.join(feed['code'] for feed in feeds)}",
                ]
                if len(feeds) == 1:
                    evidence.append(
                        f"唯一可用原油 {feeds[0]['code']} 的 {prop}={fmt(best)}"
                    )
                findings.append(
                    Finding(
                        severity="conflict",
                        rule="CDU_REQUIRED_CASE_PROPERTY_UB",
                        block=block_code,
                        case=case_code,
                        capacity=capacity_code,
                        summary=(
                            f"{block_code}/{case_code} 被要求正负荷运行，但即使采用"
                            f"理论最优掺混，{prop}={fmt(best)} 仍超过上限 {fmt(upper)}。"
                        ),
                        evidence=evidence,
                        suggestions=[
                            f"确认原油 {prop} 数据与 case 设防是否来自同一业务口径。",
                            f"提高 {case_code} 的 {prop} 上限，或增加低 {prop} 可用原油参与掺混。",
                            "若性质和原料供应不能调整，将对应能力下限设为0。",
                        ],
                    )
                )
            elif best is None and missing:
                findings.append(
                    Finding(
                        severity="warning",
                        rule="CDU_REQUIRED_CASE_PROPERTY_MISSING",
                        block=block_code,
                        case=case_code,
                        capacity=capacity_code,
                        summary=f"{block_code}/{case_code} 的 {prop} 可行性无法静态判断，部分可用原油缺少性质。",
                        evidence=[f"缺少 {prop} 的原油：{', '.join(missing)}"],
                        suggestions=[f"补齐 crude_usage.property.{prop} 后重新检查。"],
                    )
                )

        if lower is not None:
            best, allocation, missing = extreme_blend(feeds, prop, required, minimize=False)
            if best is not None and best + EPS < lower:
                findings.append(
                    Finding(
                        severity="conflict",
                        rule="CDU_REQUIRED_CASE_PROPERTY_LB",
                        block=block_code,
                        case=case_code,
                        capacity=capacity_code,
                        summary=(
                            f"{block_code}/{case_code} 被要求正负荷运行，但即使采用"
                            f"理论最优掺混，{prop}={fmt(best)} 仍低于下限 {fmt(lower)}。"
                        ),
                        evidence=[
                            f"{capacity_code}.lb 推导出 {case_code} 最小负荷 {fmt(required)}",
                            f"{prop} 下限：{fmt(lower)}",
                            f"满足最小负荷时理论最高 {prop}：{fmt(best)}",
                            f"最优高性质掺混：{allocation_text(allocation)}",
                            f"可用原油：{', '.join(feed['code'] for feed in feeds)}",
                        ],
                        suggestions=[
                            f"确认原油 {prop} 数据与 case 设防是否来自同一业务口径。",
                            f"降低 {case_code} 的 {prop} 下限，或增加高 {prop} 可用原油参与掺混。",
                            "若性质和原料供应不能调整，将对应能力下限设为0。",
                        ],
                    )
                )

    return findings


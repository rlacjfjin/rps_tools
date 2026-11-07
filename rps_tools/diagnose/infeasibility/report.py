"""Chinese rendering of verified findings, with business metadata when available."""

import logging


SENSE_TEXT = {"E": "等式", "L": "≤ 约束", "G": "≥ 约束"}


def _fmt(value):
    return f"{value:.10g}"


def _fold(steps):
    """Merge consecutive steps that one constraint produced into a single line."""
    groups = []
    for step in steps:
        if groups and groups[-1][0] == step["row"]:
            groups[-1][1].append(step)
        else:
            groups.append((step["row"], [step]))
    return groups


def _chain_lines(finding, name, detail):
    lines = ["由下列强制赋值链推出矛盾（每一步只用一条约束和当时的变量边界，可逐步复核）。"]
    groups = _fold(finding["steps"])
    shown = groups if detail else groups[:12]
    for index, (row, steps) in enumerate(shown, 1):
        head = f"  {index}. 由 {name(row)}（{SENSE_TEXT.get(steps[0]['sense'], steps[0]['sense'])}，"
        head += f"右端 {_fmt(steps[0]['rhs'])}）推出："
        fixed = [step for step in steps if step["fixes"]]
        if len(fixed) == len(steps) and len({step["value"] for step in steps}) == 1:
            listed = "、".join(name(step["variable"]) for step in steps[:3])
            more = f" 等 {len(steps)} 项" if len(steps) > 3 else ""
            # Stating why keeps the line self-explanatory for a business reader.
            why = "（该行其余各项此时的极值合计为 0，没有反向项可以抵消）" if all(
                step.get("zero_forced") for step in steps) else ""
            lines.append(f"{head}{listed}{more}只能等于 {_fmt(steps[0]['value'])}{why}。")
            continue
        for step in steps if detail else steps[:3]:
            side = "上限" if step["side"] == "ub" else "下限"
            was = "" if step["previous"] is None else f"（原为 {_fmt(step['previous'])}）"
            verb = "只能等于" if step["fixes"] else f"的{side}被压到"
            lines.append(f"{head}{name(step['variable'])} {verb} {_fmt(step['value'])}{was}。")
            head = "     "
        if not detail and len(steps) > 3:
            lines.append(f"     其余 {len(steps) - 3} 项同理，见 JSON 证据或 --detail。")
    if not detail and len(groups) > 12:
        lines.append(f"  （其余 {len(groups) - 12} 步见 JSON 证据或 --detail。）")
    conflict = finding["conflict"]
    if conflict["kind"] == "empty_domain":
        lines.append(f"  最终：{name(conflict['variable'])} 的下限 {_fmt(conflict['lb'])} "
                     f"已超过被推出的上限 {_fmt(conflict['ub'])}，差额 {_fmt(finding['gap_lower_bound'])}。")
    else:
        end = "最小" if conflict["direction"] == "min" else "最大"
        relation = "大于" if conflict["direction"] == "min" else "小于"
        lines.append(f"  最终：{name(conflict['row'])}（{SENSE_TEXT.get(conflict['sense'], conflict['sense'])}，"
                     f"右端 {_fmt(conflict['rhs'])}）在这些边界下左端{end}只能到 {_fmt(conflict['activity'])}，"
                     f"{relation}右端，差额 {_fmt(finding['gap_lower_bound'])}。")
    evidence = finding.get("bound_evidence") or []
    if evidence:
        lines.append("参与证明的原始输入边界（未被推导，直接来自入参）：")
        for term in evidence if detail else evidence[:12]:
            side = "下限" if term["bound_type"] == "lb" else "上限"
            lines.append(f"  - {name(term['variable'])}：{side} {_fmt(term['bound'])}")
        if not detail and len(evidence) > 12:
            lines.append(f"  - 其余 {len(evidence) - 12} 项见 JSON 证据或 --detail。")
    lines.append(f"证据：{len(finding['steps'])} 步推导、{finding['source_row_count']} 条约束，"
                 "经精确有理数逐步复核；不使用求解结果或迭代违反度。")
    rows_used = finding.get("rows_used") or [row for row, _ in groups]
    if rows_used:
        listed = "、".join(name(row) for row in (rows_used if detail else rows_used[:8]))
        extra = "" if detail or len(rows_used) <= 8 else f" 等 {len(rows_used)} 条"
        lines.append(f"涉及的约束：{listed}{extra}。请沿这条链和上面的原始边界核对输入。")
    return lines


def format_chinese(report, *, detail=False):
    if report.get("failure_analysis"):
        base = dict(report)
        analysis = base.pop("failure_analysis")
        prior = format_chinese(base, detail=detail) + "\n" if base.get("status") else ""
        return prior + _failure_message(analysis)
    if not detail and not report.get('proven_infeasible') and report.get('input_hints'):
        return '\n'.join(hint['message'] for hint in report['input_hints'])
    labels = report.get("labels", {})

    def name(code):
        label = labels.get(code, {}).get("name")
        return f"{label}（{code}）" if label and label != code else code

    titles = {"proven_infeasible": "检测结论：已证明模型约束存在冲突。",
              "no_conflict_found": "检测结论：当前检查范围内未发现可证明的冲突。",
              "inconclusive": "检测结论：本次检测未能给出确定结论。",
              "invalid_model": "检测结论：模型数据或引用有误，无法完成冲突检查。"}
    lines = [titles[report["status"]]]
    hint_title = ('业务提示（依据原始入参，与下方的数学证明相互独立）：' if report.get('proven_infeasible')
                  else '原始入参线索（尚未完成完整模型证明）：')
    for hint in report.get('input_hints', []):
        lines.extend(['', hint_title + hint['message'], hint['limitation']])
    for i, finding in enumerate(report.get("findings", []), 1):
        lines.append("")
        if finding["rule_id"] == "BOUND_CONFLICT":
            lines.append(f"冲突 {i}：{name(finding['variable'])}的下限 {_fmt(finding['lb'])} 大于上限 {_fmt(finding['ub'])}。")
            continue
        if finding["rule_id"] == "FORCING_CHAIN_CONFLICT":
            chain = _chain_lines(finding, name, detail)
            lines.append(f"冲突 {i}：" + chain[0])
            lines.extend(chain[1:])
            continue
        terms = finding["bound_evidence"]
        positive = [t for t in terms if t["coefficient"] > 0 and t["bound"] != 0]
        negative = [t for t in terms if t["coefficient"] < 0 and t["bound"] != 0]
        purchase_terms = [t for t in negative if labels.get(t["variable"], {}).get("kind") == "crude_purchase"]
        is_supply = (len(positive) == 1 and positive[0]["coefficient"] == 1
                     and purchase_terms and len(purchase_terms) == len(negative)
                     and all(t["coefficient"] == -1 for t in negative)
                     and finding["combined_constraint"]["rhs"] == 0)
        if is_supply:
            required = positive[0]
            upper = sum(t["bound"] for t in negative)
            lines.append(f"冲突 {i}：原油供应上限与{name(required['variable'])}下限冲突。")
            lines.append(f"至少需要 {_fmt(required['bound'])}，原油供应最多 {_fmt(upper)}，缺口至少 {_fmt(finding['gap_lower_bound'])}（沿用模型数量单位）。")
            lines.append("参与证明的原油采购上限：")
            lines.extend(f"  - {name(t['variable'])}：{_fmt(t['bound'])}" for t in negative)
            zeros = sum(t["coefficient"] < 0 and t["bound"] == 0 and labels.get(t["variable"], {}).get("kind") == "crude_purchase" for t in terms)
            if zeros:
                lines.append(f"  - 另有 {zeros} 项原油采购上限为 0。")
            lines.append("请检查总量下限或原油供应上限；解除这组冲突后仍需检查其他约束。")
        else:
            lines.append(f"冲突 {i}：相关约束与变量边界不能同时满足。")
            lines.append(f"合并约束要求左侧不超过 {_fmt(finding['combined_constraint']['rhs'])}；由变量边界推得左侧至少为 {_fmt(finding['minimum_lhs_from_bounds'])}，差额为 {_fmt(finding['gap_lower_bound'])}。")
            if terms:
                lines.append("参与证明的边界：")
                for term in terms[:(len(terms) if detail else 12)]:
                    side = "下限" if term["bound_type"] == "lb" else "上限"
                    lines.append(f"  - {name(term['variable'])}：{side} {_fmt(term['bound'])}，合并系数 {_fmt(term['coefficient'])}")
                if not detail and len(terms) > 12:
                    lines.append(f"  - 其余 {len(terms) - 12} 项见 JSON 证据或 --detail。")
        codes = list(finding["row_multipliers"])
        lines.append(f"证据：{len(codes)} 条约束的线性组合，经精确数值核验；不使用求解结果或迭代违反度。")
        named = [c for c in codes if labels.get(c, {}).get("kind") in ("custom_constraint", "capacity", "balance")]
        shown = codes if detail else (named[:8] or codes[:8])
        lines.append("关联约束：" + "、".join(name(c) for c in shown))
        if detail:
            lines.append("组合权重：" + "，".join(f"{c} × {w}" for c, w in finding["row_multipliers"].items()))
    coverage = report.get("coverage", {})
    ignored = coverage.get("ignored_rows", {})
    if coverage:
        lines.extend(["", f"检查范围：{coverage.get('linear_hard_rows', 0)} 条线性硬约束、{coverage.get('variables', 0)} 个变量的边界。"])
        skipped = ignored.get("nonlinear_or_unexpanded", 0) + ignored.get("unsupported_type", 0)
        if skipped:
            lines.append(f"有 {skipped} 条非线性或暂不支持的约束未纳入；本模块不证明非线性模型可行。")
    if report["status"] != "proven_infeasible":
        lines.append("未发现冲突不等于模型可行；整型限制和一般非线性约束需要进一步检查。")
    shown_warnings = report.get("warnings", []) if detail else (
        report.get("warnings", [])[:3] if report["status"] in ("invalid_model", "inconclusive") else [])
    for warning in shown_warnings:
        lines.append("说明：" + warning.get("code", "") + " " + warning["reason"])
    return "\n".join(lines)


def _failure_message(analysis):
    lines = ["失败约束诊断（仅提示，不改变求解状态）："]
    for finding in analysis["confirmed_conflicts"]:
        if finding.get("rule_id") == "BOUND_CONFLICT":
            lines.append(f"【确定冲突】{finding['name']}（{finding['variable']}）："
                         f"下限 {_fmt(finding['lb'])} 大于上限 {_fmt(finding['ub'])}。")
            lines.append("核对建议：核对该变量的上下限及覆盖它们的 LB/UB/FX 设置。")
            for item in finding.get("related_inputs", [])[:3]:
                lines.append("  关联入参：" + item["input_path"])
        else:
            lines.append("【确定冲突】已有精确证明，约束链和入参见原诊断报告。")
    evidence = analysis["evidence"]
    if analysis["suspected_constraints"]:
        location = "中间观测，非终止点" if evidence.get("observation_kind") == "intermediate" else "最后观测"
        lines.append(f"残差来源：本次求解 {evidence.get('profile') or '原生结果'}，"
                     f"迭代 {evidence.get('iteration', '未知')}（{location}）；仅代表该观测点，不能证明模型不可行。")
    for finding in analysis["suspected_constraints"][:3]:
        lines.append(f"【疑点】{finding['name']}（{finding['code']}）："
                     f"观测点约束违反量 {_fmt(finding['violation'])}。")
        for item in finding["related_inputs"][:6]:
            settings = item.get("input_settings") or {}
            bounds = "，".join(f"{side}={settings[side]}" for side in ("lb", "ub") if settings.get(side) is not None)
            lines.append("  关联入参：" + item["input_path"] + (f"（输入 {bounds}）" if bounds else ""))
        lines.append("核对建议：" + finding["recommendation"])
    if not analysis["suspected_constraints"] and not analysis["confirmed_conflicts"]:
        lines.append("现有输出没有可定位的失败约束证据，无法据此确定冲突原因。")
    if evidence.get("truncated") or evidence.get("timed_out"):
        lines.append("残差日志仅作有限读取，提示可能不完整。")
    if analysis["suspected_constraints"]:
        lines.append("违反量不是应放宽的幅度；失败迭代值不能作为业务结果，调整入参后仍需重新求解验证。")
    return "\n".join(lines)


def attach_failure_diagnostics(proto, result, input_data=None):
    """Append hints after status adjudication. Never solve, restore a point or gate status.

    Success, feasible checkpoints and unbounded witnesses return before reading
    the model. Diagnostics are best-effort and cannot fail the service request.
    """
    try:
        if result.get("solver_status_code") in (1, 3, 4, 7, 9, 10, 25, 26):
            return
        diagnostics = result.get("solve_diagnostics") or {}
        if (result.get("solution_available") is True or diagnostics.get("solution_source")
                or not diagnostics.get("failed")):
            return
        from .fast import check_bounds
        from .snapshot import failure_input_context

        evidence = dict(getattr(proto, "_failure_residuals", None) or {"source": "unavailable", "rows": []})
        old_report = result.get("infeasibility_diagnostics") or {}
        confirmed = [dict(f) for f in old_report.get("findings", []) if f.get("confidence") == "proven"]
        bounds_report = check_bounds(proto)
        if not confirmed:
            confirmed = [dict(f) for f in bounds_report.get("findings", [])]
        suspected = [dict(row, confidence="suspected") for row in evidence.pop("rows", [])[:10]]
        codes = [f["code"] for f in suspected] + [f["variable"] for f in confirmed if "variable" in f]
        context = failure_input_context(proto, codes, input_data) if codes else {}
        for finding in confirmed:
            finding.update(context.get(finding.get("variable"), {}))
        for finding in suspected:
            finding.update(context[finding["code"]])
            property_related = any(item.get("kind") in ("property", "property_balance", "property_recursion", "implicit_property")
                                   for item in finding["related_inputs"])
            finding["recommendation"] = (
                "核对该物料的组成范围、关联装置的分离/递推系数及方案开关，确认这些设置能否同时满足；"
                "不能仅凭残差认定范围错误或推定应调整到哪个数值。" if property_related else
                "核对关联入参的上下限、供需/能力设置及约束系数；该约束也可能受其他约束牵制，不能直接认定它是根因。")
        analysis = dict(confirmed_conflicts=confirmed, suspected_constraints=suspected, evidence=evidence,
                        bounds_check_complete=bounds_report.get("status") != "inconclusive",
                        extra_solver_calls=0)
        message = _failure_message(analysis)
        prior = result.get("diagnostic_message")
        # Idempotent for repeated serialization of the same response.
        if old_report.get("failure_analysis") == analysis:
            return
        message = (prior + "\n" if prior else "") + message
        report = dict(old_report, failure_analysis=analysis, message=message)
        result.update(infeasibility_diagnostics=report,
                      diagnostic_message=message)
        logging.getLogger("rps").warning(message)
    except Exception:
        logging.getLogger("rps").debug("失败约束提示未完成，保留原求解结果", exc_info=True)

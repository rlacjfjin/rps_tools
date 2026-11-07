"""Raw-input hints: exact local arithmetic is not a full effective-model proof."""
import copy
from dataclasses import asdict
import math
import time

from .input_hints import crude_supply_hints
from .balance_hints import balance_deadend_hints
from .route_hints import blocked_product_hints
from .shutdown_hints import starved_forced_unit_hints
from . import local_rules


def _enabled(item):
    return item.get('status', item.get('_status', 1)) == 1


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def _bound_hints(data):
    found = []
    def walk(item, path):
        if isinstance(item, dict):
            if not _enabled(item):
                return
            lb, ub = _number(item.get('lb')), _number(item.get('ub'))
            if lb is not None and ub is not None and lb > ub:
                found.append(dict(rule_id='RAW_BOUND_PAIR', confidence='candidate', input_path=path,
                    lb=lb, ub=ub, message=f'{path} 下限 {lb:g} 大于上限 {ub:g}，请核对其是否生效及是否有覆盖设置。'))
            for key, value in item.items():
                if isinstance(value, (dict, list)):
                    walk(value, f'{path}.{key}' if path else key)
        elif isinstance(item, list):
            for i, value in enumerate(item):
                walk(value, f'{path}[{i}]')
    walk(data, '')
    return found


def _aggregate_route_hints(data):
    """Generalize the old customer-specific total-load/closed-route fingerprint."""
    caps = {}
    for table in ('manuBlock', 'cduBlock'):
        for block in data.get(table, []) or []:
            if _enabled(block):
                for cap in block.get('mass_flow_capacity', []) or []:
                    caps['CCAP'+cap['code']] = block['code']
    totals = {v['code'] for v in data.get('customVariable', []) or []
              if _enabled(v) and (_number(v.get('lb')) or 0) > 0}
    findings = []
    for row in data.get('customConstraint', []) or []:
        if not _enabled(row):
            continue
        # Only exact symbol tokens, not substrings of similarly named codes.
        tokens = set(str(row.get('expr', '')).replace(' ', '').split('+'))
        tokens = {factor for token in tokens for factor in token.split('*')}
        linked_totals, units = totals & tokens, {caps[t] for t in tokens & caps.keys()}
        if not linked_totals or not units:
            continue
        for splitter in data.get('spliterBlock', []) or []:
            if not _enabled(splitter):
                continue
            for index, route in enumerate(splitter.get('output_block', []) or []):
                if route.get('code') in units and _number(route.get('ratio_ub')) == 0:
                    path=f"spliterBlock[code={splitter['code']}].output_block[{index}]"
                    findings.append(dict(rule_id='RAW_AGGREGATE_ROUTE_CLOSED', confidence='candidate',
                        input_path=path, related_inputs=[path, f"customConstraint[code={row['code']}]"],
                        message=f"总量下限 {', '.join(sorted(linked_totals))} 关联装置 {route['code']}，但该装置的一条进料路径比例上限为 0。",
                        limitation='仅关联风险；未证明其他原料或路径不足，也未据表达式推算必需负荷。'))
    return findings


def _unfinished(scan):
    outcome = {'skipped': '已跳过', 'timed_out': '超出时间预算', 'data_error': '数据不完整'}[scan['outcome']]
    detail = f"：{scan['reason']}" if scan.get('reason') else ''
    return f"{scan['scan']} 未完整运行（{outcome}{detail}），没有命中不代表这一项没有问题。"


def analyze_input(data, time_limit=10):
    if data.get('solver_status_code') is not None or 'constarint' in data or ('bounds' in data and 'rows' in data):
        raise ValueError('需要原始 input，不能把业务 output 或模型快照当作入参')
    started = time.perf_counter()
    findings, scans, warnings = _bound_hints(data), [], []
    for name, scan in (('crude_supply', crude_supply_hints), ('blocked_product', blocked_product_hints),
                       ('balance_deadend', balance_deadend_hints), ('shutdown_starves_forced_unit', starved_forced_unit_hints)):
        remaining = time_limit - (time.perf_counter()-started)
        if remaining <= 0:
            scans.append(dict(scan=name, ran=False, hits=0, outcome='timed_out', reason='budget_exhausted'))
            warnings.append(_unfinished(scans[-1]))
            continue
        try:
            trace = {}
            hits = scan(data, time_limit=min(2, remaining), trace=trace)
            findings.extend(hits)
            scans.append(dict(trace, scan=name, ran=trace['outcome'] == 'completed', hits=len(hits)))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            scans.append(dict(scan=name, ran=False, hits=0, outcome='data_error', reason=f'{type(exc).__name__}: {exc}'))
        if not scans[-1]['ran']:
            warnings.append(_unfinished(scans[-1]))
    if any(data.get(k) for k in ('plant','period','storage')):
        warnings.append('CDU/公用工程局部规则未覆盖工厂、跨期或库存耦合，已跳过。')
    else:
        # Avoid inactive blocks/cases becoming false positive local conflicts.
        cleaned = copy.deepcopy(data)
        for table in ('cduBlock','manuBlock'):
            cleaned[table] = [b for b in cleaned.get(table, []) or [] if _enabled(b)]
            for b in cleaned[table]:
                b['cases'] = [c for c in b.get('cases', []) or [] if _enabled(c)]
                available = {c['code'] for c in b['cases']}
                b['mass_flow_capacity'] = [c for c in b.get('mass_flow_capacity', []) or []
                    if _enabled(c) and set(c.get('association') or []) <= available]
        try:
            local = []
            purchases = local_rules.crude_purchase_index(cleaned)
            for block in cleaned['cduBlock']:
                cases = {c['code']: c for c in block['cases']}
                for case, reqs in local_rules.direct_case_requirements(block).items():
                    if case in cases:
                        load, cap = max(reqs, key=lambda r:r[0])
                        local.extend(local_rules.analyze_required_case(block,cases[case],load,cap,purchases))
            local.extend(local_rules.analyze_required_utility_purchase(cleaned, data))
            for f in local:
                raw = asdict(f)
                raw.update(rule_id=raw.pop('rule'), confidence='candidate', message=raw.pop('summary'))
                raw['severity'] = 'warning'
                raw['limitation'] = '局部入参推导；须核对生效边界、配置、替代来源和模型变换，不作为整模不可行证明。'
                table = 'cduBlock' if any(b['code']==f.block for b in cleaned['cduBlock']) else 'manuBlock'
                raw['related_inputs'] = [f'{table}[code={f.block}].mass_flow_capacity[code={f.capacity}]']
                findings.append(raw)
            findings.extend(_aggregate_route_hints(cleaned))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            warnings.append(f'局部规则未完成：{exc}')
    return dict(findings=findings, scans=scans, warnings=warnings,
                limitation='没有发现线索不等于整模可行；原始入参线索不改变求解状态。')

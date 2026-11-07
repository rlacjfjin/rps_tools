"""Budgeted raw-input clues, run only after failure; never a model certificate."""
from fractions import Fraction
import math
import time


def crude_supply_hints(data, time_limit=0.05, trace=None):
    """Recognize a simple total-CDU-load relation without constructing the model.

    This intentionally remains a candidate: raw JSON does not establish all
    effective bounds, model transformations or alternative supply paths.
    Unsupported syntax, inventory, transfers and multi-period data are skipped.
    `trace`, when given, says whether the scan really finished: an empty result
    alone cannot tell "checked, nothing wrong" from "never got to check".
    """
    deadline = time.perf_counter() + time_limit
    trace = {} if trace is None else trace
    trace.update(outcome='completed', timed_out=False)

    def skip(reason):
        trace.update(outcome='skipped', reason=reason)
        return []

    def check():
        if time.perf_counter() >= deadline:
            raise TimeoutError

    def number(value):
        if value is None or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError
        return Fraction(str(value))

    try:
        if any(data.get(k) for k in ('plant', 'period', 'storage')):
            return skip('multi_plant_period_storage')
        crudes = data.get('crude') or []
        blocks = data.get('cduBlock') or []
        if not crudes or not blocks:
            return []
        supply = Fraction(0)
        purchases = []
        seen = set()
        for crude in crudes:
            check()
            code = crude['code']
            if code in seen or any(crude.get(k) for k in ('stock', 'transfer', 'input')):
                return skip('duplicate_crude_or_extra_source')
            seen.add(code)
            purchase = crude.get('purchase') or {}
            limit = purchase.get('ub')
            if limit is None or limit != limit:
                # An absent ceiling is unlimited supply: no total can exceed it,
                # so the scan is finished rather than unable to run.
                trace['reason'] = 'purchase_unbounded'
                return []
            upper = number(limit)
            if upper < 0:
                return skip('negative_purchase_ub')
            supply += upper
            purchases.append({'code': code, 'upper': float(upper)})
        # Only capacities that count every case exactly once can represent
        # total crude load. Never sum overlapping capacities of one unit.
        capacities = {}
        for i, block in enumerate(blocks):
            check()
            if block.get('_status', 1) != 1:
                return skip('disabled_cdu')
            cases = {c['code'] for c in block.get('cases', [])}
            if not cases:
                return skip('cdu_without_cases')
            for cap in block.get('mass_flow_capacity', []):
                check()
                assoc = cap.get('association') or []
                if len(assoc) != len(cases) or set(assoc) != cases:
                    continue
                if any(number((cap.get('association_coef') or {}).get(c)) != 1 for c in assoc):
                    continue
                code = 'CCAP' + cap['code']
                if code in capacities:
                    return skip('duplicate_capacity')
                capacities[code] = i
        variables = {v['code']: v for v in data.get('customVariable', []) if v.get('status', 1) == 1}
        known = set(variables) | set(capacities)
        for row in data.get('customConstraint', []):
            check()
            if row.get('status', 1) != 1 or row.get('con_type') != 1 or row.get('order') != 1:
                continue
            coefficients = {}
            try:
                # A deliberately small grammar: sums of numeric factors and
                # one known symbol. No eval, nonlinear terms or implicit minus.
                for term in row.get('expr', '').replace(' ', '').split('+'):
                    check()
                    coefficient = Fraction(1)
                    symbols = []
                    for factor in term.split('*'):
                        if factor in known:
                            symbols.append(factor)
                        else:
                            coefficient *= number(factor)
                    if len(symbols) != 1:
                        raise ValueError
                    symbol = symbols[0]
                    coefficients[symbol] = coefficients.get(symbol, 0) + coefficient
                coefficients = {k: v for k, v in coefficients.items() if v}
                totals = [k for k in coefficients if k in variables]
                loads = [k for k in coefficients if k in capacities]
                if len(totals) != 1 or len(loads) != len(blocks):
                    continue
                if {capacities[k] for k in loads} != set(range(len(blocks))):
                    continue
                if any(coefficients[k] != -1 for k in loads):
                    continue
                total = totals[0]
                factor = coefficients[total]
                if factor <= 0:
                    continue
                lower = number(variables[total].get('lb'))
                rhs = number(row.get('RHS'))
                required = factor * lower - rhs
                if required <= supply + Fraction('0.000001'):
                    continue
                message = (
                    f"按自定义约束 {row['code']}，"
                    f"至少需要加工 {float(required):.10g}，原油采购上限合计 {float(supply):.10g}，"
                    f"相差 {float(required - supply):.10g}（沿用模型数量单位）。"
                    f"请核对 {total} 的下限或原油采购上限。"
                )
                return [{'rule_id': 'RAW_CRUDE_SUPPLY_SHORTFALL', 'confidence': 'candidate',
                         'message': message, 'total_variable': total, 'constraint': row['code'],
                         'required': float(required), 'supply_upper': float(supply),
                         'gap': float(required - supply), 'exact_gap': str(required - supply),
                         'total_coefficient': str(factor), 'rhs': str(rhs),
                         'purchases': purchases,
                         'limitation': '依据原始入参的供需关系，尚未核验全部生效边界及替代供应路径；不据此更改求解状态。'}]
            except (ValueError, TypeError, ZeroDivisionError, OverflowError):
                continue
    except TimeoutError:
        trace.update(outcome='timed_out', timed_out=True)
        return []
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        trace.update(outcome='data_error', reason=f'{type(exc).__name__}: {exc}')
        return []
    return []

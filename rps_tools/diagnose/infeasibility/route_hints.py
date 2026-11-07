"""Conservative, budgeted clues for forced coproducts with blocked outlets."""
from fractions import Fraction
import math
import time


def blocked_product_hints(data, time_limit=0.05, trace=None):
    """`trace`, when given, says whether the scan really finished; see crude_supply_hints."""
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

    def absent(value):
        return value is None or (isinstance(value, (int, float)) and math.isnan(value))

    try:
        if any(data.get(k) for k in ('plant', 'period', 'storage')):
            return skip('multi_plant_period_storage')
        materials = {}
        units = {}
        consumers = {}
        for material in data.get('materialList', []):
            check()
            if material['code'] in materials:
                return skip('duplicate_material_code')
            materials[material['code']] = material
        for kind in ('manuBlock', 'spliterBlock', 'cduBlock', 'blenderBlock', 'mixerBlock', 'customBlock', 'processBlock'):
            for unit in data.get(kind, []):
                check()
                if unit['code'] in units:
                    return skip('duplicate_unit_code')
                units[unit['code']] = (kind, unit)
                # Include actual recipe inputs even if routing metadata is stale.
                inputs = {m['code'] for m in unit.get('input_material', [])}
                for case in unit.get('cases', []):
                    check()
                    inputs.update(r['material'] for r in case.get('material_ratio', []) if r.get('type') == 'in')
                for code in inputs:
                    consumers.setdefault(code, set()).add(unit['code'])

        def blocked(code, stack):
            check()
            if code in stack or len(stack) >= 20:
                return None  # Cycles and deep/unknown routes are not proven blocked.
            material = materials.get(code)
            if not material or material.get('_status', 1) != 1:
                return None
            if any(material.get(k) for k in ('stock', 'transfer')):
                return None
            sell = material.get('sell')
            # parse_Material normalizes a missing sale to a dict whose price
            # and bounds are numeric NaN. No sale variable is created then.
            if sell and 'price' in sell and absent(sell['price']):
                sell = None
            if sell and (absent(sell.get('ub')) or number(sell['ub']) != 0):
                return None
            destinations = set(material.get('output') or []) | consumers.get(code, set())
            reasons = []
            for destination in sorted(destinations):
                check()
                if destination not in units:
                    return None
                kind, unit = units[destination]
                if unit.get('_status', 1) == 0:
                    reasons.append({'unit': destination, 'material': code, 'reason': 'disabled'})
                    continue
                if unit.get('_status', 1) != 1:
                    return None
                if kind == 'spliterBlock':
                    outputs = unit.get('output_material') or []
                    if not outputs:
                        return None
                    for output in outputs:
                        downstream = blocked(output['code'], stack | {code})
                        if downstream is None:
                            return None
                        reasons.extend(downstream)
                elif kind == 'manuBlock':
                    cases = [c for c in unit.get('cases', []) if c.get('status', 1) == 1]
                    if not cases:
                        return None
                    for case in cases:
                        check()
                        # Property-dependent recipe adjustments require full analysis.
                        if any(r.get('material') == code for r in case.get('db', [])):
                            return None
                        for ratio in case.get('material_ratio', []):
                            if ratio.get('material') == code and ratio.get('type') == 'in' and number(ratio.get('ratio')) != 0:
                                return None
                    reasons.append({'unit': destination, 'material': code, 'reason': 'missing_recipe_consumption',
                                    'cases': [c['code'] for c in cases]})
                else:
                    return None
            return reasons or [{'material': code, 'reason': 'no_outlet'}]

        for unit in data.get('manuBlock', []):
            check()
            if unit.get('_status', 1) != 1:
                continue
            cases = {c['code']: c for c in unit.get('cases', []) if c.get('status', 1) == 1}
            for capacity in unit.get('mass_flow_capacity', []):
                check()
                if capacity.get('lb') is None or number(capacity['lb']) <= 0:
                    continue
                assoc = capacity.get('association') or []
                if not assoc or len(set(assoc)) != len(assoc) or any(c not in cases for c in assoc):
                    continue
                product_sets = []
                for code in assoc:
                    check()
                    case = cases[code]
                    adjusted = {r.get('material') for r in case.get('db', [])}
                    product_sets.append({r['material'] for r in case.get('material_ratio', [])
                                         if r.get('type') == 'out' and r['material'] not in adjusted})
                products = set.intersection(*product_sets)
                for product in sorted(products):
                    check()
                    reasons = blocked(product, set())
                    if reasons is None:
                        continue
                    yields = []
                    for code in assoc:
                        check()
                        coefficient = number((capacity.get('association_coef') or {}).get(code))
                        if coefficient <= 0:
                            break
                        net = Fraction(0)
                        for ratio in cases[code].get('material_ratio', []):
                            if ratio['material'] == product:
                                if ratio.get('type') not in ('in', 'out'):
                                    raise ValueError
                                net += number(ratio['ratio']) * (1 if ratio['type'] == 'out' else -1)
                        if net <= 0:
                            break
                        yields.append(net / coefficient)
                    if len(yields) != len(assoc):
                        continue
                    minimum = number(capacity['lb']) * min(yields)
                    missing = [r for r in reasons if r['reason'] == 'missing_recipe_consumption']
                    disabled = sorted({r['unit'] for r in reasons if r['reason'] == 'disabled'})
                    explanation = []
                    if disabled:
                        explanation.append('接收装置 ' + '、'.join(disabled) + ' 已停用')
                    explanation.extend(f"{r['unit']} 的 {'、'.join(r['cases'])} 方案未配置 {r['material']} 的消耗" for r in missing)
                    explanation = list(dict.fromkeys(explanation))
                    message = (f"{unit.get('name') or unit['code']}（{unit['code']}）的能力下限为 {float(capacity['lb']):.10g}，"
                               f"要求产出 {product}，但当前产品去向受阻："
                               + ('；'.join(explanation) or '未配置有效接收或销售出口')
                               + f"。请核对接收方案进料配方和装置开停状态，或调整 CCAP{capacity['code']} 下限。")
                    return [{'rule_id': 'RAW_BLOCKED_COPRODUCT', 'confidence': 'candidate', 'message': message,
                             'unit': unit['code'], 'capacity': 'CCAP' + capacity['code'], 'product': product,
                             'minimum_product': float(minimum), 'exact_minimum_product': str(minimum),
                             'routes': reasons, 'capacity_lower': capacity['lb'],
                             'limitation': '依据原始方案与物料去向分析；未覆盖全部生效约束和动态配方，不据此改写求解状态。'}]
    except TimeoutError:
        trace.update(outcome='timed_out', timed_out=True)
        return []
    except (KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
        trace.update(outcome='data_error', reason=f'{type(exc).__name__}: {exc}')
        return []
    return []

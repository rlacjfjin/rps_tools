"""Budgeted raw-input clue: a shut-down unit starves a unit that must run.

Fourth scan of the failure-only clue layer, next to `input_hints` (crude
supply), `route_hints` (a forced product with no outlet) and `balance_hints`
(a utility balance that can only close at zero). Like those it reads the
request JSON only, costs milliseconds, needs no model, and still speaks when
the full certificate search runs out of budget.

This one covers the input side of `route_hints`: a material whose every
producing case is pinned to zero by a capacity ceiling (or by a disabled unit
or case) and that cannot be bought, while some other unit carries a positive
capacity lower bound and every one of that capacity's still-usable cases needs
the material as feed. The capacity floor forces at least one of those cases
above zero, the recipe then demands a strictly positive amount of a material
whose supply is identically zero, and the model is infeasible.

Why the clue layer has to say this at all: `DELETE_ZERO_CASE`
(`model/block/db_block.py`) keeps a unit whose cases are all zero-capacity out
of the material balance entirely, so by the time the model exists the shut-down
unit is no longer a term in the starved material's balance row. The
mathematical certificate can then prove the conflict but can only name the
consumer, never the unit the planner actually switched off.

These stay candidates: raw JSON does not establish effective bounds, model
transformations or alternative routes. They never change the solver status.
"""
import math
import time

from .balance_hints import _forced_zero_cases

# Units whose material flow is a recipe over `cases`. Everything else routes
# material through columns/expressions this scan does not interpret, so such a
# unit counts as a source that cannot be proven dead.
CASE_TABLES = ('manuBlock', 'cduBlock')
BLOCK_TABLES = CASE_TABLES + ('processBlock', 'blenderBlock', 'mixerBlock',
                              'multi_mixerBlock', 'spliterBlock', 'customBlock')
MAX_HINTS = 3


def _absent(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def _number(value):
    """Finite float, or None for absent/non-numeric/NaN input."""
    if _absent(value) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _enabled(item, key='status'):
    return isinstance(item, dict) and item.get(key, 1) == 1


def _tradeable(entry):
    """True when the request still configures a real purchase/sale variable.

    parse_Material normalizes a missing trade to a dict whose price and bounds
    are NaN, so `None`, an absent price and an explicit 0 ceiling all mean no
    usable outlet. The service hands this scan the request parse_Model already
    rewrote in place, so both shapes reach it.
    """
    if not isinstance(entry, dict):
        return False
    if 'price' in entry and _absent(entry['price']):
        return False
    return _number(entry.get('ub')) != 0.0


def _recipe(case, material):
    """Signed ratio of `material` in one case: >0 produced, <0 consumed.

    Returns None when the case adjusts this material through a `db` table,
    because the effective coefficient is then property-dependent and raw JSON
    cannot settle it.
    """
    if any(row.get('material') == material for row in case.get('db') or []):
        return None
    total = 0.0
    for ratio in case.get('material_ratio') or []:
        if ratio.get('material') != material:
            continue
        value = _number(ratio.get('ratio'))
        if value is None or ratio.get('type') not in ('in', 'out'):
            return None
        total += value if ratio['type'] == 'out' else -value
    return total


def _index(data, check):
    """Every unit with the cases and the zero-pinned set it declares.

    Keyed by (table, code): a real request may use one code in two tables, such
    as a gas system that is both a processBlock and a spliterBlock, and those
    are separate units with separate variable families.
    """
    units = {}
    for table in BLOCK_TABLES:
        for block in data.get(table) or []:
            check()
            if not isinstance(block, dict) or not isinstance(block.get('code'), str):
                continue
            cases = [c for c in block.get('cases') or []
                     if isinstance(c, dict) and isinstance(c.get('code'), str)]
            units[(table, block['code'])] = {
                'table': table, 'block': block, 'code': block['code'],
                'name': block.get('name') or block['code'],
                'enabled': block.get('_status', 1) == 1,
                'cases': cases,
                'dead_cases': _forced_zero_cases(block) if block.get('_status', 1) == 1
                else {c['code'] for c in cases},
            }
    return units


def _sources(units, check):
    """material -> {'live': [entries], 'dead': [entries], 'unknown': [unit codes]}."""
    supply = {}

    def bucket(material, kind):
        return supply.setdefault(material, {'live': [], 'dead': [], 'unknown': []})[kind]

    def routed(material, unit):
        """An output not tied to a recipe: only the unit's own switch settles it."""
        if unit['enabled']:
            bucket(material, 'unknown').append(unit['code'])
        else:
            bucket(material, 'dead').append({'unit': unit['code'], 'unit_name': unit['name'],
                                             'case': None, 'case_name': '整台装置'})

    for unit in units.values():
        check()
        code = unit['code']
        block = unit['block']
        declared = {entry.get('code') for entry in block.get('output_material') or []
                    if isinstance(entry, dict)}
        if unit['table'] not in CASE_TABLES or not unit['cases']:
            # Columns and routing expressions are not read here, so any output
            # such a unit declares is a source this scan cannot rule out.
            for material in declared:
                if isinstance(material, str):
                    routed(material, unit)
            continue
        produced = set()
        for case in unit['cases']:
            check()
            for ratio in case.get('material_ratio') or []:
                if ratio.get('type') == 'out' and isinstance(ratio.get('material'), str):
                    produced.add(ratio['material'])
        for material in produced:
            check()
            live, dead = [], []
            for case in unit['cases']:
                net = _recipe(case, material)
                if net is None:
                    bucket(material, 'unknown').append(code)
                    live = dead = []
                    break
                if net <= 0:
                    continue
                entry = {'unit': code, 'unit_name': unit['name'], 'case': case['code'],
                         'case_name': case.get('name') or case['code'], 'ratio': net}
                (dead if (not unit['enabled'] or not _enabled(case)
                          or case['code'] in unit['dead_cases']) else live).append(entry)
            bucket(material, 'live').extend(live)
            bucket(material, 'dead').extend(dead)
        # An output the recipes never produce is routed some other way.
        for material in declared - produced:
            if isinstance(material, str):
                routed(material, unit)
    return supply


def _starved(material, supply):
    """A material with no reachable supply, or None when any source survives."""
    if not _enabled(material, '_status') or material.get('stock') or material.get('transfer'):
        return None
    if _tradeable(material.get('purchase')):
        return None
    split = supply.get(material['code']) or {'live': [], 'dead': [], 'unknown': []}
    if split['live'] or split['unknown']:
        return None
    # Routing metadata may name a producer whose recipe this scan never saw.
    # Everything found for this material is dead here, so a named producer is
    # accounted for exactly when it appears among those dead sources.
    dead_units = {entry['unit'] for entry in split['dead']}
    if any(producer not in dead_units for producer in material.get('input') or []):
        return None
    return split['dead'] or None


def _forced_demand(units, material, check):
    """First capacity floor whose every usable case must consume `material`."""
    for unit in units.values():
        check()
        if not unit['enabled'] or unit['table'] not in CASE_TABLES or not unit['cases']:
            continue
        code = unit['code']
        cases = {case['code']: case for case in unit['cases']}
        for capacity in unit['block'].get('mass_flow_capacity') or []:
            check()
            if not _enabled(capacity):
                continue
            lower = _number(capacity.get('lb'))
            if lower is None or lower <= 0:
                continue
            association = capacity.get('association') or []
            coefficients = capacity.get('association_coef') or {}
            if not association or len(set(association)) != len(association):
                continue
            if any(case not in cases for case in association):
                continue
            if any((_number(coefficients.get(case)) or 0) <= 0 for case in association):
                continue
            usable = [case for case in association
                      if case not in unit['dead_cases'] and _enabled(cases[case])]
            if not usable:
                # The floor is already unreachable on its own; a different rule.
                continue
            demands = []
            for case in usable:
                net = _recipe(cases[case], material)
                if net is None or net >= 0:
                    demands = []
                    break
                demands.append(-net / _number(coefficients.get(case)))
            if not demands:
                continue
            return {'unit': code, 'unit_name': unit['name'],
                    'capacity': capacity.get('code'), 'capacity_name': capacity.get('name'),
                    'capacity_lower': lower, 'cases': usable,
                    'case_names': [cases[case].get('name') or case for case in usable],
                    'minimum_required': lower * min(demands)}
    return None


def _message(material, name, dead, demand, unit_names):
    shut = sorted({entry['unit'] for entry in dead})
    listed = '、'.join(f"{unit_names.get(code, code)}（{code}）" for code in shut[:3])
    listed += f" 等 {len(shut)} 台" if len(shut) > 3 else ''
    cases = '、'.join(entry['case_name'] for entry in dead[:4])
    cases += f" 等 {len(dead)} 个" if len(dead) > 4 else ''
    capacity = demand['capacity_name'] or demand['capacity']
    return (
        f"「{name}（{material}）」没有任何可用来源：它只由 {listed} 产出，"
        f"而其产出方案（{cases}）已被停用或加工能力上限为 0，该物料也未配置外购。"
        f"但「{demand['unit_name']}（{demand['unit']}）」的能力下限 "
        f"{capacity}（CCAP{demand['capacity']}）= {demand['capacity_lower']:.10g} 要求它必须开车，"
        f"其 {len(demand['cases'])} 个可用方案全部需要「{name}」进料，"
        f"按该下限至少需要 {demand['minimum_required']:.10g}，可用供应量为 0。"
        f"请三选一处理：① 放开 {listed} 的加工能力上限；"
        f"② 为 materialList[code={material}] 配置外购；"
        f"③ 取消 {demand['unit_name']} 的能力下限"
        f"（{demand['unit']} 的 mass_flow_capacity[code={demand['capacity']}].lb）。"
    )


def starved_forced_unit_hints(data, time_limit=2.0, trace=None):
    """Report materials a shutdown starves while a capacity floor forces demand.

    `trace`, when given, is filled with what actually happened. A cooperative
    timeout returns whatever was already confirmed, which looks exactly like
    "nothing found" unless the caller can see that the scan was cut short.
    """
    deadline = time.perf_counter() + time_limit
    trace = {} if trace is None else trace
    trace.update(outcome='completed', timed_out=False)

    def check():
        if time.perf_counter() >= deadline:
            raise TimeoutError

    hints = []
    try:
        if any(data.get(key) for key in ('plant', 'period', 'storage')):
            trace.update(outcome='skipped', reason='multi_plant_period_storage')
            return []
        materials = {}
        for material in data.get('materialList') or []:
            check()
            if not isinstance(material, dict) or not isinstance(material.get('code'), str):
                continue
            if material['code'] in materials:   # Ambiguous codes: no finding, but say so.
                trace.update(outcome='skipped', reason='duplicate_material_code')
                return []
            materials[material['code']] = material
        if not materials:
            return []
        units = _index(data, check)
        supply = _sources(units, check)
        unit_names = {unit['code']: unit['name'] for unit in units.values()}
        for code, material in materials.items():
            check()
            dead = _starved(material, supply)
            if not dead:
                continue
            demand = _forced_demand(units, code, check)
            if not demand:
                continue
            name = material.get('name') or code
            hints.append({
                'rule_id': 'RAW_SHUTDOWN_STARVES_FORCED_UNIT', 'confidence': 'candidate',
                'message': _message(code, name, dead, demand, unit_names),
                'subject': code, 'subject_kind': 'material', 'subject_name': name,
                'subject_path': f"materialList[code={code}]",
                'shutdown_sources': dead,
                'shutdown_units': sorted({entry['unit'] for entry in dead}),
                'forced_unit': demand['unit'], 'forced_capacity': 'CCAP' + str(demand['capacity']),
                'capacity_lower': demand['capacity_lower'],
                'forced_cases': demand['cases'],
                'minimum_required': demand['minimum_required'],
                'limitation': '依据原始入参的装置开停与配方结构；未核验全部生效边界与替代供应路径，不据此改写求解状态。',
            })
            if len(hints) >= MAX_HINTS:
                break
    except TimeoutError:
        # Cooperative budget: report whatever was already confirmed, and say so.
        trace.update(outcome='timed_out', timed_out=True)
        return hints
    except (KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError) as exc:
        trace.update(outcome='data_error', reason=f'{type(exc).__name__}: {exc}')
        return hints
    return hints

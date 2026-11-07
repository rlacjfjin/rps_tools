"""Budgeted raw-input clue: a balance subject that can only be closed at zero.

Third scan of the existing failure-only clue layer, next to `input_hints`
(crude supply) and `route_hints` (a forced product with no outlet). Like those,
it reads the request JSON only, costs milliseconds, needs no model, and still
speaks when the full certificate search runs out of budget.

The rule itself is shape-agnostic: a balance is an equality, so when every
remaining term on one subject produces it, nothing left alive consumes it, and
no external outlet exists, the balance can only hold with all of those terms at
zero. That silently pins the producing cases, and anything forcing them positive
makes the model infeasible. Raw JSON only exposes this structure for utility
subjects; the material side of the same idea is `route_hints`.

These stay candidates: raw JSON does not establish effective bounds, model
transformations or alternative routes. They never change the solver status.
"""
import math
import time

BLOCK_TABLES = ('cduBlock', 'manuBlock', 'processBlock', 'blenderBlock',
                'mixerBlock', 'multi_mixerBlock', 'spliterBlock', 'customBlock')
MAX_HINTS = 3


def _absent(value):
    return value is None or (isinstance(value, float) and math.isnan(value))


def _number_or_none(value):
    if _absent(value) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _is_zero(value):
    number = _number_or_none(value)
    return number is not None and number == 0.0


def _trade_outlet(entry):
    """Return 'open', 'zero' (an explicit 0 limit) or 'none' (never configured)."""
    if not isinstance(entry, dict):
        return 'none'
    if _is_zero(entry.get('ub')):
        return 'zero'
    return 'open'


def _forced_zero_cases(block):
    """Cases/columns that a capacity ceiling of 0 or a switch already pins to 0.

    Capacity rows are sums of nonnegative case activities with nonnegative
    coefficients, so an upper bound of 0 leaves every associated case at 0.
    """
    dead = set()
    for field in ('mass_flow_capacity', 'capacity_balance'):
        for capacity in block.get(field) or []:
            if not isinstance(capacity, dict) or capacity.get('status', 1) != 1:
                continue
            if not _is_zero(capacity.get('ub')):
                continue
            expr = capacity.get('expr')
            if isinstance(expr, dict):
                # A capacity_balance expr lists every column of the unit and
                # leaves the ones it does not constrain at null. Only terms with
                # a real coefficient are pinned by the ceiling.
                terms = {code: _number_or_none(value) for code, value in expr.items()}
                terms = {code: value for code, value in terms.items() if value}
                if any(value < 0 for value in terms.values()):
                    continue
                dead.update(code for code in terms if isinstance(code, str))
                continue
            coefficients = (capacity.get('association_coef') or {}).values()
            if any((_number_or_none(value) or 0) < 0 for value in coefficients):
                continue
            dead.update(code for code in capacity.get('association') or [] if isinstance(code, str))
    for column in block.get('column') or []:
        if not isinstance(column, dict) or not isinstance(column.get('code'), str):
            continue
        if column.get('status', 1) != 1 or _is_zero(column.get('ub')):
            dead.add(column['code'])
    for case in block.get('cases') or []:
        if isinstance(case, dict) and isinstance(case.get('code'), str) and case.get('status', 1) == 0:
            dead.add(case['code'])
    return dead


def _collect(data, check):
    """Split every utility term into live/dead producers and consumers."""
    sides = {}

    def record(code, side, alive, entry):
        if not isinstance(code, str):
            return
        split = sides.setdefault(code, {'live_out': [], 'live_in': [], 'dead_out': [], 'dead_in': []})
        split[('live_' if alive else 'dead_') + side].append(entry)

    for table in BLOCK_TABLES:
        for block in data.get(table) or []:
            check()
            if not isinstance(block, dict) or not isinstance(block.get('code'), str):
                continue
            enabled = block.get('_status', 1) == 1
            dead_cases = _forced_zero_cases(block)
            names = {}
            for group in ('column', 'cases'):
                for item in block.get(group) or []:
                    if isinstance(item, dict) and isinstance(item.get('code'), str):
                        names[item['code']] = item.get('name') or item['code']

            def entry(case, coefficient, path):
                return {'unit': block['code'], 'unit_name': block.get('name') or block['code'],
                        'case': case, 'case_name': names.get(case, case) if isinstance(case, str) else case,
                        'coefficient': abs(coefficient), 'input_path': path}

            for balance in block.get('utility_balance') or []:
                check()
                if not isinstance(balance, dict) or balance.get('type') not in ('utility_in', 'utility_out'):
                    continue
                side = 'in' if balance['type'] == 'utility_in' else 'out'
                active = balance.get('status', 1) == 1
                path = f"{table}[code={block['code']}].utility_balance[code={balance.get('code')}]"
                for case, coefficient in (balance.get('expr') or {}).items():
                    number = _number_or_none(coefficient)
                    if not number:
                        continue
                    alive = enabled and active and case not in dead_cases
                    record(balance.get('code'), side, alive, entry(case, number, path))
            for usage in block.get('utility') or []:
                check()
                if not isinstance(usage, dict):
                    continue
                number = _number_or_none(usage.get('coefficient'))
                if not number:
                    continue
                case = usage.get('association')
                # A positive utility coefficient is consumption in RPS.
                side = 'in' if number > 0 else 'out'
                alive = enabled and not (isinstance(case, str) and case in dead_cases)
                path = f"{table}[code={block['code']}].utility[material={usage.get('material')}]"
                record(usage.get('material'), side, alive, entry(case, number, path))
    return sides


def _sole_consumer_of(sides, unit, cases):
    """Subjects whose only live consumer is one of the cases being pinned to zero."""
    blocked = []
    for code, split in sides.items():
        if not split['live_out'] or not split['live_in']:
            continue
        if all(item['unit'] == unit and item['case'] in cases for item in split['live_in']):
            blocked.append(code)
    return sorted(blocked)


def _describe(items, limit=4):
    text = '、'.join(f"{item['unit_name']}（{item['unit']}）的 {item['case_name']}" for item in items[:limit])
    return text + (f" 等 {len(items)} 项" if len(items) > limit else '')


def _message(code, name, split, purchase, sale, sides, names):
    outlet = {'zero': '上限填的是 0', 'none': '未配置'}
    lines = [f"公用工程「{name}（{code}）」的平衡无法闭合：只有产出、没有在用的消耗方，"
             f"外购{outlet[purchase]}、外销{outlet[sale]}。",
             f"产出方：{_describe(split['live_out'])}。"]
    if split['dead_in']:
        lines.append("已失效的消耗方：" + "；".join(
            f"{item['unit_name']}（{item['unit']}）的 {item['case_name']} 方案配了 "
            f"{item['coefficient']:.10g} 的消耗，但该方案已停用或加工能力上限为 0"
            for item in split['dead_in'][:4]) + "。")
    else:
        lines.append("全模型没有任何装置配置了它的消耗。")
    lines.append("平衡是等式，这会把上述产出方案全部强制为 0。")
    pinned_units = sorted({item['unit'] for item in split['live_out']})
    knocked = (_sole_consumer_of(sides, pinned_units[0], {item['case'] for item in split['live_out']})
               if len(pinned_units) == 1 else [])
    if knocked:
        lines.append(f"被强制为 0 的方案同时是 "
                     f"{'、'.join(f'{names.get(c, c)}（{c}）' for c in knocked[:4])} 的唯一消耗方，"
                     f"其产出也会被一并压到 0，装置最低负荷等要求可能因此无法满足。")
    lines.append(f"请三选一处理：① 给实际消耗「{name}」的装置补上消耗系数；"
                 f"② 放开 utility[code={code}] 的外销或外购上限；"
                 f"③ 若本方案不核算该项，停用产出行（{split['live_out'][0]['input_path']}.status=0）。")
    return ''.join(lines), pinned_units, knocked


def balance_deadend_hints(data, time_limit=2.0, trace=None):
    """Report balance subjects whose production has nowhere to go.

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
        utilities = data.get('utility') or []
        if not utilities:
            return []
        names = {u.get('code'): (u.get('name') or u.get('code')) for u in utilities if isinstance(u, dict)}
        sides = _collect(data, check)
        for utility in utilities:
            check()
            code = utility.get('code')
            if not isinstance(code, str) or utility.get('_status', 1) != 1 or utility.get('dumping'):
                continue
            split = sides.get(code)
            if not split or not split['live_out'] or split['live_in']:
                continue
            purchase, sale = _trade_outlet(utility.get('purchase')), _trade_outlet(utility.get('sell'))
            if purchase == 'open' or sale == 'open':
                continue
            # Without either an explicit zero limit or a consumer the data once
            # configured, this is an accounting-only stream that is meant to
            # stay at zero. Reporting those would bury the real cases.
            if 'zero' not in (purchase, sale) and not split['dead_in']:
                continue
            name = names.get(code, code)
            message, pinned_units, knocked = _message(code, name, split, purchase, sale, sides, names)
            hints.append({'rule_id': 'RAW_BALANCE_DEADEND', 'confidence': 'candidate',
                          'message': message, 'subject': code, 'subject_kind': 'utility',
                          'subject_name': name, 'subject_path': f"utility[code={code}]",
                          'purchase_outlet': purchase, 'sell_outlet': sale,
                          'producers': split['live_out'], 'disabled_consumers': split['dead_in'],
                          'pinned_units': pinned_units, 'blocked_subjects': knocked,
                          'limitation': '依据原始入参的平衡结构；未核验全部生效边界与替代路径，不据此改写求解状态。'})
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

"""表达式层结构异常检测 —— 纯解析 proto_expression.txt。

实现:
  Pattern B: Effective-zero 闭包传播      （锁死变量集合）
  Pattern C: 死项 + L 约束单侧死区        ★ silent dumping fingerprint
  Pattern D: 退化等式 / 矛盾常数等式
  Pattern E: bound vs 单项等式 冲突
  Pattern F: 真活非线性项计数

不依赖 MPProto 对象，给一个 proto_expression.txt 就能工作（适合 CI 后处理 / 异地排查）。

用法:
    python -m rps_tools.diagnose.parse_expressions --export-dir export
    python -m rps_tools.diagnose.parse_expressions --proto path/to/proto_expression.txt
"""
from __future__ import annotations
import argparse
import os
import re
import sys
from collections import defaultdict
from glob import glob

if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

EPS = 1e-9

# 变量声明: NAME----lb:X    ub:Y    start:Z    value:W
VAR_DECL_RE = re.compile(
    r'^(?P<name>\S+?)----'
    r'lb:(?P<lb>\S+)\s+ub:(?P<ub>\S+)\s+start:(?P<start>\S+)\s+value:(?P<value>\S+)',
    re.MULTILINE,
)
# 约束首部: TYPE-CODE:  rest
CONS_RE = re.compile(r'^(?P<type>[A-Z]+)-(?P<code>\S.*?):\s*(?P<body>.*?)\s*$')


def to_float(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_term(term_str: str) -> tuple[list[str], float]:
    """'A*B*1.0' -> (['A','B'], 1.0). 最后一个 '*<num>' 视为系数。"""
    if not term_str:
        return [], 0.0
    pos = term_str.rfind('*')
    if pos < 0:
        c = to_float(term_str)
        if c is not None:
            return [], c
        return [term_str], 1.0
    coef = to_float(term_str[pos + 1:])
    if coef is None:
        return term_str.split('*'), 1.0
    factors = term_str[:pos].split('*') if pos > 0 else []
    return factors, coef


def parse_constraint_line(line: str):
    m = CONS_RE.match(line)
    if not m:
        return None
    ctype = m.group('type')
    code = m.group('code')
    body = m.group('body')
    op_pos = -1
    op = None
    for o in ('==', '<=', '>='):
        p = body.rfind(o)
        if p > op_pos:
            op_pos, op = p, o
    if op:
        expr = body[:op_pos].strip()
        rhs = to_float(body[op_pos + 2:].strip())
    else:
        expr = body
        rhs = None
    terms = []
    if expr:
        for ts in re.split(r'\s\+\s', expr):
            ts = ts.strip()
            if ts:
                terms.append(parse_term(ts))
    return ctype, code, terms, op, rhs


def load_proto(path: str):
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    var_bounds = {}
    for m in VAR_DECL_RE.finditer(text):
        var_bounds[m.group('name')] = {
            'lb': to_float(m.group('lb')),
            'ub': to_float(m.group('ub')),
            'start': to_float(m.group('start')),
            'value': to_float(m.group('value')),
        }
    e_rows, l_rows, g_rows = [], [], []
    for line in text.split('\n'):
        if not line or line[0] in '#=[(':
            continue
        # 跳过 X 行（ratio defs，body 里含 / 不好结构化）
        if line.startswith('X-'):
            continue
        parsed = parse_constraint_line(line)
        if not parsed:
            continue
        ctype, code, terms, op, rhs = parsed
        if ctype == 'E':
            e_rows.append((code, terms, rhs))
        elif ctype == 'L':
            l_rows.append((code, terms, rhs))
        elif ctype == 'G':
            g_rows.append((code, terms, rhs))
    return var_bounds, e_rows, l_rows, g_rows


# ------- Pattern B: Effective-zero 闭包 -------

def compute_ez(var_bounds: dict, e_rows: list, max_iter: int = 30) -> tuple[set, dict]:
    """返回 (EZ 集合, 每个 EZ 变量的来源说明 dict)."""
    ez = set()
    source = {}  # var -> "reason"
    for v, b in var_bounds.items():
        lb, ub = b['lb'], b['ub']
        if lb is not None and ub is not None and abs(lb) < EPS and abs(ub) < EPS:
            ez.add(v)
            source[v] = 'bound: lb=ub=0'

    for iteration in range(max_iter):
        added = 0
        for code, terms, rhs in e_rows:
            if rhs is None or abs(rhs) > EPS:
                continue
            unknown = []
            blocked = False
            for factors, coef in terms:
                if any(f in ez for f in factors) or abs(coef) < EPS:
                    continue
                if len(factors) == 1:
                    unknown.append((factors[0], coef))
                else:
                    blocked = True
                    break
            if blocked or len(unknown) != 1:
                continue
            v, c = unknown[0]
            if abs(c) > EPS and v not in ez:
                ez.add(v)
                source[v] = f'singleton E: {code}'
                added += 1
        if added == 0:
            break
    return ez, source


def term_is_dead(factors: list[str], coef: float, ez: set) -> bool:
    if abs(coef) < EPS:
        return True
    return any(f in ez for f in factors)


# ------- Pattern C: L 行单侧死区 -------

def scan_dead_side_L(l_rows: list, ez: set) -> list[dict]:
    out = []
    for code, terms, rhs in l_rows:
        if not terms:
            continue
        pos_alive = neg_alive = pos_dead = neg_dead = 0
        ez_factors = set()
        for factors, coef in terms:
            dead = term_is_dead(factors, coef, ez)
            if dead:
                ez_factors.update(f for f in factors if f in ez)
            if coef > EPS:
                if dead:
                    pos_dead += 1
                else:
                    pos_alive += 1
            elif coef < -EPS:
                if dead:
                    neg_dead += 1
                else:
                    neg_alive += 1
        # 一侧全死，另一侧仍活
        if pos_dead > 0 and pos_alive == 0 and neg_alive > 0:
            out.append({'code': code, 'side': 'positive', 'dead': pos_dead,
                        'alive': neg_alive, 'ez_factors': ez_factors})
        elif neg_dead > 0 and neg_alive == 0 and pos_alive > 0:
            out.append({'code': code, 'side': 'negative', 'dead': neg_dead,
                        'alive': pos_alive, 'ez_factors': ez_factors})
    out.sort(key=lambda r: -r['dead'])
    return out


# ------- Pattern D: 退化等式 -------

def scan_degenerate_E(e_rows: list, ez: set) -> tuple[list, list]:
    redundant = []
    infeasible = []
    for code, terms, rhs in e_rows:
        if not terms:
            if rhs is not None and abs(rhs) > EPS:
                infeasible.append((code, 0.0, rhs))
            else:
                redundant.append(code)
            continue
        if all(term_is_dead(f, c, ez) for f, c in terms):
            if rhs is not None and abs(rhs) > EPS:
                infeasible.append((code, 0.0, rhs))
            else:
                redundant.append(code)
    return redundant, infeasible


# ------- Pattern E: bound vs equality 冲突 -------

def scan_bound_eq_conflict(e_rows: list, var_bounds: dict) -> list[dict]:
    conflicts = []
    for code, terms, rhs in e_rows:
        if len(terms) != 1 or rhs is None:
            continue
        factors, coef = terms[0]
        if len(factors) != 1 or abs(coef) < EPS:
            continue
        v = factors[0]
        forced = rhs / coef
        b = var_bounds.get(v)
        if not b:
            continue
        lb, ub = b['lb'], b['ub']
        if lb is not None and forced < lb - 1e-6:
            conflicts.append({'var': v, 'cons': code, 'forced': forced,
                              'lb': lb, 'ub': ub, 'side': 'below lb'})
        elif ub is not None and forced > ub + 1e-6:
            conflicts.append({'var': v, 'cons': code, 'forced': forced,
                              'lb': lb, 'ub': ub, 'side': 'above ub'})
    return conflicts


# ------- Pattern F: 非线性项统计 -------

def count_nl_terms(rows: list, ez: set) -> tuple[dict, dict, int]:
    alive = defaultdict(int)
    dead = defaultdict(int)
    total = 0
    for code, terms, rhs in rows:
        for factors, coef in terms:
            total += 1
            deg = len(factors)
            if term_is_dead(factors, coef, ez):
                dead[deg] += 1
            else:
                alive[deg] += 1
    return alive, dead, total


# ------- 主流程 -------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--export-dir', default='export')
    ap.add_argument('--proto', help='proto_expression txt path')
    ap.add_argument('--limit', type=int, default=20)
    ap.add_argument('--material', help='可选：聚焦某物料，打印 EZ 追溯链')
    args = ap.parse_args(argv)

    proto_path = args.proto
    if not proto_path:
        files = glob(os.path.join(args.export_dir, '*_proto_expression.txt'))
        if files:
            proto_path = max(files, key=os.path.getmtime)
    if not proto_path or not os.path.exists(proto_path):
        sys.exit('缺少 proto_expression.txt，请用 --proto 或 --export-dir 指定。')

    print(f'proto: {proto_path}')
    var_bounds, e_rows, l_rows, g_rows = load_proto(proto_path)
    print(f'变量: {len(var_bounds)}  E行: {len(e_rows)}  L行: {len(l_rows)}  G行: {len(g_rows)}')

    ez, ez_source = compute_ez(var_bounds, e_rows)
    seed = sum(1 for v in ez if ez_source[v].startswith('bound'))
    print(f'\n[B] Effective-zero 集合 |EZ|={len(ez)}  (seed by bounds={seed}, propagated={len(ez)-seed})')

    # Pattern C
    print()
    print('=' * 72)
    print('[C] L 约束单侧死亡 (silent-dumping fingerprint)')
    print('=' * 72)
    dead_side = scan_dead_side_L(l_rows, ez)
    wbal_dead = [r for r in dead_side if r['code'].startswith('WBAL')]
    other_dead = [r for r in dead_side if not r['code'].startswith('WBAL')]
    print(f'WBAL 类（物料平衡）单侧死亡: {len(wbal_dead)} 条 ★')
    for r in wbal_dead[:args.limit]:
        mat = r['code'][len('WBAL'):]
        ez_show = ','.join(sorted(r['ez_factors'])[:3])
        if len(r['ez_factors']) > 3:
            ez_show += f',...(+{len(r["ez_factors"])-3})'
        print(f'  ⚠ L-WBAL{mat}: {r["side"]} side dead ({r["dead"]} terms), '
              f'alive={r["alive"]}, EZ 因子: [{ez_show}]')
    print(f'\n其他 L 行单侧死亡: {len(other_dead)} 条')
    for r in other_dead[:args.limit]:
        print(f'  L-{r["code"]}: {r["side"]} side dead ({r["dead"]} terms), alive={r["alive"]}')

    # Pattern D
    print()
    print('=' * 72)
    print('[D] 退化等式')
    print('=' * 72)
    redundant, infeasible = scan_degenerate_E(e_rows, ez)
    print(f'redundant (LHS全dead, RHS=0): {len(redundant)} 条')
    print(f'⚠ infeasible (LHS全dead, RHS≠0): {len(infeasible)} 条')
    for code, lhs, rhs in infeasible[:args.limit]:
        print(f'  ⚠ E-{code}: 0 == {rhs}  数学上不可行')

    # Pattern E
    print()
    print('=' * 72)
    print('[E] Bound vs 单项等式 冲突')
    print('=' * 72)
    conflicts = scan_bound_eq_conflict(e_rows, var_bounds)
    print(f'共 {len(conflicts)} 条')
    for c in conflicts[:args.limit]:
        print(f'  ⚠ {c["var"]} forced to {c["forced"]} by E-{c["cons"]}, '
              f'bounds=[{c["lb"]}, {c["ub"]}]  ({c["side"]})')

    # Pattern F
    print()
    print('=' * 72)
    print('[F] 非线性项（degree≥2）统计')
    print('=' * 72)
    all_rows = e_rows + l_rows + g_rows
    alive, dead, total = count_nl_terms(all_rows, ez)
    print(f'总项数: {total}')
    for d in sorted(set(alive) | set(dead)):
        print(f'  degree={d}: alive={alive[d]:>7} dead={dead[d]:>7}')
    nl_alive = sum(alive[d] for d in alive if d >= 2)
    nl_dead = sum(dead[d] for d in dead if d >= 2)
    nl_total = nl_alive + nl_dead
    if nl_total > 0:
        print(f'-- 非线性项 alive={nl_alive}, dead={nl_dead}  '
              f'(死项占 {nl_dead/nl_total:.1%}，越高说明 EZ 锁链越严重)')

    # HINT
    print()
    print('=' * 72)
    print('[HINT] 表达式级根因候选 (WBAL 单侧死亡 → 该物料可甩货)')
    print('=' * 72)
    if not wbal_dead:
        print('  无 — 物料平衡都正常。')
    else:
        for r in wbal_dead[:args.limit]:
            mat = r['code'][len('WBAL'):]
            ez_list = sorted(r['ez_factors'])
            print(f'  - {mat}')
            print(f'      死项 EZ 因子: {ez_list[:6]}{"..." if len(ez_list)>6 else ""}')
            # 追溯每个 EZ 因子的来源
            for v in ez_list[:3]:
                print(f'        {v}  ←  {ez_source.get(v, "?")}')

    # 单物料聚焦模式
    if args.material:
        print()
        print('=' * 72)
        print(f'[FOCUS] 物料 {args.material} 的链路追溯')
        print('=' * 72)
        mat = args.material
        target = f'WBAL{mat}'
        matched = [r for r in dead_side if r['code'] == target]
        if matched:
            r = matched[0]
            print(f'L-{target}: {r["side"]} side dead, dead={r["dead"]}, alive={r["alive"]}')
            for v in sorted(r['ez_factors']):
                print(f'  EZ 因子 {v}  来源: {ez_source.get(v, "?")}')
                # 如果是 propagated，找具体的 E 行
                src = ez_source.get(v, '')
                if src.startswith('singleton E:'):
                    e_code = src.split(': ', 1)[1]
                    for c, terms, rhs in e_rows:
                        if c == e_code:
                            print(f'    └─ E-{e_code}: ' +
                                  ' + '.join('*'.join(list(f) + [str(co)]) for f, co in terms) +
                                  f' == {rhs}')
                            break
        else:
            print(f'L-{target} 未出现在死区列表里 — 物料 {mat} 在表达式上看正常。')


if __name__ == '__main__':
    main()

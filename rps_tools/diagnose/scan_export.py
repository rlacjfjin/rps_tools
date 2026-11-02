"""全量扫描 export 目录，列出所有可疑物料。

启发式规则：
  R1: output 里 |dumping| > eps
  R2: proto 中 Q<PROP><MAT>  lb=ub=0  且该 material 在 output 出现
  R3: input materialList 里 property 数组为空，但该物料作为 spliter property_set 的输入
  R4: input materialList 里某 property: start=0, prop_status=4，且无上游 property_transfer

用法:
    python -m rps_tools.diagnose.scan_export --export-dir export
"""
from __future__ import annotations
import argparse
import json
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
VAR_DECL_RE = re.compile(
    r'^(?P<name>[A-Za-z0-9#\-_()]+)----'
    r'lb:(?P<lb>[^\s]+)\s+ub:(?P<ub>[^\s]+)\s+start:(?P<start>[^\s]+)\s+value:(?P<value>\S+)',
    re.MULTILINE,
)


def _latest(pattern: str, export_dir: str) -> str | None:
    files = glob(os.path.join(export_dir, pattern))
    return max(files, key=os.path.getmtime) if files else None


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def index_proto_vars(proto_path: str) -> dict[str, dict]:
    with open(proto_path, 'r', encoding='utf-8') as f:
        text = f.read()
    out = {}
    for m in VAR_DECL_RE.finditer(text):
        out[m.group('name')] = {k: m.group(k) for k in ('lb', 'ub', 'start', 'value')}
    return out


def material_codes(data: dict) -> set[str]:
    return {m.get('code') for m in data.get('materialList', []) or [] if m.get('code')}


def scan_dumping(output_data: dict) -> list[dict]:
    rows = []
    for m in output_data.get('materialList', []) or []:
        d = m.get('dumping')
        if d is not None and abs(d) > EPS:
            rows.append({'material': m.get('code'),
                         'type': m.get('type'),
                         'dumping': d,
                         'marginal_benefit': m.get('marginal_benefit')})
    rows.sort(key=lambda r: -abs(r['dumping']))
    return rows


def scan_locked_props(var_index: dict, codes: set[str]) -> list[dict]:
    # 按物料聚合：哪些物料的某个 Q<prop><mat> 被锁成 0
    # 注意：suffix 匹配可能歧义（A 是 B 的后缀）。我们走"最长匹配"：
    # 把 codes 按长度倒序，逐 var 找一个匹配。
    sorted_codes = sorted(codes, key=len, reverse=True)
    locked = defaultdict(list)
    for name, info in var_index.items():
        if not name.startswith('Q'):
            continue
        lb, ub = _to_float(info['lb']), _to_float(info['ub'])
        if lb is None or ub is None or abs(lb) >= EPS or abs(ub) >= EPS:
            continue
        body = name[1:]
        for c in sorted_codes:
            if body.endswith(c):
                prop = body[:-len(c)]
                if prop and prop.isupper():
                    locked[c].append({'prop': prop, 'var': name})
                break
    rows = [{'material': c, 'locked_props': sorted(v, key=lambda x: x['prop'])}
            for c, v in locked.items()]
    rows.sort(key=lambda r: (-len(r['locked_props']), r['material']))
    return rows


def scan_empty_property_data(input_data: dict) -> list[dict]:
    """找 property 数组为空，但被 spliter 的 property_set 引用作为输入的物料."""
    # 谁是 spliter 的输入？
    spliter_inputs = defaultdict(list)  # mat -> [(spliter_code, set_of_props)]
    for b in input_data.get('spliterBlock', []) or []:
        ps = b.get('property_set', []) or []
        props = [p if isinstance(p, str) else p.get('code') for p in ps]
        for im in b.get('input_material', []) or []:
            spliter_inputs[im.get('code')].append((b.get('code'), props))
    rows = []
    for m in input_data.get('materialList', []) or []:
        code = m.get('code')
        if not m.get('property') and code in spliter_inputs:
            rows.append({
                'material': code,
                'consumed_by_spliter': [s for s, _ in spliter_inputs[code]],
                'props_expected': sorted({p for _, ps in spliter_inputs[code] for p in ps if p}),
            })
    rows.sort(key=lambda r: r['material'])
    return rows


def scan_zero_start_prop(input_data: dict) -> list[dict]:
    """找 property.start=0 且 prop_status=4，且没有上游 property_transfer 写入。"""
    # 谁的某个属性有 property_transfer 写入？
    has_upstream = set()  # (mat, prop)
    for k in ('manuBlock', 'processBlock'):
        for b in input_data.get(k, []) or []:
            for pt in b.get('property_transfer', []) or []:
                has_upstream.add((pt.get('out_material'), pt.get('prop')))
    # 谁在 spliter 输出，且 spliter 有 property_set？
    for b in input_data.get('spliterBlock', []) or []:
        ps = b.get('property_set', []) or []
        props = [p if isinstance(p, str) else p.get('code') for p in ps]
        for om in b.get('output_material', []) or []:
            for p in props:
                if p:
                    has_upstream.add((om.get('code'), p))
    rows = []
    for m in input_data.get('materialList', []) or []:
        code = m.get('code')
        for p in m.get('property', []) or []:
            if p.get('start') == 0 and p.get('prop_status') == 4 and (code, p.get('code')) not in has_upstream:
                rows.append({'material': code, 'prop': p.get('code'),
                             'start': 0, 'prop_status': 4})
    rows.sort(key=lambda r: (r['material'], r['prop']))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--export-dir', default='export')
    ap.add_argument('--input', help='model_input json path')
    ap.add_argument('--output', help='model_output json path')
    ap.add_argument('--proto', help='proto_expression txt path')
    ap.add_argument('--limit', type=int, default=50, help='每类最多打印多少条')
    args = ap.parse_args(argv)

    inp_path = args.input or _latest('*_model_input*.json', args.export_dir)
    out_path = args.output or _latest('*_model_output*.json', args.export_dir)
    proto_path = args.proto or _latest('*_proto_expression.txt', args.export_dir)
    missing = [n for n, p in (('input', inp_path), ('output', out_path), ('proto', proto_path)) if not p]
    if missing:
        sys.exit(f'缺少文件: {missing}')

    print(f'input : {inp_path}')
    print(f'output: {out_path}')
    print(f'proto : {proto_path}')

    with open(inp_path, 'r', encoding='utf-8') as f:
        input_data = json.load(f)
    with open(out_path, 'r', encoding='utf-8') as f:
        output_data = json.load(f)
    var_index = index_proto_vars(proto_path)
    print(f'proto 变量索引: {len(var_index)} 条\n')

    # R1
    print('=' * 72)
    print('[R1] 物料平衡异常 (|dumping| > 0)')
    print('=' * 72)
    rows = scan_dumping(output_data)
    print(f'共 {len(rows)} 条。')
    for r in rows[:args.limit]:
        sign = '产>耗' if r['dumping'] < 0 else '耗>产'
        print(f'  {r["material"]:<30}  dumping={r["dumping"]:+.6g}  ({sign})  '
              f'mb={r["marginal_benefit"]}  type={r["type"]}')

    # R2
    print()
    print('=' * 72)
    print('[R2] LP 中属性变量被锁成 0 (Q<prop><mat> lb=ub=0)')
    print('=' * 72)
    codes = material_codes(output_data) | material_codes(input_data)
    rows = scan_locked_props(var_index, codes)
    print(f'共 {len(rows)} 个物料被锁。')
    for r in rows[:args.limit]:
        props = ','.join(p['prop'] for p in r['locked_props'])
        print(f'  {r["material"]:<30}  锁住的属性: [{props}]')

    # R3
    print()
    print('=' * 72)
    print('[R3] property 数组为空，但被 spliter property_set 引用')
    print('=' * 72)
    rows = scan_empty_property_data(input_data)
    print(f'共 {len(rows)} 条 — 这些往往是属性断链的"真正根因"。')
    for r in rows[:args.limit]:
        print(f'  {r["material"]:<30}  spliter={r["consumed_by_spliter"]}  '
              f'缺属性={r["props_expected"]}')

    # R4
    print()
    print('=' * 72)
    print('[R4] property.start=0 且 prop_status=4 且无上游 property_transfer')
    print('=' * 72)
    rows = scan_zero_start_prop(input_data)
    print(f'共 {len(rows)} 条。')
    for r in rows[:args.limit]:
        print(f'  {r["material"]:<30}  prop={r["prop"]}')

    # HINT: 把 R1 的 dumping 物料和 R2/R3 关联起来
    print()
    print('=' * 72)
    print('[HINT] R1 dumping 物料的属性链路追溯')
    print('=' * 72)
    locked_set = {r['material'] for r in scan_locked_props(var_index, codes)}
    empty_set = {r['material'] for r in scan_empty_property_data(input_data)}

    # 建 property_transfer 反向图: out_material -> [in_material]
    transfer_up = defaultdict(set)
    for k in ('manuBlock', 'processBlock'):
        for b in input_data.get(k, []) or []:
            for pt in b.get('property_transfer', []) or []:
                if pt.get('out_material') and pt.get('in_material'):
                    transfer_up[pt['out_material']].add(pt['in_material'])
    # spliter 反向图
    for b in input_data.get('spliterBlock', []) or []:
        outs = [om.get('code') for om in b.get('output_material', []) or [] if om.get('code')]
        ins = [im.get('code') for im in b.get('input_material', []) or [] if im.get('code')]
        if b.get('property_set'):
            for o in outs:
                for i in ins:
                    transfer_up[o].add(i)

    def trace_upstream(start: str, max_depth: int = 4) -> list[tuple[str, int]]:
        seen = {start}
        out = []
        queue = [(start, 0)]
        while queue:
            cur, d = queue.pop(0)
            if d >= max_depth:
                continue
            for up in transfer_up.get(cur, ()):
                if up not in seen:
                    seen.add(up)
                    out.append((up, d + 1))
                    queue.append((up, d + 1))
        return out

    # 预扫：每个物料的生产者 case 是否撞 capacity ub（Pattern G push 信号）
    def find_push_cases(mat):
        out = []
        for k in ('cduBlock', 'manuBlock', 'blenderBlock', 'mixerBlock',
                  'spliterBlock', 'customBlock', 'processBlock'):
            for b in input_data.get(k, []) or []:
                for c in b.get('cases', []) or []:
                    produces = any(mr.get('material') == mat and mr.get('type') == 'out'
                                   for mr in c.get('material_ratio', []) or [])
                    if not produces:
                        continue
                    for ob in output_data.get(k, []) or []:
                        if ob.get('code') != b.get('code'):
                            continue
                        for mc in ob.get('mass_flow_capacity', []) or []:
                            if c.get('code') in (mc.get('association') or []):
                                ub = mc.get('ub')
                                val = mc.get('value')
                                if ub is not None and val is not None \
                                        and abs(val - ub) < 1e-6 and ub > 1e-9:
                                    out.append((b.get('code'), c.get('code'), val, ub))
        return out

    # customVariable 硬锁
    clocks = [v.get('code') for v in input_data.get('customVariable', []) or []
              if v.get('lb') is not None and v.get('ub') is not None
              and abs(float(v['lb']) - float(v['ub'])) < 1e-9 and abs(float(v['lb'])) > 1e-9]

    for r in scan_dumping(output_data):
        mat = r['material']
        chain = trace_upstream(mat)
        culprit_locked = [u for u, _ in chain if u in locked_set]
        culprit_empty = [u for u, _ in chain if u in empty_set]
        directly_locked = mat in locked_set
        push_cases = find_push_cases(mat)
        # 分类
        is_dead_side = directly_locked or bool(culprit_locked) or bool(culprit_empty)
        is_push = bool(push_cases)
        if is_dead_side and not is_push:
            kind = 'DEAD-SIDE (属性锁死链)'
        elif is_push and not is_dead_side:
            kind = 'PUSH (产能推送)'
        elif is_dead_side and is_push:
            kind = 'MIXED (两种症状都有)'
        else:
            kind = 'UNKNOWN (查连通性 / 经济性)'
        print(f'  - {mat}  dumping={r["dumping"]:+.6g}  [{kind}]')
        if directly_locked:
            print(f'      自身属性被锁')
        if culprit_locked:
            print(f'      上游属性被锁: {culprit_locked[:5]}')
        if culprit_empty:
            print(f'      上游 property 为空: {culprit_empty[:5]}')
        for blk, case, val, ub in push_cases[:3]:
            print(f'      生产者撞顶: [{blk}].{case} mass_flow={val}=ub')
        if is_push and clocks:
            print(f'      上游 customVariable 硬锁: {clocks[:5]}')


if __name__ == '__main__':
    main()

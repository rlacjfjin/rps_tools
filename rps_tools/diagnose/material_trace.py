"""单物料流通异常诊断。

用法:
    python -m rps_tools.diagnose.material_trace --export-dir export --material <物料code>
    python -m rps_tools.diagnose.material_trace --input X.json --output Y.json --proto Z.txt --material CODE
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from glob import glob

# Windows 默认 GBK 控制台不识别部分 unicode，先强制 utf-8
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

BLOCK_KEYS = ('cduBlock', 'manuBlock', 'blenderBlock', 'mixerBlock',
              'spliterBlock', 'customBlock', 'processBlock')
EPS = 1e-9


def _latest(pattern: str, export_dir: str) -> str | None:
    files = glob(os.path.join(export_dir, pattern))
    return max(files, key=os.path.getmtime) if files else None


def resolve_paths(args) -> tuple[str, str, str]:
    inp = args.input or _latest('*_model_input*.json', args.export_dir)
    out = args.output or _latest('*_model_output*.json', args.export_dir)
    proto = args.proto or _latest('*_proto_expression.txt', args.export_dir)
    missing = [n for n, p in (('input', inp), ('output', out), ('proto', proto)) if not p]
    if missing:
        sys.exit(f'缺少文件: {missing}. 请用 --{"/--".join(missing)} 指定路径，或 --export-dir 指定目录。')
    return inp, out, proto


def load_json(path: str) -> dict:
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def find_material(data: dict, code: str) -> dict | None:
    for m in data.get('materialList', []) or []:
        if m.get('code') == code:
            return m
    return None


def find_block(data: dict, key: str, code: str) -> dict | None:
    for b in data.get(key, []) or []:
        if b.get('code') == code:
            return b
    return None


def _case_mass_flow_from_output(output_data: dict, block_kind: str, block_code: str, case_code: str):
    blk = find_block(output_data, block_kind, block_code)
    if not blk:
        return None
    for c in blk.get('cases', []) or []:
        if c.get('code') == case_code:
            mf = c.get('mass_flow') or {}
            return mf.get('value')
    return None


def _output_material_value(output_data: dict, block_kind: str, block_code: str, mat: str):
    blk = find_block(output_data, block_kind, block_code)
    if not blk:
        return None
    for om in blk.get('output_material', []) or []:
        if om.get('code') == mat:
            return om.get('value')
    return None


def _input_material_value(output_data: dict, block_kind: str, block_code: str, mat: str):
    blk = find_block(output_data, block_kind, block_code)
    if not blk:
        return None
    for im in blk.get('input_material', []) or []:
        if im.get('code') == mat:
            return im.get('value')
    return None


def producers(input_data: dict, output_data: dict, code: str) -> list[tuple[str, str, dict]]:
    """拓扑用 input，数值用 output。返回 [(block_kind, block_code, detail)]."""
    found = []
    for k in BLOCK_KEYS:
        for b in input_data.get(k, []) or []:
            for c in b.get('cases', []) or []:
                for mr in c.get('material_ratio', []) or []:
                    if mr.get('material') == code and mr.get('type') == 'out':
                        found.append((k, b.get('code'), {
                            'case': c.get('code'),
                            'ratio': mr.get('ratio'),
                            'case_mass_flow': _case_mass_flow_from_output(output_data, k, b.get('code'), c.get('code')),
                        }))
            if not b.get('cases'):
                for om in b.get('output_material', []) or []:
                    if om.get('code') == code:
                        found.append((k, b.get('code'), {
                            'output_material_value': _output_material_value(output_data, k, b.get('code'), code),
                        }))
    return found


def consumers(input_data: dict, output_data: dict, code: str) -> list[tuple[str, str, dict]]:
    found = []
    for k in BLOCK_KEYS:
        for b in input_data.get(k, []) or []:
            for c in b.get('cases', []) or []:
                for mr in c.get('material_ratio', []) or []:
                    if mr.get('material') == code and mr.get('type') == 'in':
                        found.append((k, b.get('code'), {
                            'case': c.get('code'),
                            'ratio': mr.get('ratio'),
                            'case_mass_flow': _case_mass_flow_from_output(output_data, k, b.get('code'), c.get('code')),
                        }))
            if not b.get('cases'):
                for im in b.get('input_material', []) or []:
                    if im.get('code') == code:
                        found.append((k, b.get('code'), {
                            'input_material_value': _input_material_value(output_data, k, b.get('code'), code),
                        }))
            # blend_matrix 用 output 里的实际数值
            blk_out = find_block(output_data, k, b.get('code'))
            if blk_out:
                for bm in blk_out.get('blend_matrix', []) or []:
                    if bm.get('input') == code:
                        found.append((k, b.get('code'), {
                            'blend_to': bm.get('output'),
                            'mass_flow': bm.get('mass_flow', 0.0),
                        }))
    return found


# proto_expression 里变量声明的格式：QDENXYZ----lb:0.0\t ub:0.0\t start:0.0\t value:nan
VAR_DECL_RE = re.compile(
    r'^(?P<name>[A-Za-z0-9#\-_()]+)----'
    r'lb:(?P<lb>[^\s]+)\s+ub:(?P<ub>[^\s]+)\s+start:(?P<start>[^\s]+)\s+value:(?P<value>\S+)',
    re.MULTILINE,
)


def index_proto_vars(proto_path: str) -> dict[str, dict]:
    """解析 proto_expression.txt 中所有变量声明，返回 name -> {lb, ub, start, value}."""
    with open(proto_path, 'r', encoding='utf-8') as f:
        text = f.read()
    out = {}
    for m in VAR_DECL_RE.finditer(text):
        name = m.group('name')
        out[name] = {k: m.group(k) for k in ('lb', 'ub', 'start', 'value')}
    return out


def _to_float(s: str) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def is_locked_zero(var: dict) -> bool:
    lb = _to_float(var.get('lb'))
    ub = _to_float(var.get('ub'))
    return lb is not None and ub is not None and abs(lb) < EPS and abs(ub) < EPS


def find_property_vars(var_index: dict, material_code: str) -> list[tuple[str, str, dict]]:
    """找该物料的所有 Q<prop><code> 变量。返回 [(prop_name, var_name, var_info)]."""
    suffix = material_code
    found = []
    for name, info in var_index.items():
        # 变量名形如 QDEN<material> / QSUL<material> 等
        if not name.startswith('Q'):
            continue
        if not name.endswith(suffix):
            continue
        prop = name[1:-len(suffix)]
        if prop and prop.isupper():
            found.append((prop, name, info))
    return found


def upstream_property_sources(input_data: dict, material_code: str) -> list[dict]:
    """从 input_data 找哪些 block 通过 property_transfer 或 spliter property_set
    把上游属性传到当前物料。返回 [{'block','from','prop','coef','const'}]."""
    sources = []
    for k in ('manuBlock', 'processBlock'):
        for b in input_data.get(k, []) or []:
            for pt in b.get('property_transfer', []) or []:
                if pt.get('out_material') == material_code:
                    sources.append({
                        'via': f'{k}.property_transfer',
                        'block': b.get('code'),
                        'from': pt.get('in_material'),
                        'prop': pt.get('prop'),
                        'coef': pt.get('coefficient'),
                        'const': pt.get('constant'),
                    })
    for b in input_data.get('spliterBlock', []) or []:
        outs = [om.get('code') for om in b.get('output_material', []) or []]
        ins = [im.get('code') for im in b.get('input_material', []) or []]
        if material_code in outs:
            ps = b.get('property_set', []) or []
            for prop in ps:
                for src in ins:
                    sources.append({
                        'via': 'spliterBlock.property_set',
                        'block': b.get('code'),
                        'from': src,
                        'prop': prop if isinstance(prop, str) else prop.get('code'),
                        'coef': 'mass-weighted blend',
                        'const': None,
                    })
    return sources


def material_property_state(input_data: dict, code: str) -> list[dict]:
    m = find_material(input_data, code)
    if not m:
        return []
    out = []
    for p in m.get('property', []) or []:
        out.append({
            'code': p.get('code'),
            'start': p.get('start'),
            'lb': p.get('lb'),
            'ub': p.get('ub'),
            'prop_status': p.get('prop_status'),
        })
    return out


# ------- 报告打印 -------

def fmt(v):
    if v is None:
        return 'None'
    if isinstance(v, float):
        return f'{v:.6g}'
    return str(v)


def print_section(title: str):
    print()
    print('=' * 72)
    print(title)
    print('=' * 72)


def detect_push_pattern(material: str, input_data: dict, output_data: dict) -> dict:
    """Pattern G: 检测物料是否因上游 case 撞 capacity ub 而被强 push 出来。
    返回 {'push_cases': [...], 'downstream_bottleneck': [...]}.
    """
    push_cases = []
    # 找该物料的所有"out" 生产者 case，看它在 output 里 mass_flow.value 是否 == capacity.ub
    for k in BLOCK_KEYS:
        for b in input_data.get(k, []) or []:
            for c in b.get('cases', []) or []:
                produces = any(mr.get('material') == material and mr.get('type') == 'out'
                               for mr in c.get('material_ratio', []) or [])
                if not produces:
                    continue
                # 取该 case 在 output 里的实际 mass_flow.value
                blk_out = find_block(output_data, k, b.get('code'))
                if not blk_out:
                    continue
                case_value = None
                for co in blk_out.get('cases', []) or []:
                    if co.get('code') == c.get('code'):
                        case_value = (co.get('mass_flow') or {}).get('value')
                        break
                # 取该 block 在 output 里的 mass_flow_capacity 的 ub 和 value
                for mc in blk_out.get('mass_flow_capacity', []) or []:
                    if c.get('code') in (mc.get('association') or []):
                        ub = mc.get('ub')
                        cap_val = mc.get('value')
                        if ub is not None and cap_val is not None and abs(cap_val - ub) < 1e-6 and ub > 1e-9:
                            ratio = next((mr.get('ratio') for mr in c.get('material_ratio', [])
                                          if mr.get('material') == material and mr.get('type') == 'out'), None)
                            push_cases.append({
                                'block_kind': k,
                                'block': b.get('code'),
                                'case': c.get('code'),
                                'cap_code': mc.get('code'),
                                'cap_ub': ub,
                                'cap_value': cap_val,
                                'case_mass_flow': case_value,
                                'ratio_to_target': ratio,
                                'produced': (case_value or 0) * (ratio or 0),
                            })
    # 下游瓶颈：spliter 输出在 output 是否撞 sell.value=sell.ub？暂时不深查，留个 hook。
    downstream_bottleneck = []
    return {'push_cases': push_cases, 'downstream_bottleneck': downstream_bottleneck}


def detect_custom_lock(input_data: dict, var_index: dict) -> list[dict]:
    """看 customVariable 里 lb==ub 的（硬锁），并报告它出现在哪些 customConstraint 里。"""
    locked = []
    for v in input_data.get('customVariable', []) or []:
        lb, ub = v.get('lb'), v.get('ub')
        if lb is not None and ub is not None and abs(float(lb) - float(ub)) < 1e-9 and abs(float(lb)) > 1e-9:
            locked.append({'code': v.get('code'), 'value': lb, 'used_in': []})
    # 找它出现在哪些 customConstraint
    for v in locked:
        for cc in input_data.get('customConstraint', []) or []:
            if v['code'] in (cc.get('expr') or ''):
                v['used_in'].append(cc.get('code'))
    return locked


def diagnose(material: str, input_data: dict, output_data: dict, var_index: dict, max_depth: int = 3):
    print(f'\n>>> 诊断物料: {material}')

    # 准则 1: dumping
    print_section('[1] 物料平衡 (dumping)')
    m_in = find_material(input_data, material)
    m_out = find_material(output_data, material)
    if not m_in and not m_out:
        print(f'物料 {material} 在 input 和 output 中都找不到。')
        return
    if m_out:
        dumping = m_out.get('dumping')
        mb = m_out.get('marginal_benefit')
        print(f'dumping = {fmt(dumping)}    (≠0 表示产-耗不平衡)')
        print(f'marginal_benefit = {fmt(mb)}')
        if dumping is not None and abs(dumping) > EPS:
            sign = '产>耗（多余被甩出）' if dumping < 0 else '耗>产（凭空消耗）'
            print(f'  ⚠ 平衡异常: {sign}')
    if m_in:
        print(f'input 中 type={m_in.get("type")} type_small={m_in.get("type_small")} '
              f'sell={m_in.get("sell")} purchase={m_in.get("purchase")}')

    # 准则 2: 拓扑
    print_section('[2] 生产/消费 拓扑（拓扑取自 input，数值取自 output）')
    prods = producers(input_data, output_data, material)
    cons = consumers(input_data, output_data, material)
    print(f'-- 生产者 ({len(prods)}) --')
    for k, code, d in prods:
        print(f'  {k}[{code}]  {d}')
    print(f'-- 消费者 ({len(cons)}) --')
    for k, code, d in cons:
        print(f'  {k}[{code}]  {d}')

    # 准则 3: 连通性 + value 是否全 0
    print_section('[3] 下游连通性 & 各路径实际流量')
    blender_consumers = [(k, code, d) for k, code, d in cons if 'blend_to' in d]
    if blender_consumers:
        by_blender = defaultdict(list)
        for k, code, d in blender_consumers:
            by_blender[code].append(d)
        for blender, paths in by_blender.items():
            zero_paths = [p for p in paths if abs(p.get('mass_flow') or 0) < EPS]
            print(f'  blender [{blender}]: 允许 {len(paths)} 条路径，其中 {len(zero_paths)} 条流量为 0')
            for p in paths:
                print(f'    -> {p["blend_to"]:<25} mass_flow={fmt(p["mass_flow"])}')
            # 看这些 blend_to 产品自身在 output 是不是也都 0
            blk = find_block(output_data, 'blenderBlock', blender)
            if blk:
                out_demand = {om.get('code'): om.get('value') for om in blk.get('output_material', []) or []}
                referenced = {p['blend_to'] for p in paths}
                zero_demand = [c for c in referenced if abs(out_demand.get(c) or 0) < EPS]
                if len(zero_demand) == len(referenced):
                    print(f'    ⚠ 所有允许的下游产品在解里都为 0 — 没有需求拉动')
                else:
                    nonzero = {c: out_demand.get(c) for c in referenced if abs(out_demand.get(c) or 0) >= EPS}
                    print(f'    有需求的下游产品: {nonzero}')

    # 准则 4: property_limit 卡硫等
    print_section('[4] property_limit 检查（混合机配方质量约束）')
    for k, code, d in blender_consumers:
        blk = find_block(output_data, 'blenderBlock', code)
        if not blk:
            continue
        relevant_outs = {p.get('blend_to') for p in [d] if 'blend_to' in p}
        for pl in blk.get('property_limit', []) or []:
            if pl.get('material') in {dd.get('blend_to') for _, _, dd in blender_consumers if dd.get('blend_to')}:
                print(f'  [{code}] {pl.get("material")} {pl.get("prop")}: '
                      f'lb={fmt(pl.get("lb"))} ub={fmt(pl.get("ub"))} value={fmt(pl.get("value"))}')
        break  # 已经枚举完所有blender产品的限制了

    # 准则 5: 属性变量是否被锁
    print_section('[5] LP 属性变量状态（来自 proto_expression）')
    props = find_property_vars(var_index, material)
    if not props:
        print(f'  在 proto 中找不到 Q*{material} 形式的变量。')
    locked = []
    for prop, name, info in sorted(props):
        flag = ''
        if is_locked_zero(info):
            flag = '  ⚠ 被锁成 0 (lb=ub=0)'
            locked.append(prop)
        elif _to_float(info['lb']) is not None and _to_float(info['ub']) is not None \
                and _to_float(info['lb']) == _to_float(info['ub']):
            flag = f"  ⚠ 被锁成常量 {info['lb']}"
            locked.append(prop)
        print(f'  {name:<40} lb={info["lb"]:<8} ub={info["ub"]:<8} start={info["start"]:<8} value={info["value"]}{flag}')

    # 准则 6: 上游属性来源
    print_section('[6] 上游属性来源（追根因）')
    sources = upstream_property_sources(input_data, material)
    if not sources:
        print('  该物料没有 property_transfer / spliter property_set 入口；属性靠自身 materialList.property 提供。')
        print('  本物料的 property 数据:')
        for p in material_property_state(input_data, material):
            empty_flag = '  ⚠ start=0 且 prop_status=4 → 可能被锁为常量 0' \
                if p.get('start') == 0 and p.get('prop_status') == 4 else ''
            print(f'    {p["code"]}: start={fmt(p["start"])} lb={fmt(p["lb"])} ub={fmt(p["ub"])} '
                  f'prop_status={p["prop_status"]}{empty_flag}')
    else:
        for s in sources:
            print(f'  via {s["via"]}  block={s["block"]}  from={s["from"]}  prop={s["prop"]}  '
                  f'coef={s["coef"]}  const={s["const"]}')

    # 准则 6 续：递归追上游
    # 即使本物料的 Q 变量没被锁成 0，只要本物料属性来源于上游 property_transfer，
    # 也要追到上游，看上游 Q<prop> 是不是被锁——上游被锁会通过 transfer 代入成 0。
    should_recurse = bool(locked) or bool(sources)
    if should_recurse and max_depth > 0:
        print_section(f'[6+] 递归追溯上游 (深度={max_depth})')
        upstream_seen = {material}
        queue = []
        for s in sources:
            up = s.get('from')
            if up and up not in upstream_seen:
                queue.append((up, 1))
                upstream_seen.add(up)
        while queue:
            up, depth = queue.pop(0)
            if depth > max_depth:
                continue
            print(f'\n  --- 上游物料 [{up}] (深度 {depth}) ---')
            up_props = material_property_state(input_data, up)
            # 找该上游物料下游有哪些 spliter/mixer 想消费它的属性（property_set 列了 prop 但本物料没数据）
            triggered_blocks = []
            for k in ('spliterBlock', 'mixerBlock'):
                for b in input_data.get(k, []) or []:
                    ps_codes = []
                    for p in b.get('property_set', []) or []:
                        ps_codes.append(p if isinstance(p, str) else p.get('code'))
                    has_input = any(im.get('code') == up
                                    for im in b.get('input_material', []) or [])
                    if has_input and ps_codes:
                        triggered_blocks.append((b.get('code'), b.get('name'), ps_codes))
            if not up_props:
                # 不是 hand-wavy 的"极可能"，而是引用具体证据
                if triggered_blocks:
                    print(f'    >> {up} 的 materialList.property 为空 []')
                    for bcode, bname, ps in triggered_blocks:
                        print(f'    >> 下游 spliter/mixer [{bcode}] 的 property_set={ps} 要求 {up} 提供这些属性')
                    print(f'    >> 触发 model/block/spliter.py:382 inavail_prop 分支 →')
                    print(f'       update_prop_status_by_basi 把这些性质标为 Constant →')
                    print(f'       LP 变量按 start 值 lb=ub 锁死')
                    needed_props = sorted({p for _, _, ps in triggered_blocks for p in ps if p})
                    print(f'    => 这就是 dumping/dead-side 的根因。可选修复方案:')
                    print(f'       (A) 治本: 给 {up}.materialList.property 补上 {needed_props} 的真实数据')
                    print(f'       (B) 治标: 在 {material}（或链路上某中间料）的 property 里把这些性质')
                    print(f'           的 start 改成**非零**值 — Constant 状态不变但锁的值≠0，WBAL 项不再死')
                else:
                    print(f'    >> {up} 的 property 数组为空，但未被任何 spliter/mixer property_set 引用 — '
                          f'可能不是直接根因，继续向上追。')
            else:
                for p in up_props:
                    flag = ''
                    if p.get('start') == 0 and p.get('prop_status') == 4:
                        flag = '  ⚠ start=0 prop_status=4'
                    print(f'    {p["code"]}: start={fmt(p["start"])} lb={fmt(p["lb"])} '
                          f'ub={fmt(p["ub"])} prop_status={p["prop_status"]}{flag}')
            # 看上游变量是不是也被锁
            up_vars = find_property_vars(var_index, up)
            for prop, name, info in up_vars:
                if is_locked_zero(info):
                    print(f'    Q{prop}{up} 在 LP 中被锁成 0 (lb=ub=0)')
            # 继续向上
            up_sources = upstream_property_sources(input_data, up)
            for s in up_sources:
                nxt = s.get('from')
                if nxt and nxt not in upstream_seen:
                    queue.append((nxt, depth + 1))
                    upstream_seen.add(nxt)

    # 准则 7: push 型检测（上游 case 撞 capacity ub）
    print_section('[7] Push 型甩货检测（生产者 case 是否撞 capacity 上界）')
    push = detect_push_pattern(material, input_data, output_data)
    if not push['push_cases']:
        print('  生产者 case 都没撞容量上界 → 不是 push 型甩货。')
    else:
        print(f'  发现 {len(push["push_cases"])} 个生产者 case 撞 capacity ub：')
        for pc in push['push_cases']:
            print(f'  ⚠ {pc["block_kind"]}[{pc["block"]}] case={pc["case"]}: '
                  f'mass_flow={pc["case_mass_flow"]} (capacity {pc["cap_code"]} ub={pc["cap_ub"]} 撞顶)')
            print(f'      生产本物料 = {pc["case_mass_flow"]} × {pc["ratio_to_target"]} = {pc["produced"]:.4f}')
        # 关联 customVariable 硬锁
        clocks = detect_custom_lock(input_data, var_index)
        if clocks:
            print()
            print('  上游可能存在 customVariable 硬锁推动产能：')
            for cl in clocks[:10]:
                print(f'    {cl["code"]} = {cl["value"]}（lb=ub） → 出现于 customConstraint: {cl["used_in"]}')
            print('  Fix 方向: (a) 解除 customVariable 的 lb=ub 锁；(b) 放宽下游 sell 上限；'
                  '(c) 改对应 customConstraint con_type 为不等式（≤ 而非 ==）。')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--export-dir', default='export')
    ap.add_argument('--input', help='model_input json path')
    ap.add_argument('--output', help='model_output json path')
    ap.add_argument('--proto', help='proto_expression txt path')
    ap.add_argument('--material', required=True, help='要诊断的物料 code')
    ap.add_argument('--depth', type=int, default=3, help='上游递归深度')
    args = ap.parse_args(argv)

    inp_path, out_path, proto_path = resolve_paths(args)
    print(f'input : {inp_path}')
    print(f'output: {out_path}')
    print(f'proto : {proto_path}')

    input_data = load_json(inp_path)
    output_data = load_json(out_path)
    var_index = index_proto_vars(proto_path)
    print(f'proto 变量索引: {len(var_index)} 条')

    diagnose(args.material, input_data, output_data, var_index, max_depth=args.depth)


if __name__ == '__main__':
    main()

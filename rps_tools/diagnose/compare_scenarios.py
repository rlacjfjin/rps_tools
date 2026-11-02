"""双场景对比 + 路径价值 + OBJ 分解 + 敏感性分析。

把"为什么这次 LP 跟上次不一样"的分析方法论自动化。

用法:
    python -m rps_tools.diagnose.compare_scenarios \
        --before export/总流程_model_output2026-05-27-09-05-59.json \
        --after  export/总流程_model_output2026-05-27-09-05-22.json

    可选: --material <code>   聚焦某个物料
          --top 15            敏感性排行 top N
          --dump-threshold 1e-4  视为甩货的阈值

会自动从同目录找对应的 input.json (用 _model_input 替换 _model_output)。
"""
from __future__ import annotations
import argparse
import json
import os
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
BLOCK_KEYS = ('cduBlock', 'manuBlock', 'blenderBlock', 'mixerBlock',
              'spliterBlock', 'customBlock', 'processBlock')


def fnum(v, prec=4):
    if v is None:
        return 'None'
    if isinstance(v, float):
        if abs(v) >= 1000:
            return f'{v:,.{prec-2}f}'
        return f'{v:.{prec}f}'
    return str(v)


def fmt_signed(v):
    if v is None:
        return 'None'
    sign = '+' if v >= 0 else ''
    return f'{sign}{v:,.4f}' if abs(v) < 1000 else f'{sign}{v:,.2f}'


def section(title):
    print()
    print('=' * 80)
    print(title)
    print('=' * 80)


# ---------- 加载 ----------

def load_pair(out_path, override_in=None):
    in_path = override_in
    # 1) 文件名直接匹配
    if not in_path:
        candidate = out_path.replace('_model_output', '_model_input')
        if os.path.exists(candidate):
            in_path = candidate
    # 2) 同目录里 mtime ≤ output 的最新 input
    if not in_path:
        d = os.path.dirname(out_path) or '.'
        out_mtime = os.path.getmtime(out_path)
        candidates = []
        for f in os.listdir(d):
            if '_model_input' in f and f.endswith('.json'):
                p = os.path.join(d, f)
                t = os.path.getmtime(p)
                if t <= out_mtime + 60:  # 容忍 60s
                    candidates.append((t, p))
        if candidates:
            candidates.sort()
            # 取最接近 out_mtime 的（即最大 t ≤ out_mtime+60）
            in_path = candidates[-1][1]
            print(f'  (input 自动匹配: {os.path.basename(in_path)})')
    if not in_path:
        print(f'  ⚠ 没找到任何匹配的 input 文件，路径价值分析会降级。')

    with open(out_path, 'r', encoding='utf-8') as f:
        out = json.load(f)
    inp = None
    if in_path:
        with open(in_path, 'r', encoding='utf-8') as f:
            inp = json.load(f)
    return inp, out


def get_material(d, code):
    for m in d.get('materialList', []) or []:
        if m.get('code') == code:
            return m
    return None


def get_cvar(d, code):
    for v in d.get('customVariable', []) or []:
        if v.get('code') == code:
            return v
    return None


def get_case(d, block_kind, block_code, case_code):
    for b in d.get(block_kind, []) or []:
        if b.get('code') == block_code:
            for c in b.get('cases', []) or []:
                if c.get('code') == case_code:
                    return b, c
    return None, None


# ---------- [1] OBJ Δ ----------

def section_obj(before, after):
    section('[1] OBJ Δ')
    ob, oa = before.get('OBJ'), after.get('OBJ')
    delta = (oa or 0) - (ob or 0)
    print(f'  before OBJ = {fnum(ob, 2)}')
    print(f'  after  OBJ = {fnum(oa, 2)}')
    print(f'  Δ          = {fmt_signed(delta)}')
    return delta


# ---------- [2] customVariable 改动 ----------

def section_cvar(before, after):
    section('[2] customVariable 改动（lb/ub/value 变化）')
    by_code = {}
    for d, tag in [(before, 'b'), (after, 'a')]:
        for v in d.get('customVariable', []) or []:
            by_code.setdefault(v.get('code'), {})[tag] = v
    changed = []
    for code, pair in by_code.items():
        b, a = pair.get('b'), pair.get('a')
        if not b or not a:
            changed.append((code, b, a, 'added/removed'))
            continue

        def feq(x, y, eps=1e-6):
            if x is None and y is None:
                return True
            if x is None or y is None:
                return False
            try:
                return abs(float(x) - float(y)) < eps
            except (TypeError, ValueError):
                return x == y
        if not (feq(b.get('lb'), a.get('lb'))
                and feq(b.get('ub'), a.get('ub'))
                and feq(b.get('value'), a.get('value'))):
            changed.append((code, b, a, ''))
    if not changed:
        print('  无变化。')
        return []
    for code, b, a, note in changed:
        print(f'  - {code}:')
        if b and a:
            print(f'      bounds: [{b.get("lb")}, {b.get("ub")}] → [{a.get("lb")}, {a.get("ub")}]')
            print(f'      value : {fnum(b.get("value"))} → {fnum(a.get("value"))}')
            if (b.get('lb') == b.get('ub')) and not (a.get('lb') == a.get('ub')):
                print(f'      ★ 从硬锁状态解除')
            elif not (b.get('lb') == b.get('ub')) and (a.get('lb') == a.get('ub')):
                print(f'      ★ 变成硬锁')
        else:
            print(f'      {note}')
    return changed


# ---------- [3] 物料状态翻转 ----------

def section_material_flips(before, after, dump_threshold=1e-4):
    section('[3] 物料状态翻转（dumping / sell.value / marginal_benefit）')
    by_code = {}
    for d, tag in [(before, 'b'), (after, 'a')]:
        for m in d.get('materialList', []) or []:
            by_code.setdefault(m.get('code'), {})[tag] = m

    flips = {'dump_clear': [], 'dump_new': [], 'sell_jump': [], 'mb_jump': []}
    for code, pair in by_code.items():
        mb = pair.get('b') or {}
        ma = pair.get('a') or {}
        db = mb.get('dumping') or 0
        da = ma.get('dumping') or 0
        if abs(db) >= dump_threshold and abs(da) < dump_threshold:
            flips['dump_clear'].append((code, db, da))
        elif abs(db) < dump_threshold and abs(da) >= dump_threshold:
            flips['dump_new'].append((code, db, da))
        sell_b = (mb.get('sell') or {}).get('value') or 0
        sell_a = (ma.get('sell') or {}).get('value') or 0
        if abs(sell_a - sell_b) > 0.01:
            flips['sell_jump'].append((code, sell_b, sell_a, sell_a - sell_b))
        mbb = mb.get('marginal_benefit') or 0
        mba = ma.get('marginal_benefit') or 0
        if abs(mba - mbb) > 100:  # 阈值过滤微抖
            flips['mb_jump'].append((code, mbb, mba, mba - mbb))

    if flips['dump_clear']:
        print(f'  ★ 甩货被消除: {len(flips["dump_clear"])} 个')
        for code, db, da in sorted(flips['dump_clear'], key=lambda x: abs(x[1]), reverse=True)[:10]:
            print(f'      {code:<28}  dumping {db:+.4f} → {da:+.4f}')
    if flips['dump_new']:
        print(f'  ⚠ 新增甩货: {len(flips["dump_new"])} 个')
        for code, db, da in sorted(flips['dump_new'], key=lambda x: abs(x[2]), reverse=True)[:10]:
            print(f'      {code:<28}  dumping {db:+.4f} → {da:+.4f}')
    if flips['sell_jump']:
        flips['sell_jump'].sort(key=lambda x: abs(x[3]), reverse=True)
        print(f'\n  销量大变 (|Δ|>0.01): {len(flips["sell_jump"])} 个')
        for code, sb, sa, ds in flips['sell_jump'][:15]:
            print(f'      {code:<28}  sell.value {fnum(sb)} → {fnum(sa)}  Δ={fmt_signed(ds)}')
    if flips['mb_jump']:
        flips['mb_jump'].sort(key=lambda x: abs(x[3]), reverse=True)
        print(f'\n  marginal_benefit 大变 (|Δ|>100): {len(flips["mb_jump"])} 个')
        for code, mbb, mba, dmb in flips['mb_jump'][:15]:
            print(f'      {code:<28}  mb {fnum(mbb)} → {fnum(mba)}  Δ={fmt_signed(dmb)}')
    return flips


# ---------- [4] case 容量翻转 ----------

def section_case_flips(before, after):
    section('[4] case 容量翻转（撞顶 ↔ 退离）')
    # 取 case mass_flow + 对应 capacity 状态
    def collect(d):
        rows = {}
        for k in BLOCK_KEYS:
            for b in d.get(k, []) or []:
                # 把 capacity_ub 按 case 反查
                cap_ub_by_case = {}
                for mc in b.get('mass_flow_capacity', []) or []:
                    assoc = mc.get('association') or []
                    for case_code in assoc:
                        # 一个 case 可能关联多个 capacity；记录最紧的 ub
                        prev = cap_ub_by_case.get(case_code)
                        if mc.get('ub') is not None and (prev is None or mc['ub'] < prev[0]):
                            cap_ub_by_case[case_code] = (mc['ub'], mc.get('value'), mc.get('code'))
                for c in b.get('cases', []) or []:
                    mf = (c.get('mass_flow') or {}).get('value')
                    if mf is None:
                        continue
                    cap = cap_ub_by_case.get(c.get('code'))
                    rows[(k, b.get('code'), c.get('code'))] = (mf, cap)
        return rows
    rb = collect(before)
    ra = collect(after)
    keys = set(rb) | set(ra)
    transitions = []
    for key in keys:
        b = rb.get(key)
        a = ra.get(key)
        if not b or not a:
            continue
        mf_b, cap_b = b
        mf_a, cap_a = a
        ub_b = cap_b[0] if cap_b else None
        ub_a = cap_a[0] if cap_a else None
        hit_b = ub_b is not None and abs(mf_b - ub_b) < 1e-5 and ub_b > EPS
        hit_a = ub_a is not None and abs(mf_a - ub_a) < 1e-5 and ub_a > EPS
        if hit_b != hit_a or abs(mf_a - mf_b) > 1e-4:
            transitions.append((key, mf_b, mf_a, ub_b, hit_b, hit_a, abs(mf_a - mf_b)))
    transitions.sort(key=lambda x: -x[-1])
    flipped_only = [t for t in transitions if t[4] != t[5]]
    other_changes = [t for t in transitions if t[4] == t[5]]
    if flipped_only:
        print(f'  ★ 撞顶/退离翻转: {len(flipped_only)} 个')
        for (k, blk, c), mfb, mfa, ub, hb, ha, _ in flipped_only[:15]:
            arrow = '撞顶→退离' if hb and not ha else '退离→撞顶'
            print(f'      {k}[{blk}].{c}: mass_flow {fnum(mfb)} → {fnum(mfa)} '
                  f'(ub={fnum(ub)})  ★{arrow}')
    if other_changes:
        print(f'\n  其他大量变化 (|Δmass_flow|>1e-4): {len(other_changes)} 个 (showing top 10)')
        for (k, blk, c), mfb, mfa, ub, hb, ha, _ in other_changes[:10]:
            cap = '撞顶' if ha else ('近顶' if ub and mfa/ub > 0.95 else '')
            print(f'      {k}[{blk}].{c}: {fnum(mfb)} → {fnum(mfa)} (ub={fnum(ub)})  {cap}')
    return transitions


# ---------- [5] 路径价值分析 ----------

def find_paths(material, input_data):
    """返回该物料的所有出口 path: direct sale / 进 case / 进 blender(尚未支持)。"""
    paths = []
    if not input_data:
        return paths
    m = get_material(input_data, material)
    if m:
        sell = m.get('sell') or {}
        if sell.get('price') is not None and (sell.get('ub') is None or sell.get('ub') > 0):
            paths.append({'kind': 'sell', 'price': sell.get('price')})
        purchase = m.get('purchase') or {}
        if purchase.get('price') is not None and (purchase.get('ub') is None or purchase.get('ub') > 0):
            paths.append({'kind': 'purchase', 'price': purchase.get('price')})
    for k in BLOCK_KEYS:
        for b in input_data.get(k, []) or []:
            for c in b.get('cases', []) or []:
                in_ratio = None
                outs = []
                for mr in c.get('material_ratio', []) or []:
                    if mr.get('material') == material and mr.get('type') == 'in':
                        in_ratio = mr.get('ratio')
                    elif mr.get('type') == 'out':
                        outs.append((mr.get('material'), mr.get('ratio')))
                if in_ratio and in_ratio > 1e-9:
                    paths.append({'kind': 'case', 'block_kind': k, 'block': b.get('code'),
                                  'case': c.get('code'), 'in_ratio': in_ratio, 'outs': outs})
    return paths


def compute_V_path(path, output_data):
    if path['kind'] == 'sell':
        return path.get('price') or 0, {}
    if path['kind'] == 'purchase':
        return -(path.get('price') or 0), {}  # 不太用到，作为占位
    if path['kind'] == 'case':
        V = 0
        breakdown = []
        for mat, y in path['outs']:
            m = get_material(output_data, mat)
            mb = (m.get('marginal_benefit') if m else None) or 0
            contrib = (y or 0) * mb
            V += contrib
            breakdown.append({'mat': mat, 'yield': y, 'mb': mb, 'contrib': contrib})
        V_per_input = V / (path['in_ratio'] or 1)
        return V_per_input, breakdown
    return None, {}


def path_is_active(path, output_data):
    """该路径在解里是不是有实际流量？"""
    if path['kind'] == 'sell':
        m = get_material(output_data, path.get('_material') or '')
        # 调用方传入材料 code
        return False
    if path['kind'] == 'case':
        _, c = get_case(output_data, path['block_kind'], path['block'], path['case'])
        if not c:
            return False
        mf = (c.get('mass_flow') or {}).get('value') or 0
        return abs(mf) > 1e-9
    return False


def section_path_analysis(before_in, before_out, after_in, after_out, focus_material=None):
    section('[5] 路径价值分析（V_path 计算 + LP 均衡验证）')

    # 自动找"有多条路径且至少一条 case 路径"的物料
    candidates = set()
    if focus_material:
        candidates.add(focus_material)
    else:
        for ind in (before_in, after_in):
            if not ind:
                continue
            for m in ind.get('materialList', []) or []:
                code = m.get('code')
                paths = find_paths(code, ind)
                has_sale = any(p['kind'] == 'sell' for p in paths)
                has_case = any(p['kind'] == 'case' for p in paths)
                if has_sale and has_case:
                    candidates.add(code)

    if not candidates:
        print('  没找到"既有直销又能进 case"的物料 — 路径分析跳过。')
        return

    print(f'  分析对象: {len(candidates)} 个有多路径的物料\n')

    for mat in sorted(candidates):
        paths_b = find_paths(mat, before_in) if before_in else []
        paths_a = find_paths(mat, after_in) if after_in else []
        if not (paths_b or paths_a):
            continue
        if focus_material and mat != focus_material:
            continue
        # 用 path 的 "fingerprint" 配对 before/after 路径，价格分别用各自 input 里的（处理改价那种情形）
        def fp(p):
            if p['kind'] == 'sell':
                return ('sell',)
            if p['kind'] == 'case':
                return ('case', p['block_kind'], p['block'], p['case'])
            return (p['kind'],)
        paths_a_by_fp = {fp(p): p for p in paths_a}
        paths_b_by_fp = {fp(p): p for p in paths_b}
        all_fps = list(paths_a_by_fp) + [f for f in paths_b_by_fp if f not in paths_a_by_fp]
        paths_pairs = [(paths_b_by_fp.get(f), paths_a_by_fp.get(f)) for f in all_fps]

        # 该物料各路径的实际流量
        def active_flow(path, out_d):
            if path['kind'] == 'sell':
                m = get_material(out_d, mat)
                return (m.get('sell') or {}).get('value') if m else None
            if path['kind'] == 'case':
                _, c = get_case(out_d, path['block_kind'], path['block'], path['case'])
                if not c:
                    return 0
                mf = (c.get('mass_flow') or {}).get('value') or 0
                # 该 case 消耗的本物料 = mf × in_ratio
                return mf * (path.get('in_ratio') or 1)
            return None

        print(f'  物料 [{mat}]:')
        rows = []
        for pb, pa in paths_pairs:
            p_label = pa or pb
            if p_label['kind'] == 'sell':
                price_b = pb.get('price') if pb else '-'
                price_a = pa.get('price') if pa else '-'
                if price_b != price_a:
                    label = f'直销 (price {price_b}→{price_a})'
                else:
                    label = f'直销 (price={price_a})'
            elif p_label['kind'] == 'case':
                label = f'{p_label["block_kind"]}[{p_label["block"]}].{p_label["case"]} (in_ratio={p_label["in_ratio"]})'
            else:
                label = f'其他 {p_label["kind"]}'
            Vb, _ = compute_V_path(pb, before_out) if pb else (None, {})
            Va, _ = compute_V_path(pa, after_out) if pa else (None, {})
            fb = active_flow(pb, before_out) if pb else None
            fa = active_flow(pa, after_out) if pa else None
            rows.append((label, Vb, Va, fb, fa, pa or pb))
        # 打印
        print(f'      {"path":<55} {"V_before":>12} {"V_after":>12} {"flow_b":>10} {"flow_a":>10}')
        for label, Vb, Va, fb, fa, _ in rows:
            print(f'      {label:<55} {fnum(Vb):>12} {fnum(Va):>12} {fnum(fb):>10} {fnum(fa):>10}')

        # LP 均衡: 活跃路径的 V 应该相等（在内点处）
        def margin_summary(label, V_list_with_flow):
            actives = [(lab, V) for lab, V, f in V_list_with_flow if f and f > 1e-6]
            if len(actives) < 2:
                if actives:
                    print(f'        {label}: 只有 1 条活跃路径 ({actives[0][0]}, V={fnum(actives[0][1])})')
                return
            Vs = [V for _, V in actives]
            spread = max(Vs) - min(Vs)
            top = sorted(actives, key=lambda x: -x[1])[0]
            bot = sorted(actives, key=lambda x: x[1])[0]
            print(f'        {label}: {len(actives)} 条活跃路径，V 范围 {fnum(bot[1])}~{fnum(top[1])}，'
                  f'差 {fmt_signed(spread)}')
            if spread > 1:
                print(f'          ★ 差额 = {fnum(spread)} —— 说明 {top[0]} 路径在边际上撞了容量上界，'
                      f'差额是该容量约束的影子价。')
            else:
                print(f'          (差 < 1，符合 LP 内点均衡 V_i = V_j)')

        margin_summary('BEFORE', [(lab, Vb, fb) for lab, Vb, Va, fb, fa, _ in rows])
        margin_summary('AFTER ', [(lab, Va, fa) for lab, Vb, Va, fb, fa, _ in rows])
        print()


# ---------- [6] OBJ Δ 分解（启发式）----------

def section_obj_decomp(before_in, before_out, after_in, after_out, total_delta, flips):
    section('[6] OBJ Δ 分解 (按 material_revenue / material_cost / utility_* 聚合)')

    # 顶层聚合（OBJ = material_revenue + utility_revenue − material_cost − utility_cost − fix_cost）
    mr_b = before_out.get('material_revenue') or 0
    mr_a = after_out.get('material_revenue') or 0
    uc_b = before_out.get('utility_cost') or 0
    uc_a = after_out.get('utility_cost') or 0
    mc_b = before_out.get('material_cost') or 0
    mc_a = after_out.get('material_cost') or 0
    ur_b = before_out.get('utility_revenue') or 0
    ur_a = after_out.get('utility_revenue') or 0
    fc_b = before_out.get('fix_cost') or 0
    fc_a = after_out.get('fix_cost') or 0

    print('  顶层聚合 ΔOBJ:')
    parts = [
        ('material_revenue', mr_a - mr_b),
        ('utility_revenue',  ur_a - ur_b),
        ('material_cost   (取负)', -(mc_a - mc_b)),
        ('utility_cost    (取负)', -(uc_a - uc_b)),
        ('fix_cost        (取负)', -(fc_a - fc_b)),
    ]
    s = 0
    for label, v in parts:
        print(f'    {fmt_signed(v):>14}  Δ {label}')
        s += v
    print(f'    {fmt_signed(s):>14}  合计')
    print(f'    {fmt_signed(total_delta):>14}  实际 OBJ Δ')
    if abs(s - total_delta) > 1:
        print(f'    ⚠ 差 {fmt_signed(s - total_delta)} — 顶层字段可能不完整')
    else:
        print(f'    ✓ 顶层分解完整')

    # 收入 = Σ price × sell.value，成本 = Σ price × purchase.value
    # 用 materialList 明细把 Δmaterial_revenue 进一步拆到具体物料
    by_code = {}
    for d, tag in [(before_out, 'b'), (after_out, 'a')]:
        for m in d.get('materialList', []) or []:
            by_code.setdefault(m.get('code'), {})[tag] = m

    revenue_changes = []  # (code, Δ, sell_b, sell_a, price_b, price_a)
    cost_changes = []
    for code, pair in by_code.items():
        mb = pair.get('b') or {}
        ma = pair.get('a') or {}
        # 销售
        sb = mb.get('sell') or {}
        sa = ma.get('sell') or {}
        rev_b = (sb.get('value') or 0) * (sb.get('price') or 0)
        rev_a = (sa.get('value') or 0) * (sa.get('price') or 0)
        if abs(rev_a - rev_b) > 1:
            revenue_changes.append((code, rev_a - rev_b, sb.get('value'), sa.get('value'),
                                    sb.get('price'), sa.get('price')))
        # 采购（成本）
        pb = mb.get('purchase') or {}
        pa = ma.get('purchase') or {}
        cost_b = (pb.get('value') or 0) * (pb.get('price') or 0)
        cost_a = (pa.get('value') or 0) * (pa.get('price') or 0)
        if abs(cost_a - cost_b) > 1:
            cost_changes.append((code, cost_a - cost_b, pb.get('value'), pa.get('value'),
                                 pb.get('price'), pa.get('price')))

    revenue_changes.sort(key=lambda x: -abs(x[1]))
    cost_changes.sort(key=lambda x: -abs(x[1]))

    rev_sum = sum(x[1] for x in revenue_changes)
    cost_sum = sum(x[1] for x in cost_changes)
    explained = rev_sum - cost_sum

    print(f'\n  Δmaterial_revenue 明细 (showing top 15):')
    for code, d, sb, sa, pb, pa in revenue_changes[:15]:
        price_note = f'price {pb}→{pa}' if pb != pa else f'price={pa}'
        print(f'    {fmt_signed(d):>14}  {code:<30}  sell {fnum(sb)}→{fnum(sa)}  {price_note}')
    rev_residual = (mr_a - mr_b) - rev_sum
    print(f'    {fmt_signed(rev_sum):>14}  上述销售合计')
    print(f'    {fmt_signed(rev_residual):>14}  其他细微销售（汇总差额）')

    print(f'\n  Δmaterial_cost 明细 (purchase, showing top 15):')
    for code, d, sb, sa, pb, pa in cost_changes[:15]:
        price_note = f'price {pb}→{pa}' if pb != pa else f'price={pa}'
        print(f'    {fmt_signed(d):>14}  {code:<30}  purc {fnum(sb)}→{fnum(sa)}  {price_note}')
    cost_residual = (mc_a - mc_b) - cost_sum
    print(f'    {fmt_signed(cost_sum):>14}  上述采购合计')
    print(f'    {fmt_signed(cost_residual):>14}  其他采购成本（PURC* 原油，多在 customVariable 而非 materialList.purchase）')


# ---------- [7] 敏感性 / 拐点 ----------

def section_sensitivity(after_out, top_n=15):
    section('[7] 当前解的敏感性热点（after 状态）')

    # 7a) marginal_benefit 高的物料 — 解开这些约束最赚
    mats = []
    for m in after_out.get('materialList', []) or []:
        mb = m.get('marginal_benefit')
        if mb is not None and abs(mb) > 1:
            mats.append((m.get('code'), mb, m.get('dumping') or 0))
    mats.sort(key=lambda x: -abs(x[1]))
    print(f'  Top {top_n} marginal_benefit (物料层):')
    for code, mb, dump in mats[:top_n]:
        tag = ' ⚠ 仍在甩货' if abs(dump) > 1e-4 else ''
        print(f'    {code:<32} mb={fnum(mb):>14}{tag}')

    # 7b) 撞顶 capacity / mass_flow_capacity
    binds = []
    for k in BLOCK_KEYS:
        for b in after_out.get(k, []) or []:
            for mc in b.get('mass_flow_capacity', []) or []:
                v, ub = mc.get('value'), mc.get('ub')
                mb = mc.get('marginal_benefit')
                if v is not None and ub is not None and abs(v - ub) < 1e-5 and ub > EPS:
                    binds.append((k, b.get('code'), mc.get('code'), ub, mb))
    binds.sort(key=lambda x: -(abs(x[4]) if x[4] else 0))
    print(f'\n  Top {top_n} 撞顶容量约束 (block.capacity)：按影子价排序')
    for k, blk, code, ub, mb in binds[:top_n]:
        if mb is not None and abs(mb) > 0.1:
            print(f'    {k}[{blk}].{code:<25} ub={fnum(ub):<10} 影子价 mb={fnum(mb):>14}')

    # 7c) customVariable 硬锁的
    print(f'\n  硬锁的 customVariable (lb=ub)：')
    for v in after_out.get('customVariable', []) or []:
        lb, ub = v.get('lb'), v.get('ub')
        if lb is not None and ub is not None and abs(float(lb) - float(ub)) < 1e-9 and abs(float(lb)) > EPS:
            mb = v.get('marginal_benefit')
            print(f'    {v.get("code"):<32} = {lb}  marginal_benefit={fnum(mb)}')


# ---------- 主流程 ----------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--before', required=True, help='before model_output json')
    ap.add_argument('--after', required=True, help='after model_output json')
    ap.add_argument('--before-in', help='手动指定 before 的 input json')
    ap.add_argument('--after-in', help='手动指定 after 的 input json')
    ap.add_argument('--material', help='聚焦某物料 (路径分析时只显示该物料)')
    ap.add_argument('--top', type=int, default=15)
    ap.add_argument('--dump-threshold', type=float, default=1e-4)
    args = ap.parse_args(argv)

    print(f'before: {args.before}')
    print(f'after : {args.after}')
    before_in, before_out = load_pair(args.before, getattr(args, 'before_in', None))
    after_in, after_out = load_pair(args.after, getattr(args, 'after_in', None))

    total_delta = section_obj(before_out, after_out)
    section_cvar(before_out, after_out)
    flips = section_material_flips(before_out, after_out, args.dump_threshold)
    section_case_flips(before_out, after_out)
    section_path_analysis(before_in, before_out, after_in, after_out, focus_material=args.material)
    section_obj_decomp(before_in, before_out, after_in, after_out, total_delta, flips)
    section_sensitivity(after_out, top_n=args.top)


if __name__ == '__main__':
    main()

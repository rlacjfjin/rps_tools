"""Offline orchestration. Original result fields are references, never decisions."""
import copy
import math

from .adapters import load_artifacts, model_from_dict, proto_from_dict
from .core import detect_infeasible
from .execution import capture_failure_residuals
from .property_bounds import attach_property_bound_hints
from .snapshot import failure_input_context
from .static import analyze_input

SUCCESS = (1, 4, 25, 26)
SKIP_FAILURE = (1, 3, 4, 7, 9, 10, 25, 26)


def successful(output):
    return (output.get('solver_status_code') in SUCCESS
            and output.get('solution_available') is not False
            and not (output.get('solve_diagnostics') or {}).get('failed'))


def failure_eligible(output):
    diag = output.get('solve_diagnostics') or {}
    return (output.get('solver_status_code') not in SKIP_FAILURE
            and output.get('solution_available') is not True and not diag.get('solution_source')
            and (diag.get('failed') is True or output.get('solver_status_code') in (2, 20)))


def _proof(snapshot, input_data, limit):
    if not isinstance(snapshot.get('bounds'), dict) or not isinstance(snapshot.get('rows'), list):
        raise ValueError('证明检查需要原始模型的 bounds/rows 快照；旧 MPProto dump 缺失的变量类型不能自动猜测')
    return detect_infeasible(snapshot, input_data, time_limit=limit, use_lp=False)


def analyze(mode, params):
    data, provenance, warnings = load_artifacts(params)
    raw, output = data.get('input', {}), data.get('output', {})
    limit = float(params.get('time_limit') or 10)
    tolerance = float(params.get('tolerance') or 1e-6)
    if not math.isfinite(limit) or not 0 < limit <= 300:
        raise ValueError('诊断时间预算需要在 0～300 秒之间')
    if not math.isfinite(tolerance) or tolerance <= 0:
        raise ValueError('残差展示阈值必须为正的有限数')
    report = dict(schema_version=1, mode=mode, provenance=provenance, warnings=warnings,
                  extra_solver_calls=0, auxiliary_lp_calls=0,
                  confirmed_conflicts=[], suspected_constraints=[], input_hints=[],
                  limitations=['没有发现冲突不代表整个模型可行；诊断不修改原求解状态。'])
    report['solver_reference'] = {k:copy.deepcopy(output[k]) for k in
        ('solver_status_code','solver_status','solution_available','OBJ','stable','multi_start_obj','multi_start_stability') if k in output}
    if mode == 'property_bounds':
        if not successful(output):
            report['scope'] = 'skipped_non_success'
            report['warnings'].append('该结果不满足成功结果条件，不读取其性质或回退值。')
            return report
        existing = output.get('property_bound_diagnostics')
        if isinstance(existing, dict) and all(isinstance(existing.get(k), list) for k in ('binding_properties','fixed_properties')):
            report['property_bound_diagnostics'] = copy.deepcopy(existing)
            report['scope'] = 'embedded_report'
            return report
        if not data.get('model'):
            report['scope'] = 'insufficient_evidence'
            report['warnings'].append('旧 output 缺少性质卡边证据；需要同次求解后模型的实际值、有效边界和边际来源。')
            return report
        model = model_from_dict(data['model'], output, raw)
        result = copy.deepcopy(output)
        attach_property_bound_hints(model, result, raw)
        if 'property_bound_diagnostics' in result:
            report['property_bound_diagnostics'] = result['property_bound_diagnostics']
            report['scope'] = 'saved_solution_model'
            report['limitations'].append('模型文件按显式选择的求解后导出解释；不使用初值或补算性质。缺失 basis 的物料跳过。')
        else:
            report['scope'] = 'insufficient_evidence'
            report['warnings'].append('求解后模型信息不足，未能生成性质提示。')
        return report

    if mode == 'failure_analysis' and not failure_eligible(output):
        report['scope'] = 'skipped_non_failure'
        report['warnings'].append('成功、可行检查点、无界或终止原因未知的结果不进入失败残差归因。')
        return report
    if raw:
        static = analyze_input(raw, limit)
        report['input_hints'] = static['findings']
        report['input_scans'] = static['scans']
        report['warnings'].extend(static['warnings'])
    if data.get('snapshot'):
        proof = _proof(data['snapshot'], raw, limit)
        report['model_proof'] = proof
        report['confirmed_conflicts'] = proof['findings']
        if proof['status'] in ('inconclusive', 'invalid_model'):
            report['warnings'].append(f"模型冲突检查没有给出结论（{proof['status']}），已复核冲突数为 0 不代表没有冲突。")
    report['scope'] = 'raw_input_and_optional_snapshot'
    if mode == 'input_feasibility':
        return report
    prior = output.get('infeasibility_diagnostics') or {}
    embedded = prior.get('failure_analysis')
    if isinstance(embedded, dict):
        report['embedded_failure_analysis'] = copy.deepcopy(embedded)
        # Keep origin visible: imported proofs are not silently re-certified.
        report['reported_conflicts'] = copy.deepcopy(embedded.get('confirmed_conflicts') or [])
        report['suspected_constraints'] = copy.deepcopy(embedded.get('suspected_constraints') or [])
        report['evidence'] = copy.deepcopy(embedded.get('evidence') or {})
        report['scope'] = 'embedded_report'
        return report
    if prior.get('findings'):
        report['reported_conflicts'] = copy.deepcopy(prior['findings'])
    model_data = data.get('model') or data.get('snapshot')
    if not model_data:
        report['warnings'].append('缺少同次模型行定义，不能把原生残差可靠关联到入参。')
        report['scope'] = 'insufficient_evidence'
        return report
    proto = proto_from_dict(model_data)
    proto.solve_diagnostics = dict(failed=True, solution_source=None)
    row_map = data.get('row_map')
    if row_map is not None:
        row_map = row_map.get('row_map', row_map)
        if not isinstance(row_map, dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in row_map.items()):
            raise ValueError('row_map 必须为原生行名到原模型 code 的字符串字典')
    native = copy.deepcopy(data.get('native') or {})
    if len(native) == 1 and isinstance(next(iter(native.values())), dict):
        candidate = next(iter(native.values()))
        if 'equ' in candidate or 'sol' in candidate:
            native = candidate
    if native.get('equ') and native.get('scaled') is not False:
        native.pop('equ')
        report['warnings'].append('原生结果没有明确 scaled=false，活动值不作为原始空间证据。')
    capture_failure_residuals(proto, native, row_map or {}, row_map is not None,
                              data.get('trace'), tolerance)
    evidence = dict(getattr(proto, '_failure_residuals', {}))
    rows = evidence.pop('rows', [])
    context = failure_input_context(proto, [r['code'] for r in rows], raw)
    report['suspected_constraints'] = [dict(r, confidence='suspected', **context[r['code']],
        recommendation='核对关联上下限、物性、分离/递推系数及方案开关；残差不是应放宽的幅度。') for r in rows]
    report['evidence'] = evidence
    report['scope'] = 'saved_native_evidence'
    if not rows:
        report['warnings'].append('未取得可解释的本次原始空间失败残差；不能用初值补做归因。')
    return report

"""Three offline tools sharing the existing CLI, Web and background jobs."""
import html
import json
import math

from rps_tools.core.tooling import Param, ParamError, Tool, register
from .analysis import analyze
from .report import format_chinese


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k):_json_safe(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [_json_safe(v) for v in value]
    return value


def render(report):
    lines = ['# 诊断报告', '', '只读解释已有入参和证据；原求解状态保留。', '']
    ref=report.get('solver_reference', {})
    if ref:
        lines += ['原结果：'+html.escape(str({k:ref[k] for k in ('solver_status_code','OBJ','stable') if k in ref})), '']
    for title, key in (('已复核的模型冲突','confirmed_conflicts'),('原结果报告的冲突（保留来源，未重新证明）','reported_conflicts'),
                       ('失败约束疑点','suspected_constraints'),('原始入参线索','input_hints')):
        items=report.get(key, [])
        lines += [f'## {title}（{len(items)}）', '']
        if not items:
            lines += ['未发现或证据不足。', '']
        for item in items:
            label=item.get('message') or item.get('code') or item.get('rule_id') or '证据'
            lines += ['- '+html.escape(str(label))]
            if key=='suspected_constraints':
                lines += ['  残差：'+str(item.get('violation'))+'；仅是失败观测，不是不可行证明。']
            paths=item.get('related_inputs', [])
            if item.get('input_path'):
                paths=[item['input_path'], *paths]
            for path in paths:
                path = path.get('input_path') if isinstance(path, dict) else path
                lines += ['  入参：'+html.escape(str(path))]
            if item.get('recommendation'):
                lines += ['  建议：'+html.escape(item['recommendation'])]
            for suggestion in item.get('suggestions', []):
                lines += ['  建议：'+html.escape(str(suggestion))]
        lines += ['']
    if report.get('model_proof'):
        lines += ['## 数学证据', '', '<pre>'+html.escape(format_chinese(report['model_proof'],detail=True))+'</pre>', '']
    if report.get('evidence'):
        lines += ['## 观测来源', '', '<pre>'+html.escape(json.dumps(report['evidence'],ensure_ascii=False,indent=2))+'</pre>', '']
    prop=report.get('property_bound_diagnostics')
    if prop:
        lines += ['## 性质卡边', '', '<pre>'+html.escape(prop.get('message',''))+'</pre>', '',
                  '完整条目、固定性质与可信边际来源见 report.json。', '']
    lines += ['## 覆盖与限制', '']
    lines += ['- '+html.escape(str(w)) for w in report.get('warnings',[])+report.get('limitations',[])]
    lines += ['', '额外业务求解次数：0；辅助 LP 次数：0。', '']
    return '\n'.join(lines)


def runner(mode):
    def run(ctx, params):
        ctx.progress(10, '读取显式选定的文件及来源信息')
        report=analyze(mode,params)
        ctx.progress(80, '生成证据分级与关联入参报告')
        report=_json_safe(report)
        (ctx.out_dir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        (ctx.out_dir/'report.md').write_text(render(report),encoding='utf-8')
        props=report.get('property_bound_diagnostics') or {}
        metrics={'已复核冲突':len(report['confirmed_conflicts']), '原结果冲突记录':len(report.get('reported_conflicts',[])),
              '失败约束疑点':len(report['suspected_constraints']),
              '入参线索':len(report['input_hints']), '性质贴边':len(props.get('binding_properties',[])),
              '固定性质':len(props.get('fixed_properties',[])), '额外求解':0}
        ctx.progress(100, '诊断完成，原始文件与求解状态保持不变')
        return dict(metrics=metrics, report='report.md', scope=report['scope'], extra_solver_calls=0)
    return run


def file(name,label,required=False,accept=None,help=''):
    return Param(name,label,type='file',required=required,accept=accept or ['.json'],help=help)


COMMON=[file('manifest','同次运行文件哈希清单（可选）',help='files 按角色列 sha256；只验证已选文件，不读取清单中的路径'),
        Param('time_limit','静态诊断预算（秒）',type='float',default=10,advanced=True)]


def validate(p):
    value=p.get('time_limit',10)
    if not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<value<=300:
        raise ParamError('静态诊断预算须大于 0 且不超过 300 秒')
    tol=p.get('tolerance',1e-6)
    if not isinstance(tol,(int,float)) or not math.isfinite(tol) or tol<=0:
        raise ParamError('残差展示阈值须为正的有限数')


register(Tool(id='diagnose.input_feasibility',group='diagnose',order=5,title='入参冲突与供需路径检查',
    description='扫描上下限、供需、装置启停、性质和路径线索；可复核原模型快照中的数学矛盾，不调用求解器。',
    params=[file('input','原始 input',True),file('snapshot','原始模型 bounds/rows 快照（可选）')]+COMMON,
    run=runner('input_feasibility'),view='report',validate=validate))
register(Tool(id='diagnose.failure_analysis',group='diagnose',order=6,title='失败约束与关联入参',
    description='解释已保存失败结果，区分复核冲突与残差疑点；保留中间/最后观测来源，不翻判状态。',
    params=[file('output','本次 output',True),file('input','同次原始 input（可选）'),
            file('snapshot','原始模型 bounds/rows 快照（可选）'),file('model','同次完整 MPProto 模型（可选）'),
            file('native','原生 solution JSON（可选）'),file('trace','原生 iterations 日志（可选）',accept=['.jsonl']),
            file('row_map','原生行名到模型 code 映射（可选）'),
            Param('tolerance','残差展示阈值',type='float',default=1e-6,advanced=True)]+COMMON,
    run=runner('failure_analysis'),view='report',validate=validate))
register(Tool(id='diagnose.property_bounds',group='diagnose',order=7,title='成功结果性质卡边',
    description='实际值、有效上下限、固定性质、可信边际和关联入参；零流量不报，缺失证据不补初值。',
    params=[file('output','成功 output',True),file('input','同次原始 input（可选）'),
            file('model','同次求解后的 MPProto 模型（旧 output 无内嵌提示时使用）')]+COMMON,
    run=runner('property_bounds'),view='report',validate=validate))

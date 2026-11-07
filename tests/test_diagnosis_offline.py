import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from rps_tools.diagnose.infeasibility.adapters import load_artifacts, proto_from_dict
from rps_tools.diagnose.infeasibility.analysis import analyze
from rps_tools.diagnose.infeasibility.core import detect_infeasible, verify_conflict
from rps_tools.diagnose.infeasibility.snapshot import snapshot_from_proto
from rps_tools.diagnose.infeasibility.static import analyze_input
from rps_tools.core.tooling import JobContext, get_tool


def write(tmp_path, name, data):
    path = tmp_path/(name+'.json')
    path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return str(path)


def snap():
    return dict(bounds={'x':dict(lb=1,ub=10), 'y':dict(lb=0,ub=10)}, rows=[
        dict(code='a',type='E',rhs=0,vars=[['x',1],['y',-1]]),
        dict(code='b',type='E',rhs=0,vars=[['y',1]])])


def input_case():
    return dict(configuration={},crude=[dict(code='FEED-A',purchase=dict(ub=10))],cduBlock=[
        dict(code='UNIT-A',_status=1,mass_flow_capacity=[dict(code='CAP-A',lb=1,association=['CASE-A'],association_coef={'CASE-A':1})],
             cases=[dict(code='CASE-A',crude_usage=[dict(crude='FEED-A',ub=10,property={'PROP-A':5})],
                         defence=[dict(code='PROP-A',ub=2)])])])


def files(tmp_path, **data):
    return {name:write(tmp_path,name,value) for name,value in data.items()}


def test_proof_replayed_and_snapshot_not_mutated():
    data=snap(); before=copy.deepcopy(data)
    with patch('subprocess.Popen',side_effect=AssertionError('no solver')):
        r=detect_infeasible(data)
    assert r['proven_infeasible'] and not r['auxiliary_lp_called']
    assert verify_conflict(data,r['findings'][0])
    assert data==before
    altered=copy.deepcopy(data); altered['bounds']['x']['lb']=0
    assert not verify_conflict(altered,r['findings'][0])


@pytest.mark.parametrize('bounds,rows,proven',[
    ({'x':dict(lb=2,ub=1)},[],True),
    ({'x':dict(lb=2,ub=1,semi_continuous=True)},[],False),
    ({'x':dict(lb=2,ub=None)},[],False),
    ({'x':dict(lb=0,ub=1)},[dict(code='r',type='G',rhs=2,vars=[['x',1]])],True),
    ({'x':dict(lb=0,ub=1)},[dict(code='r',type='G',rhs=2,status=0,vars=[['x',1]])],False),
    ({'x':dict(lb=1,ub=2)},[dict(code='r',type='E',rhs=-1,vars=[['x','x',1]])],False),
])
def test_proof_semantics(bounds,rows,proven):
    r=detect_infeasible(dict(bounds=bounds,rows=rows))
    assert r['proven_infeasible']==proven


def test_bound_assignment_order_and_n_expression_rhs():
    raw=dict(variable=[dict(code='x',lb=2,ub=1)],constarint=[
        dict(code='a',con_type='LB',RHS=0.0,status=0,vars=[['x',1]]),
        dict(code='expr',con_type='N',RHS=2,vars=[['x',1]]),
        dict(code='r',con_type='G',RHS=0,vars=[['expr',1]])])
    normalized=snapshot_from_proto(proto_from_dict(raw))
    assert normalized['bounds']['x']['lb']==0
    assert detect_infeasible(normalized)['proven_infeasible']


def test_legacy_rules_skip_disabled_unit_and_no_false_proven():
    data=input_case(); data['cduBlock'][0]['cases'][0]['crude_usage']=[]
    r=analyze_input(data)
    assert any(f['rule_id']=='CDU_REQUIRED_CASE_NO_FEED' for f in r['findings'])
    assert all(f['confidence']=='candidate' for f in r['findings'])
    data['cduBlock'][0]['_status']=0
    assert not any(f['rule_id'].startswith('CDU_') for f in analyze_input(data)['findings'])


def test_multiperiod_and_raw_bounds_are_cautious():
    data=input_case(); data['period']='P1'; data['customVariable']=[dict(code='TOTAL-A',lb=5,ub=1)]
    r=analyze_input(data)
    assert r['warnings']
    assert any(f['rule_id']=='RAW_BOUND_PAIR' for f in r['findings'])
    assert not any(f['rule_id'].startswith('CDU_') for f in r['findings'])


def test_generic_aggregate_route_replaces_customer_names():
    data=dict(customVariable=[dict(code='TOTAL-A',lb=1)],customConstraint=[dict(code='LINK-A',expr='TOTAL-A+-1*CCAPCAP-A')],
        manuBlock=[dict(code='UNIT-A',mass_flow_capacity=[dict(code='CAP-A')])],
        spliterBlock=[dict(code='SPLIT-A',output_block=[dict(code='UNIT-A',ratio_ub=0)])])
    hits=analyze_input(data)['findings']
    assert any(f['rule_id']=='RAW_AGGREGATE_ROUTE_CLOSED' and f['confidence']=='candidate' for f in hits)


def test_run_mismatch_and_hash_mismatch_are_rejected(tmp_path):
    p=files(tmp_path,input=dict(run_id='A'),output=dict(run_id='B'))
    with pytest.raises(ValueError,match='不同'):
        load_artifacts(p)
    p=files(tmp_path,input=dict(configuration={}))
    p['manifest']=write(tmp_path,'manifest',dict(files={'input':dict(sha256='bad')}))
    with pytest.raises(ValueError,match='SHA256'):
        load_artifacts(p)


def test_valid_manifest_never_follows_embedded_paths(tmp_path):
    p=files(tmp_path,input=dict(configuration={}))
    sha=hashlib.sha256(Path(p['input']).read_bytes()).hexdigest()
    p['manifest']=write(tmp_path,'manifest',dict(files={'input':dict(sha256=sha,path='DO-NOT-READ')}))
    _,prov,warnings=load_artifacts(p)
    assert prov['association']=='manifest_hashes' and not warnings


def test_failed_output_not_used_as_input(tmp_path):
    with pytest.raises(ValueError,match='原始 input'):
        analyze('input_feasibility',files(tmp_path,input=dict(solver_status_code=20)))


def test_input_template_with_empty_output_fields_is_accepted():
    data=input_case()
    data.update(solver_status_code=None,solver_status=None,OBJ=None,multi_start_obj=[])
    assert any(f['rule_id']=='CDU_REQUIRED_CASE_PROPERTY_UB' for f in analyze_input(data)['findings'])


def test_old_output_is_information_shortage_not_start_fallback(tmp_path):
    output=dict(solver_status_code=1,OBJ=1,stable=True,multi_start_obj=[1],materialList=[dict(code='MAT-A',property=[dict(code='PROP-A',value=1,start=1,ub=1)])])
    p=files(tmp_path,output=output)
    before=Path(p['output']).read_bytes()
    r=analyze('property_bounds',p)
    assert r['scope']=='insufficient_evidence'
    assert r['solver_reference']['multi_start_obj']==[1]
    assert before==Path(p['output']).read_bytes()


@pytest.mark.parametrize('code,extra',[(1,{}),(3,{}),(20,dict(solution_available=True)),(20,dict(solve_diagnostics=dict(failed=True,solution_source='checkpoint'))) ])
def test_failure_gates_skip_saved_values(tmp_path,code,extra):
    p=files(tmp_path,output=dict(solver_status_code=code,**extra))
    r=analyze('failure_analysis',p)
    assert r['scope']=='skipped_non_failure' and not r['suspected_constraints']


def test_failure_native_mapping_is_suspected_and_read_only(tmp_path):
    output=dict(solver_status_code=20,solve_diagnostics=dict(failed=True),stable=False,multi_start_obj=[])
    model=dict(variable=[dict(code='QPROP-AMAT-A',lb=0,ub=1)],constarint=[dict(code='RPROP-AMAT-A',con_type='E',RHS=0,vars=[['QPROP-AMAT-A',1]])])
    data=dict(materialList=[dict(code='MAT-A',property=[dict(code='PROP-A',lb=0,ub=1)])])
    p=files(tmp_path,output=output,model=model,input=data,native=dict(scaled=False,equ={'r0':[-3,0]}),row_map={'r0':'RPROP-AMAT-A'})
    before={k:Path(v).read_bytes() for k,v in p.items()}
    with patch('subprocess.Popen',side_effect=AssertionError('no process')):
        r=analyze('failure_analysis',p)
    assert not r['confirmed_conflicts']
    assert r['suspected_constraints'][0]['confidence']=='suspected'
    assert r['suspected_constraints'][0]['related_inputs'][0]['input_path']=='materialList[code=MAT-A].property[code=PROP-A]'
    assert r['solver_reference']['solver_status_code']==20
    assert before=={k:Path(v).read_bytes() for k,v in p.items()}
    Path(p['native']).write_text(json.dumps(dict(scaled=True,equ={'r0':[-3,0]})))
    assert not analyze('failure_analysis',p)['suspected_constraints']


def test_embedded_reports_keep_source_without_recertifying(tmp_path):
    embedded=dict(confirmed_conflicts=[dict(rule_id='BOUND_CONFLICT',confidence='proven')],suspected_constraints=[],evidence=dict(source='native'))
    p=files(tmp_path,output=dict(solver_status_code=20,infeasibility_diagnostics=dict(failure_analysis=embedded)))
    r=analyze('failure_analysis',p)
    assert r['scope']=='embedded_report' and r['reported_conflicts']
    assert not r['confirmed_conflicts']
    p=files(tmp_path,output=dict(solver_status_code=26,property_bound_diagnostics=dict(binding_properties=[],fixed_properties=[])))
    assert analyze('property_bounds',p)['scope']=='embedded_report'


def test_registered_tools_write_reports_without_optimizers(tmp_path):
    p=files(tmp_path,input=dict(configuration={}),snapshot=snap())
    tool=get_tool('diagnose.input_feasibility')
    ctx=JobContext(tmp_path/'job')
    with patch('subprocess.Popen',side_effect=AssertionError('no optimizer')):
        result=tool.run(ctx,tool.coerce_params(p))
    assert result['extra_solver_calls']==0
    report=json.loads((ctx.out_dir/'report.json').read_text(encoding='utf-8'))
    assert report['confirmed_conflicts'] and report['auxiliary_lp_calls']==0
    assert '已复核' in (ctx.out_dir/'report.md').read_text(encoding='utf-8')


def test_cli_uses_the_same_registered_entry(tmp_path):
    p=files(tmp_path,input=dict(configuration={}))
    result=subprocess.run([sys.executable,'-B','-m','rps_tools','tools','diagnose.input_feasibility'],capture_output=True)
    assert result.returncode==0 and b'diagnose.input_feasibility' in result.stdout
    result=subprocess.run([sys.executable,'-B','-m','rps_tools','run','diagnose.input_feasibility',
        'input='+p['input'],'--out',str(tmp_path/'cli')],capture_output=True)
    assert result.returncode==0,result.stderr.decode(errors='replace')
    assert (tmp_path/'cli/output/report.json').is_file()


def test_imports_do_not_load_rps_or_numeric_runtime():
    code="from rps_tools.core.tooling import get_tool; import sys; get_tool('diagnose.property_bounds'); assert not any(x in sys.modules for x in ('model','mp_interface','solver_caller','numpy','pandas','scipy'))"
    p=subprocess.run([sys.executable,'-B','-c',code],capture_output=True)
    assert p.returncode==0,p.stderr.decode(errors='replace')

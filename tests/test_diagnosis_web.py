import json

import pytest
from fastapi.testclient import TestClient

from rps_tools.config import Settings
from rps_tools.web.app import create_app
from test_web import wait


@pytest.fixture(scope='module')
def diagnostic_client(tmp_path_factory):
    with TestClient(create_app(Settings(workspace=tmp_path_factory.mktemp('diagnostic_jobs'),workers=1))) as c:
        yield c


@pytest.mark.parametrize('tool,data,expected',[
    ('input_feasibility',{'input':{'customVariable':[{'code':'VAR-A','lb':2,'ub':1}]}},'raw_input_and_optional_snapshot'),
    ('failure_analysis',{'output':{'solver_status_code':20,'solve_diagnostics':{'failed':True},
         'infeasibility_diagnostics':{'failure_analysis':{'confirmed_conflicts':[],'suspected_constraints':[{'code':'ROW-A','violation':1,'confidence':'suspected'}],'evidence':{'source':'native'}}}}},'embedded_report'),
    ('property_bounds',{'output':{'solver_status_code':26,'property_bound_diagnostics':{'binding_properties':[], 'fixed_properties':[], 'message':'无性质卡边'}}},'embedded_report'),
])
def test_diagnostic_upload_report(diagnostic_client,tool,data,expected):
    c=diagnostic_client
    files={key:(key+'.json',json.dumps(value)) for key,value in data.items()}
    r=c.post('/api/tools/diagnose.'+tool+'/jobs',files=files)
    assert r.status_code==200,r.text
    job=wait(c,r.json()['id'])
    assert job['status']=='succeeded',job
    report=c.get(f"/api/jobs/{job['id']}/files/report.json").json()
    assert report['scope']==expected and report['extra_solver_calls']==0
    html=c.get(f"/api/jobs/{job['id']}/markdown/report.md")
    assert html.status_code==200 and '诊断报告' in html.text


def test_wrong_run_files_fail_without_diagnosis(diagnostic_client):
    c=diagnostic_client
    r=c.post('/api/tools/diagnose.failure_analysis/jobs',files={
        'input':('in.json',json.dumps({'run_id':'A'})),
        'output':('out.json',json.dumps({'run_id':'B','solver_status_code':20}))})
    job=wait(c,r.json()['id'])
    assert job['status']=='failed' and 'run_id' in job['error']


def test_invalid_budget_is_rejected_before_job(diagnostic_client):
    r=diagnostic_client.post('/api/tools/diagnose.input_feasibility/jobs',
        files={'input':('input.json','{}')},data={'time_limit':'nan'})
    assert r.status_code==400

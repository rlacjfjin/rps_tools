"""Regressions found by replaying real inputs and by randomized models.

Every model here is synthetic. Status-1 solved cases must not look like conflicts,
true conflicts must not be dropped, and an unfinished scan must not look clean.
"""
import copy
import random

import pytest

from rps_tools.diagnose.infeasibility.analysis import analyze
from rps_tools.diagnose.infeasibility.core import _prepare, detect_infeasible, verify_conflict
from rps_tools.diagnose.infeasibility.propagation import chain, forcing_chain_conflict, propagate, replay, _emit_step
from rps_tools.diagnose.infeasibility.static import analyze_input
from rps_tools.diagnose.infeasibility import input_hints, route_hints
from test_diagnosis_offline import files


def lossy_loop():
    """a -> b -> c -> a loses mass every pass, so a positive feed cannot circulate."""
    return dict(bounds={'a':dict(lb=1,ub=100),'b':dict(lb=0,ub=100),'c':dict(lb=0,ub=100)},rows=[
        dict(code='r1',type='E',rhs=0,vars=[['b',1],['a',-0.5]]),
        dict(code='r2',type='E',rhs=0,vars=[['c',1],['b',-0.5]]),
        dict(code='r3',type='E',rhs=0,vars=[['a',1],['c',-1]])])


def random_rows(rng, names, feasible_point=None):
    rows = []
    for index in range(rng.randint(3, 10)):
        terms = [[code, rng.choice([-3,-2,-1,1,2,3])] for code in rng.sample(names, rng.randint(2, min(4, len(names))))]
        sense = rng.choice('ELG')
        if feasible_point is None:
            rhs = rng.randint(-10, 25)
        else:
            activity = sum(coefficient*feasible_point[code] for code, coefficient in terms)
            rhs = activity + {'E':0,'L':rng.choice([0,1,2]),'G':-rng.choice([0,1,2])}[sense]
        rows.append(dict(code=f'r{index}',type=sense,rhs=rhs,vars=terms))
    return rows


def test_conflict_built_on_an_earlier_tightening_is_proven():
    data = lossy_loop()
    report = detect_infeasible(data)
    assert report['status'] == 'proven_infeasible'
    assert report['coverage']['forcing_chain_search'] == 'completed'
    assert verify_conflict(data, report['findings'][0])


def test_every_conflict_keeps_a_replayable_chain():
    conflicts = 0
    for seed in range(1500):
        rng = random.Random(seed)
        bounds = {f'x{i}':dict(lb=rng.choice([0,0,-2]),ub=rng.choice([5,8,20,40])) for i in range(rng.randint(3,7))}
        data = dict(bounds=bounds,rows=random_rows(rng,list(bounds)))
        exact_bounds, rows, _, _ = _prepare(data)
        conflict, ordered, _ = propagate(exact_bounds, rows)
        if conflict is None:
            continue
        conflicts += 1
        steps, _ = chain(conflict, ordered)
        assert replay(exact_bounds, rows, [_emit_step(step) for step in steps], conflict), seed
        report = detect_infeasible(data)
        assert report['proven_infeasible'] and verify_conflict(data, report['findings'][0]), seed
    assert conflicts > 500


def test_model_with_a_known_feasible_point_is_never_proven_infeasible():
    for seed in range(1500):
        rng = random.Random(seed)
        point = {f'x{i}':rng.randint(-5,5) for i in range(rng.randint(3,8))}
        bounds = {code:dict(lb=value-rng.choice([0,1,3]),ub=value+rng.choice([0,1,3])) for code,value in point.items()}
        report = detect_infeasible(dict(bounds=bounds,rows=random_rows(rng,list(point),point)))
        assert report['status'] == 'no_conflict_found', seed


def test_chain_ends_on_original_bounds():
    finding, stop, _ = forcing_chain_conflict(*_prepare(lossy_loop())[:2])
    assert stop == 'completed'
    assert {'variable':'a','bound_type':'lb','bound':1.0,'exact_bound':'1'} in finding['bound_evidence']


@pytest.mark.parametrize('kwargs,stop',[(dict(time_limit=1e-9),'budget_exhausted')])
def test_interrupted_search_is_inconclusive(kwargs,stop):
    report = detect_infeasible(lossy_loop(), **kwargs)
    assert report['status'] == 'inconclusive' and not report['proven_infeasible']
    assert report['coverage']['forcing_chain_search'] == stop
    assert stop in report['warnings'][0]['reason']


def test_step_limit_and_failed_replay_are_never_a_clean_result(monkeypatch):
    from rps_tools.diagnose.infeasibility import core
    for stop in ('step_limit', 'replay_failed'):
        monkeypatch.setattr(core, 'forcing_chain_conflict', lambda *a, **k: (None, stop, 0))
        assert detect_infeasible(lossy_loop())['status'] == 'inconclusive'


def test_unfinished_proof_reaches_the_report(tmp_path, monkeypatch):
    from rps_tools.diagnose.infeasibility import core
    monkeypatch.setattr(core, 'forcing_chain_conflict', lambda *a, **k: (None, 'step_limit', 0))
    report = analyze('input_feasibility', files(tmp_path,input=dict(configuration={}),snapshot=lossy_loop()))
    assert not report['confirmed_conflicts']
    assert any('inconclusive' in warning for warning in report['warnings'])


def utility_case():
    return dict(configuration={},utility=[dict(code='UTIL-A',purchase=dict(ub=0),sell=dict(ub=0))],manuBlock=[
        dict(code='UNIT-A',mass_flow_capacity=[dict(code='CAP-A',lb=10,association=['CASE-A'],association_coef={'CASE-A':1})],
             cases=[dict(code='CASE-A')],utility=[dict(material='UTIL-A',association='CASE-A',coefficient=2)])])


def utility_hits(data):
    return [f for f in analyze_input(data)['findings'] if f['rule_id']=='UTILITY_REQUIRED_PURCHASE_UB_ZERO']


def producer(**extra):
    return dict(code='UNIT-B',column=[dict(code='COL-A')],utility_balance=[
        dict(code='UTIL-A',type='utility_out',expr={'COL-A':1},**extra)])


def test_forced_utility_without_any_source_is_flagged():
    assert len(utility_hits(utility_case())) == 1


def test_utility_balance_output_counts_as_internal_supply():
    data = utility_case()
    data['processBlock'] = [producer()]
    assert not utility_hits(data)


@pytest.mark.parametrize('mutate',[
    lambda block: block.update(_status=0),
    lambda block: block['utility_balance'][0].update(status=0),
    lambda block: block['column'][0].update(status=0),
])
def test_shut_down_producer_is_not_supply(mutate):
    data = utility_case()
    data['processBlock'] = [producer()]
    mutate(data['processBlock'][0])
    assert len(utility_hits(data)) == 1


def test_disabled_case_producer_is_not_supply():
    data = utility_case()
    data['manuBlock'].append(dict(code='UNIT-C',cases=[dict(code='CASE-C',status=0)],
        utility=[dict(material='UTIL-A',association='CASE-C',coefficient=-5)]))
    assert len(utility_hits(data)) == 1
    data['manuBlock'][1]['cases'][0]['status'] = 1
    assert not utility_hits(data)


def test_open_sale_is_not_supply():
    data = utility_case()
    data['utility'][0]['sell'] = dict(ub=100)
    assert len(utility_hits(data)) == 1


@pytest.mark.parametrize('field,value',[('dumping',True),('_status',0)])
def test_utility_without_a_strict_balance_is_not_flagged(field,value):
    data = utility_case()
    data['utility'][0][field] = value
    assert not utility_hits(data)


def starved_case(**splitter):
    return dict(materialList=[dict(code='MAT-A',name='物料甲',input=['SPLIT-A'])],
        spliterBlock=[dict(code='SPLIT-A',output_material=[dict(code='MAT-A')],**splitter)],
        manuBlock=[dict(code='UNIT-A',mass_flow_capacity=[dict(code='CAP-A',lb=4,association=['CASE-A'],association_coef={'CASE-A':2})],
            cases=[dict(code='CASE-A',material_ratio=[dict(material='MAT-A',type='in',ratio=0.5)])])])


def test_unit_without_cases_that_is_shut_down_counts_as_a_dead_source():
    report = analyze_input(starved_case(_status=0))
    scan = next(s for s in report['scans'] if s['scan']=='shutdown_starves_forced_unit')
    assert scan['ran'] and scan['hits'] == 1
    hit = next(f for f in report['findings'] if f['rule_id']=='RAW_SHUTDOWN_STARVES_FORCED_UNIT')
    assert hit['shutdown_units'] == ['SPLIT-A'] and hit['minimum_required'] == 1
    assert '整台装置' in hit['message']


def test_running_unit_without_cases_stays_an_unknown_source():
    report = analyze_input(starved_case())
    assert not any(f['rule_id']=='RAW_SHUTDOWN_STARVES_FORCED_UNIT' for f in report['findings'])


def scans_of(data):
    report = analyze_input(data)
    return {scan['scan']:scan for scan in report['scans']}, report['warnings']


def test_clean_input_reports_completed_scans():
    scans, warnings = scans_of(dict(configuration={}))
    assert all(scan['ran'] and scan['outcome']=='completed' for scan in scans.values())
    assert not warnings


def test_malformed_data_is_not_reported_as_a_clean_scan():
    data = dict(crude=[dict(purchase=dict(ub=1))],cduBlock=[dict(code='UNIT-A')])
    scans, warnings = scans_of(data)
    assert not scans['crude_supply']['ran'] and scans['crude_supply']['outcome']=='data_error'
    assert any(w.startswith('crude_supply') for w in warnings)


def test_multi_period_scans_all_say_they_were_skipped():
    scans, warnings = scans_of(dict(period='P1',utility=[dict(code='UTIL-A')],materialList=[dict(code='MAT-A')]))
    assert all(not scan['ran'] and scan['outcome']=='skipped' for scan in scans.values())
    assert sum('multi_plant_period_storage' in w for w in warnings) == 4


def test_unbounded_purchase_is_a_finished_scan():
    data = dict(crude=[dict(code='FEED-A',purchase=dict(ub=None))],cduBlock=[dict(code='UNIT-A',cases=[dict(code='CASE-A')])])
    scans, warnings = scans_of(data)
    assert scans['crude_supply']['ran'] and scans['crude_supply']['reason']=='purchase_unbounded'
    assert not warnings


@pytest.mark.parametrize('scan,data,name',[
    (input_hints.crude_supply_hints,dict(crude=[dict(code='FEED-A')],cduBlock=[dict(code='UNIT-A')]),'crude'),
    (route_hints.blocked_product_hints,dict(materialList=[dict(code='MAT-A')]),'route'),
])
def test_timeout_is_visible(scan,data,name):
    trace = {}
    assert scan(copy.deepcopy(data),time_limit=0,trace=trace) == []
    assert trace['outcome'] == 'timed_out' and trace['timed_out'], name

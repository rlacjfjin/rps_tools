"""Failure hints must be evidence-only, never a second solver or a status gate."""
import copy
import json
import math
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from rps_tools.diagnose.infeasibility.execution import (
    capture_failure_residuals, read_iteration_residuals,
)
from rps_tools.diagnose.infeasibility.report import attach_failure_diagnostics, format_chinese
from rps_tools.diagnose.infeasibility.snapshot import failure_input_context
from diagnosis_helpers import FakeProto as MPProto
from enum import IntEnum
Status = IntEnum("Status", {**{f"STATUS_{i}":i for i in range(1,27)}, "NUMERICAL_ERROR":20})


def make_proto():
    proto = MPProto()
    proto.add_variable(dict(code='QPROP-APRODUCT', lb=49, ub=51, start=49))
    proto.add_variable(dict(code='FLOW', lb=0, ub=100, start=1))
    proto.add_constraint(dict(code='RPROP-APRODUCT', con_type='E', RHS=0,
                              vars=[('QPROP-APRODUCT', 'FLOW', 1), ('FLOW', -60)]))
    proto.add_constraint(dict(code='OBJ', con_type='N', RHS=0, vars=[('FLOW', 1)]))
    return proto


def input_data():
    return {'materialList': [dict(code='PRODUCT', name='外销产品', property=[
        dict(code='PROP-A', name='PROP-A,w%', lb=49, ub=51, start=45)])],
        'processBlock': [dict(code='SEP', name='分离装置', recursion_balance=[
            dict(bind_code='PRODUCT', bind_prop='PROP-A', expr={'BASE': -2.5})])]}


def event(violation=32, *, profile='pdr-seed/v1', iteration=10, **kw):
    return dict(event='iteration', profile=profile, scaled=False, iteration=iteration,
                sequence=iteration, pid=1, timestamp_ms=iteration, infeas=violation,
                top_violations=[dict(name='R0000000', violation=violation, activity=-violation, lb=0, ub=0)],
                **kw)


def failed_result():
    return dict(solver_status_code=20, solver_status=Status.NUMERICAL_ERROR,
                solve_diagnostics=dict(failed=True, solution_source=None),
                solution_available=False, stable=False, multi_start_obj=[])


class IterationEvidenceTest(unittest.TestCase):
    def read(self, events, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'trace.jsonl'
            path.write_text('\n'.join(e if isinstance(e, str) else json.dumps(e) for e in events), encoding='utf-8')
            return read_iteration_residuals(path, {'R0000000': 'RPROP-APRODUCT'}, 1e-6, **kw)

    def test_better_intermediate_observation_is_explicit_not_a_terminal_point(self):
        report = self.read([event(1, iteration=1), event(32, iteration=2, note='final'),
                            event(40, profile='elastic', iteration=3)])
        self.assertEqual(report['rows'][0]['violation'], 1)
        self.assertFalse(report['terminal_record'])
        self.assertEqual(report['observation_kind'], 'intermediate')
        self.assertEqual(report['latest_profile_observation']['total_violation'], 32)
        self.assertEqual(report['profile'], 'pdr-seed/v1')

    def test_runaway_does_not_hide_lower_residual_observation(self):
        report = self.read([event(32, iteration=10), event(6e7, iteration=20),
                            event(132, profile='elastic', iteration=111, note='final')])
        self.assertEqual(report['profile'], 'pdr-seed/v1')
        self.assertEqual(report['rows'][0]['violation'], 32)
        self.assertEqual(report['observation_kind'], 'intermediate')

    def test_zero_endpoint_invalidates_earlier_residual(self):
        self.assertEqual(self.read([event(32), event(0, iteration=11)])['rows'], [])

    def test_skips_scaled_unknown_generator_nan_and_broken_records(self):
        scaled = dict(event(), scaled=True)
        unknown = dict(event(), top_violations=[dict(name='unknown', violation=100)])
        nan = dict(event(), infeas=float('nan'))
        report = self.read([scaled, unknown, nan, event(profile='pdr-generator'), '{broken', event(0.01, iteration=12)])
        self.assertEqual(report['rows'][0]['violation'], 0.01)
        self.assertEqual(report['unknown_rows'], 1)
        self.assertEqual(report['malformed_lines'], 1)

    def test_zero_budget_and_bounded_tail(self):
        self.assertTrue(self.read([event()], time_limit=0)['timed_out'])
        report = self.read(['x' * 8000, event()], max_bytes=1500)
        self.assertTrue(report['truncated'])
        self.assertEqual(report['rows'][0]['code'], 'RPROP-APRODUCT')


    def test_equ_activities_are_diagnostics_only(self):
        proto = make_proto()
        proto.solve_diagnostics = dict(failed=True, solution_source=None)
        before = copy.deepcopy(proto.to_dict())
        capture_failure_residuals(proto, {'equ': {'R0000000': [-32, 0]}},
                                  {'R0000000': 'RPROP-APRODUCT'}, True, None, 1e-6)
        self.assertEqual(proto._failure_residuals['rows'][0]['violation'], 32)
        self.assertEqual(proto.to_dict(), before)

    def test_feasible_checkpoint_does_not_read_failed_traces(self):
        proto = SimpleNamespace(solve_diagnostics=dict(failed=True, solution_source='multi_start_candidate'))
        capture_failure_residuals(proto, {}, {}, True, 'missing', 1e-6)
        self.assertFalse(hasattr(proto, '_failure_residuals'))


class BusinessFailureHintsTest(unittest.TestCase):
    def test_residual_maps_input_and_suggests_review_without_claiming_proof(self):
        proto = make_proto()
        proto._failure_residuals = dict(source='native_iteration_log', profile='pdr', iteration=8,
                                        observation_kind='intermediate',
                                        rows=[dict(code='RPROP-APRODUCT', violation=32)])
        data, result = input_data(), failed_result()
        before, model_before = copy.deepcopy(result), copy.deepcopy(proto.to_dict())
        with patch('subprocess.run', side_effect=AssertionError('no second solve')), \
             patch('rps_tools.diagnose.infeasibility.core.detect_infeasible', side_effect=AssertionError('no LP proof search')):
            attach_failure_diagnostics(proto, result, data)
        analysis = result['infeasibility_diagnostics']['failure_analysis']
        self.assertEqual(analysis['confirmed_conflicts'], [])
        row = analysis['suspected_constraints'][0]
        self.assertEqual(row['confidence'], 'suspected')
        self.assertEqual(row['related_inputs'][0]['input_settings']['ub'], 51)
        self.assertIn('materialList[code=PRODUCT].property[code=PROP-A]', result['diagnostic_message'])
        self.assertIn('processBlock[code=SEP].recursion_balance[0]', result['diagnostic_message'])
        self.assertNotIn('确定冲突', result['diagnostic_message'])
        self.assertNotIn('62', result['diagnostic_message'])
        self.assertIn('中间观测，非终止点', result['diagnostic_message'])
        self.assertEqual(format_chinese(result['infeasibility_diagnostics']), result['diagnostic_message'])
        self.assertEqual(result['infeasibility_diagnostics']['message'], result['diagnostic_message'])
        for key, value in before.items():
            self.assertEqual(result[key], value)
        self.assertEqual(proto.to_dict(), model_before)
        self.assertEqual(data, input_data())

    def test_actual_bound_conflict_is_proven_but_keeps_termination(self):
        proto = make_proto()
        proto.get_variable('QPROP-APRODUCT').lb = 52
        result = failed_result()
        attach_failure_diagnostics(proto, result, input_data())
        analysis = result['infeasibility_diagnostics']['failure_analysis']
        self.assertEqual(analysis['confirmed_conflicts'][0]['confidence'], 'proven')
        self.assertEqual(result['solver_status_code'], 20)
        self.assertIn('【确定冲突】', result['diagnostic_message'])

    def test_success_unbounded_and_checkpoints_return_before_model_access(self):
        for status in (1, 3, 4, 7, 9, 10, 25, 26):
            result = dict(failed_result(), solver_status_code=status)
            before = copy.deepcopy(result)
            attach_failure_diagnostics(object(), result)
            self.assertEqual(result, before)
        for extra in (dict(solution_available=True), dict(solve_diagnostics=dict(failed=True, solution_source='primary'))):
            result = dict(failed_result(), **extra)
            before = copy.deepcopy(result)
            attach_failure_diagnostics(object(), result)
            self.assertEqual(result, before)

    def test_no_evidence_does_not_use_initial_values_as_failure_point(self):
        result = failed_result()
        attach_failure_diagnostics(make_proto(), result, input_data())
        analysis = result['infeasibility_diagnostics']['failure_analysis']
        self.assertEqual(analysis['suspected_constraints'], [])
        self.assertIn('无法据此确定冲突原因', result['diagnostic_message'])

    def test_malformed_mapping_cannot_change_response(self):
        proto, result = make_proto(), failed_result()
        proto._failure_residuals = dict(rows=[dict(code='RPROP-APRODUCT', violation=32)])
        before = copy.deepcopy(result)
        with patch('rps_tools.diagnose.infeasibility.snapshot.failure_input_context', side_effect=ValueError('bad metadata')):
            attach_failure_diagnostics(proto, result, input_data())
        self.assertEqual(result, before)

    def test_keeps_prior_message_and_is_idempotent(self):
        proto, result = make_proto(), failed_result()
        result['diagnostic_message'] = '原终止原因'
        result['infeasibility_diagnostics'] = {'warnings': ['keep']}
        attach_failure_diagnostics(proto, result)
        before = copy.deepcopy(result)
        attach_failure_diagnostics(proto, result)
        self.assertEqual(result, before)
        self.assertTrue(result['diagnostic_message'].startswith('原终止原因\n'))
        self.assertEqual(result['infeasibility_diagnostics']['warnings'], ['keep'])

    def test_termination_codes_are_preserved_on_failure(self):
        for code in (2, 5, 6, 8, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24):
            with self.subTest(code=code):
                proto, result = make_proto(), failed_result()
                result.update(solver_status_code=code, solver_status=Status(code))
                proto.status = Status(code)
                attach_failure_diagnostics(proto, result)
                self.assertEqual(result['solver_status_code'], code)
                self.assertEqual(result['solver_status'], Status(code))
                self.assertEqual(proto.status, Status(code))

    def test_bound_override_does_not_report_superseded_conflict(self):
        proto = make_proto()
        proto.get_variable('QPROP-APRODUCT').lb = 52
        proto.add_constraint(dict(code='OVERRIDE', con_type='LB', RHS=49.0, vars=[('QPROP-APRODUCT', 1)]))
        result = failed_result()
        attach_failure_diagnostics(proto, result, input_data())
        self.assertEqual(result['infeasibility_diagnostics']['failure_analysis']['confirmed_conflicts'], [])

    def test_implicit_property_links_recursion_without_inventing_input_bounds(self):
        data = {'manuBlock': [dict(code='REACTOR', property_recursion=[dict(code='PRODUCT', prop='PROP-A')])]}
        context = failure_input_context(make_proto(), ['RPROP-APRODUCT'], data)['RPROP-APRODUCT']
        self.assertIn('property_recursion[0]', context['related_inputs'][0]['input_path'])
        self.assertNotIn('input_settings', context['related_inputs'][0])
        self.assertEqual(context['variables'][1]['ub'], 51)




if __name__ == '__main__':
    unittest.main()

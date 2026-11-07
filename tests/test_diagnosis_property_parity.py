import copy
import math
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from rps_tools.diagnose.infeasibility.property_bounds import attach_property_bound_hints
from diagnosis_helpers import FakeProto as MPProto


def fixture(*, value=51, lb=49, ub=51, flow=10, blender=False, suffix=''):
    proto = MPProto()
    mcode = 'PRODUCT' + suffix
    proto.add_variable(dict(code='SELL'+mcode, lb=0, ub=100, value=flow))
    prop = dict(code='PROP-A', name='PROP-A,w%', lb=lb, ub=ub, start=51, value=value,
                marginal_benefit=3.0, marginal_quality=dict(verdict='usable', reason='solver_native'))
    if blender:
        for prefix, kind in (('XQ', 'X'), ('X', 'G'), ('N', 'L')):
            proto.add_constraint(dict(code=prefix+'PROP-A'+mcode, con_type=kind, RHS=0,
                                      vars=[('SELL'+mcode, 1)]))
        proto.constraint.loc['XQPROP-A'+mcode].dual_value = value
        proto.constraint.loc['XPROP-A'+mcode].dual_value = -3.0
        proto.constraint.loc['NPROP-A'+mcode].dual_value = -5.0
    else:
        proto.add_variable(dict(code='QPROP-A'+mcode, lb=lb, ub=ub, value=value, reduced_cost=3.0))
        proto.add_constraint(dict(code='REC', con_type='E', RHS=0,
                                  vars=[('QPROP-A'+mcode, 'SELL'+mcode, 1)]))
    mat = NS(_code='PRODUCT', code=mcode, name='产品', input=[], output=[],
             basi=NS(name='BlenderOut' if blender else 'Mass'))
    model = NS(mpproto=proto, material_list=NS(material_table=[mat]))
    result = dict(solver_status_code=1, solve_diagnostics=dict(failed=False), OBJ=42,
                  multi_start_obj=[42,42], stable=True,
                  materialList=[dict(code='PRODUCT', property=[prop])])
    data = dict(materialList=[dict(code='PRODUCT', property=[dict(code='PROP-A', lb=lb, ub=ub, start=51)])])
    return model, result, data


class PropertyBoundHintsTest(unittest.TestCase):
    def annotate(self, **kw):
        model, result, data = fixture(**kw)
        attach_property_bound_hints(model, result, data)
        return model, result, result['property_bound_diagnostics']

    def test_upper_bound_values_marginal_and_input_are_read_only(self):
        model, result, data = fixture()
        before, pb, ib = copy.deepcopy(result), copy.deepcopy(model.mpproto.to_dict()), copy.deepcopy(data)
        with patch('subprocess.run', side_effect=AssertionError('no solve')), \
             patch('subprocess.Popen', side_effect=AssertionError('no subprocess')):
            attach_property_bound_hints(model, result, data)
        diag = result.pop('property_bound_diagnostics')
        self.assertEqual(result, before)
        self.assertEqual(model.mpproto.to_dict(), pb)
        self.assertEqual(data, ib)
        item = diag['binding_properties'][0]
        self.assertEqual((item['value'],item['lb'],item['ub'],item['side']), (51,49,51,'upper'))
        self.assertTrue(item['marginal']['usable'])
        self.assertEqual(item['marginal']['value'], 3)
        self.assertEqual(item['input_path'], 'materialList[code=PRODUCT].property[code=PROP-A]')

    def test_zero_and_missing_flow_never_report_property_binding(self):
        for flow in (0, 1e-12, -1, math.nan):
            with self.subTest(flow=flow):
                _, _, diag = self.annotate(flow=flow)
                self.assertEqual(diag['binding_properties'], [])
                self.assertEqual(diag['fixed_properties'], [])

    def test_internal_flow_counts_when_sales_are_zero(self):
        model, result, data = fixture(flow=0)
        model.material_list.material_table[0].input = ['UNIT']
        proto = model.mpproto
        proto.add_constraint(dict(code='NUNITPRODUCT', con_type='N', RHS=0, vars=[('SELLPRODUCT',1)]))
        proto.constraint.loc['NUNITPRODUCT'].dual_value = 8
        attach_property_bound_hints(model, result, data)
        self.assertEqual(result['property_bound_diagnostics']['binding_properties'][0]['flow_evidence']['code'], 'NUNITPRODUCT')

    def test_fixed_and_blender_properties_also_require_positive_flow(self):
        for options in (dict(lb=51,ub=51), dict(blender=True)):
            _, _, diag = self.annotate(flow=0, **options)
            self.assertEqual(diag['binding_properties'], [])
            self.assertEqual(diag['fixed_properties'], [])

    def test_hard_row_dual_is_not_mistaken_for_a_positive_flow(self):
        model, result, data = fixture(flow=0)
        model.material_list.material_table[0].input = ['UNIT']
        model.mpproto.add_constraint(dict(code='NUNITPRODUCT',con_type='E',RHS=0,vars=[('SELLPRODUCT',1)]))
        model.mpproto.constraint.loc['NUNITPRODUCT'].dual_value = 1000
        attach_property_bound_hints(model, result, data)
        self.assertEqual(result['property_bound_diagnostics']['binding_properties'], [])

    def test_fixed_properties_are_separate_and_no_one_sided_marginal(self):
        _, _, diag = self.annotate(value=51, lb=51, ub=51)
        self.assertEqual(diag['binding_properties'], [])
        self.assertEqual(diag['fixed_properties'][0]['side'], 'fixed')
        self.assertIsNone(diag['fixed_properties'][0]['marginal']['value'])
        self.assertIn('固定性质', diag['message'])

    def test_narrow_interval_is_not_mistaken_for_fixed(self):
        _, _, diag = self.annotate(lb=50.999999, ub=51.000001)
        self.assertEqual(diag['fixed_properties'], [])
        self.assertEqual(diag['binding_properties'][0]['side'], 'both')
        self.assertIsNone(diag['binding_properties'][0]['marginal']['value'])

    def test_reporting_start_fallback_is_not_a_solution_value(self):
        model, result, data = fixture(value=math.nan)
        result['materialList'][0]['property'][0]['value'] = 51
        attach_property_bound_hints(model, result, data)
        self.assertEqual(result['property_bound_diagnostics']['binding_properties'], [])

    def test_unused_property_with_a_number_does_not_report_binding(self):
        model, result, data = fixture()
        model.mpproto.constraint.loc['REC'].vars = []
        attach_property_bound_hints(model, result, data)
        self.assertEqual(result['property_bound_diagnostics']['binding_properties'], [])

    def test_success_only_and_checkpoint_or_unbounded_is_untouched(self):
        for code in (0,2,3,5,6,7,9,11,12,19,20,24):
            result = dict(solver_status_code=code)
            attach_property_bound_hints(object(), result)
            self.assertEqual(result, dict(solver_status_code=code))
        for extra in (dict(solution_available=False), dict(solve_diagnostics=dict(failed=True))):
            result = dict(solver_status_code=1, **extra)
            before = copy.deepcopy(result)
            attach_property_bound_hints(object(), result)
            self.assertEqual(result, before)
        for code in (1,4,25,26):
            model, result, data = fixture()
            result['solver_status_code'] = code
            attach_property_bound_hints(model, result, data)
            self.assertEqual(result['solver_status_code'], code)
            self.assertTrue(result['property_bound_diagnostics']['binding_properties'])

    def test_blender_uses_expression_and_matching_bound_dual(self):
        _, _, diag = self.annotate(blender=True)
        self.assertEqual(diag['binding_properties'][0]['value_source'], 'XQPROP-APRODUCT')
        self.assertEqual(diag['binding_properties'][0]['marginal']['source_code'], 'XPROP-APRODUCT')
        model, result, data = fixture(blender=True, value=49)
        result['materialList'][0]['property'][0]['marginal_benefit'] = 5
        attach_property_bound_hints(model, result, data)
        item = result['property_bound_diagnostics']['binding_properties'][0]
        self.assertEqual(item['marginal']['source_code'], 'NPROP-APRODUCT')
        self.assertEqual(item['marginal']['value'], 5)

    def test_disabled_blend_bound_is_not_inferred_from_input(self):
        model, result, data = fixture(blender=True)
        model.mpproto.constraint.loc['XPROP-APRODUCT'].vars = []
        attach_property_bound_hints(model, result, data)
        self.assertEqual(result['property_bound_diagnostics']['binding_properties'], [])

    def test_blend_spec_input_path_exists_even_when_material_properties_empty(self):
        model, result, data = fixture(blender=True)
        data['materialList'][0]['property'] = []
        data['blenderBlock'] = [dict(code='BLEND', property_limit=[dict(code='PROP-A', material='PRODUCT', lb=49, ub=51)])]
        attach_property_bound_hints(model, result, data)
        item = result['property_bound_diagnostics']['binding_properties'][0]
        self.assertEqual(item['input_path'], 'blenderBlock[code=BLEND].property_limit[0]')
        self.assertEqual(item['input_settings']['ub'], 51)

    def test_global_and_generated_properties_do_not_invent_local_input_entries(self):
        for has_global in (True, False):
            model, result, data = fixture()
            data['materialList'][0]['property'] = []
            if has_global:
                data['property'] = [dict(code='PROP-A', lb=0, ub=100)]
            attach_property_bound_hints(model, result, data)
            item = result['property_bound_diagnostics']['binding_properties'][0]
            self.assertEqual(item['input_path'], 'property[code=PROP-A]' if has_global else 'materialList[code=PRODUCT]')
            self.assertEqual(item['ub'], 51)  # Effective bounds, not the global default.

    def test_suspect_missing_and_mismatched_marginals_are_not_shown(self):
        for quality, value in (('suspect',3), ('unusable',3), ('usable',None), ('usable',100)):
            model, result, data = fixture()
            prop = result['materialList'][0]['property'][0]
            prop.update(marginal_quality=dict(verdict=quality), marginal_benefit=value)
            attach_property_bound_hints(model, result, data)
            marginal = result['property_bound_diagnostics']['binding_properties'][0]['marginal']
            self.assertFalse(marginal['usable'])
            self.assertIsNone(marginal['value'])

    def test_credible_zero_marginal_is_not_missing(self):
        model, result, data = fixture()
        model.mpproto.get_variable('QPROP-APRODUCT').reduced_cost = 0
        result['materialList'][0]['property'][0]['marginal_benefit'] = 0
        attach_property_bound_hints(model, result, data)
        marginal = result['property_bound_diagnostics']['binding_properties'][0]['marginal']
        self.assertTrue(marginal['usable'])
        self.assertEqual(marginal['value'], 0)

    def test_effective_bounds_take_precedence_over_input(self):
        model, result, data = fixture()
        model.mpproto.get_variable('QPROP-APRODUCT').ub = 60
        attach_property_bound_hints(model, result, data)
        self.assertEqual(result['property_bound_diagnostics']['binding_properties'], [])

    def test_property_scale_and_numerical_excess_do_not_change_status(self):
        for value,lb,ub,expected in ((0.001,0,0.001,True), (0.001001,0,0.001,False),
                                     (0.8120000001,0.812,0.84,True), (0.82,0.812,0.84,False)):
            _, result, diag = self.annotate(value=value, lb=lb, ub=ub)
            self.assertEqual(bool(diag['binding_properties']), expected)
            self.assertEqual(result['solver_status_code'], 1)

    def test_period_suffix_uses_model_code_but_preserves_raw_input_path(self):
        _, _, diag = self.annotate(suffix='PLANTP1')
        item = diag['binding_properties'][0]
        self.assertEqual(item['value_source'], 'QPROP-APRODUCTPLANTP1')
        self.assertIn('[code=PRODUCT]', item['input_path'])

    def test_failure_in_hint_generation_is_atomic(self):
        model, result, data = fixture()
        before = copy.deepcopy(result)
        with patch('rps_tools.diagnose.infeasibility.property_bounds._marginal', side_effect=ValueError('bad metadata')):
            attach_property_bound_hints(model, result, data)
        self.assertEqual(result, before)

    def test_serialization_is_idempotent(self):
        model, result, data = fixture()
        attach_property_bound_hints(model, result, data)
        before = copy.deepcopy(result)
        attach_property_bound_hints(model, result, data)
        self.assertEqual(result, before)


if __name__ == '__main__':
    unittest.main()

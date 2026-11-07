"""Read marginal provenance; never reconstruct a dual or solve an LP."""
import math


def finite(value):
    try:
        return value is not None and not isinstance(value, bool) and math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _row_verdict(proto, code):
    if code not in proto.constraint.index:
        return 'unusable', 'row_removed_by_simplification', None
    row = proto.constraint.loc[code]
    if hasattr(row, 'slp_dual_value'):
        drift = getattr(row, 'recompute_drift_exposure', None)
        if not finite(drift):
            return 'unusable', 'recompute_provenance_missing', None
        return ('unusable', 'drift_unreliable', drift) if drift >= 1 else ('usable', 'recomputed', drift)
    if row.con_type.name in ('N', 'X'):
        return 'unusable', 'row_folded_to_expression', None
    if not finite(row.dual_value):
        return 'unusable', 'dual_missing', None
    return 'usable', 'solver_native', None


def _var_verdict(proto, code, referenced):
    if not proto.has_variable(code):
        return 'unusable', 'variable_removed', []
    var = proto.get_variable(code)
    if hasattr(var, 'slp_reduced_cost'):
        drift = getattr(var, 'recompute_drift_exposure', None)
        if not finite(drift):
            return 'unusable', 'recompute_provenance_missing', []
        if drift >= 1:
            return 'unusable', 'drift_unreliable', []
        return 'usable', 'recomputed', []
    if code not in referenced:
        return 'unusable', 'variable_eliminated', []
    if not finite(var.reduced_cost):
        return 'unusable', 'reduced_cost_missing', []
    return 'usable', 'solver_native', []

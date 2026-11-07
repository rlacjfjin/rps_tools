"""Synthetic model-like fixture, with no import from the solver project."""
import copy
from types import SimpleNamespace as NS

from rps_tools.diagnose.infeasibility.adapters import Records


class FakeProto:
    def __init__(self):
        self.variable = Records()
        self.constraint = Records()

    def add_variable(self, raw):
        meta = dict(lb=None,ub=None,start=None,value=None,reduced_cost=None)
        meta.update(raw)
        self.variable = Records([*self.variable, NS(**meta)])

    def add_constraint(self, raw):
        meta = dict(RHS=0,vars=[],status=1,dual_value=None)
        meta.update(raw)
        meta['con_type'] = NS(name=raw['con_type'])
        self.constraint = Records([*self.constraint, NS(**meta)])

    def get_variable(self, code):
        return self.variable.get_variable(code)

    def has_variable(self, code, warning=False):
        return code in self.variable.index

    def to_dict(self):
        return copy.deepcopy(dict(variable=[vars(v) for v in self.variable],
            constarint=[dict(vars(c),con_type=c.con_type.name) for c in self.constraint]))

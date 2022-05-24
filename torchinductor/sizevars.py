import collections
import dataclasses
import functools
from typing import Dict
from typing import List

import sympy
from sympy import Expr
from sympy import Integer
from sympy import Symbol


@dataclasses.dataclass
class ZeroGuard:
    """
    An expression we should check equals zero.
    Guards are currently not checked.  Plan to add this later.
    """

    expr: sympy.Expr


@dataclasses.dataclass
class PositiveGuard:
    """
    An expression we should check for > 0
    Guards are currently not checked.  Plan to add this later.
    """

    expr: sympy.Expr


class SizeVarAllocator(object):
    def __init__(self, prefix="s", zero_one_const=True):
        super().__init__()
        self.prefix = prefix
        self.val_to_var: Dict[int, Expr] = {0: Integer(0), 1: Integer(1)}
        self.var_to_val: Dict[Expr, int] = collections.OrderedDict()
        self.guards = []
        self.replacements = {}
        if not zero_one_const:
            self.val_to_var.clear()

    def simplify(self, expr):
        return sympy.expand(expr).subs(self.replacements)

    def guard_equals(self, left: sympy.Symbol, right: sympy.Symbol):
        if left == right:
            return left
        expr = self.simplify(left - right)
        assert self.size_hint(expr) == 0, (expr, self.size_hint(expr))
        free = list(expr.free_symbols)
        if len(free) == 0:
            assert expr == 0
            return left
        elif len(free) in (1, 2, 3):
            # remove the largest of the guarded variables
            free.sort(key=self.size_hint)
            try:
                solutions = sympy.solve(expr, free[-1])
                if (
                    len(solutions) == 1
                    and solutions[0]
                    and "/" not in str(solutions[0])
                ):
                    self.replacements[free[-1]] = solutions[0]
            except NotImplementedError:
                pass

        self.guards.append(ZeroGuard(expr))

        if len(right.free_symbols) < len(left.free_symbols):
            return right
        else:
            return left

    def maybe_guard_equals(self, left: sympy.Symbol, right: sympy.Symbol):
        if self.size_hint(left - right) == 0:
            self.guard_equals(left, right)
            return True
        return False

    def guard_lt(self, left: sympy.Symbol, right: sympy.Symbol):
        expr = self.simplify(right - left)
        assert self.size_hint(expr) > 0
        if len(expr.free_symbols) == 0:
            return
        if "-" in str(expr):
            # all vars are positive, so needs a minus sign to get negative values
            self.guards.append(PositiveGuard(expr))

    def guard_min(self, left: sympy.Symbol, right: sympy.Symbol):
        """return the smaller of left and right, and guard on that choice"""
        lv = self.size_hint(left)
        rv = self.size_hint(right)
        if lv == rv:
            return self.guard_equals(left, right)
        elif lv < rv:
            self.guard_lt(left, right)
            return left
        else:
            self.guard_lt(right, left)
            return right

    def guard_max(self, left: sympy.Symbol, right: sympy.Symbol):
        """return the larger of left and right, and guard on that choice"""
        return -self.guard_min(-left, -right)

    def guard_static_shape(self, left):
        right = self.size_hint(left)
        self.guard_equals(left, sympy.Integer(right))
        return int(right)

    def __getitem__(self, val):
        if val < 0:
            # all variables are positive
            return -self[-val]
        if val in self.val_to_var:
            return self.val_to_var[val]
        var = Symbol(
            f"{self.prefix}{len(self.var_to_val)}", positive=True, integer=True
        )
        self.val_to_var[val] = var
        self.var_to_val[var] = val
        return var

    def size_hint(self, expr: Expr):
        return int(sympy.expand(expr).subs(self.var_to_val))

    def stride_vars(self, index: sympy.Expr, vars: List[sympy.Symbol]):
        """Convert an indexing expression back into strides"""
        strides = []
        index = index.subs(self.replacements)
        # remove any offset
        index = index - index.subs({v: sympy.Integer(0) for v in vars if v != 0})
        for i in range(len(vars)):
            # drop all the other dims
            index_dim = index.subs(
                {
                    vars[j]: sympy.Integer(0)
                    for j in range(len(vars))
                    if i != j and vars[j] != 0
                }
            )
            v = vars[i]
            if v == 0:
                strides.append(sympy.Integer(0))
            else:
                # TODO(jansel): should we use sympy.diff here?
                strides.append(
                    index_dim.subs({v: sympy.Integer(1)})
                    - index_dim.subs({v: sympy.Integer(0)})
                )
        return strides

    def stride_hints(self, index: sympy.Expr, vars: List[sympy.Symbol]):
        return [self.size_hint(s) for s in self.stride_vars(index, vars)]

    def stride_order(self, index: sympy.Expr, vars: List[sympy.Symbol]):
        strides = tuple(map(abs, self.stride_hints(index, vars)))
        order = list(range(len(strides)))
        order.sort(key=lambda x: (strides[x] == 0, strides[x]))
        return order

    def codegen(self, code, graph_inputs):
        """Assign all symbolic shapes to locals"""

        @functools.lru_cache(None)
        def sizeof(name):
            code.writeline(f"{name}_size = {name}.size()")
            return f"{name}_size"

        @functools.lru_cache(None)
        def strideof(name):
            code.writeline(f"{name}_stride = {name}.stride()")
            return f"{name}_stride"

        needed = set(map(str, self.var_to_val.keys())) - set(self.replacements.keys())

        for name, value in graph_inputs.items():
            shapes = value.get_size()
            for dim, shape in enumerate(shapes):
                shape = str(shape)
                if shape in needed:
                    needed.remove(shape)
                    code.writeline(f"{shape} = {sizeof(name)}[{dim}]")

        for name, value in graph_inputs.items():
            shapes = value.get_stride()
            for dim, shape in enumerate(shapes):
                shape = str(shape)
                if shape in needed:
                    needed.remove(shape)
                    code.writeline(f"{shape} = {strideof(name)}[{dim}]")

        assert not needed

    def codegen_sizevar(self, x):
        from .codegen.wrapper import pexpr

        return pexpr(x.subs(self.replacements))

    def codegen_shape_tuple(self, shape):
        parts = list(map(self.codegen_sizevar, shape))
        if len(parts) == 0:
            return "()"
        if len(parts) == 1:
            return f"({parts[0]}, )"
        return f"({', '.join(parts)})"

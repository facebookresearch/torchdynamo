import collections
import contextlib
import dataclasses
import functools
import itertools
import logging
import math
import operator
from typing import Dict
from typing import List

import sympy
import torch

import torchinductor

from .. import codecache
from .. import config
from .. import ir
from ..utils import has_triton_libdevice
from ..utils import sympy_dot
from ..utils import sympy_product
from ..virtualized import V
from ..virtualized import ops
from .common import DeferredLine
from .common import ExprPrinter
from .common import IndentedBuffer
from .common import Kernel
from .common import OpOverrides

log = logging.getLogger(__name__)


class TritonPrinter(ExprPrinter):
    def _print_ModularIndexing(self, expr):
        x, div, mod = expr.args
        x = self.paren(self.doprint(x))
        div = self.paren(self.doprint(div))
        mod = self.paren(self.doprint(mod))
        if div != "1":
            x = f"({x} // {div})"
        return f"{x} % {mod}"

    def _print_IndexingDiv(self, expr):
        x, div = expr.args
        x = self.paren(self.doprint(x))
        div = self.paren(self.doprint(div))
        return f"({x} // {div})"


texpr = TritonPrinter().doprint


def triton_compute_type(dtype):
    triton_type_name = str(dtype).split(".")[-1]
    if triton_type_name == "bool":
        triton_type_name = "int1"
    if triton_type_name in ("float16", "bfloat16"):
        # float16 math is done in float32 inside the kernel
        triton_type_name = "float32"
    return f"tl.{triton_type_name}"


def triton_constant(value):
    if value == float("inf"):
        return 'float("inf")'
    elif value == float("-inf"):
        return 'float("-inf")'
    elif math.isnan(value):
        return 'float("nan")'
    return repr(value)


class TritonOverrides(OpOverrides):
    """Map element-wise ops to Triton"""

    @staticmethod
    def to_dtype(x, dtype: torch.dtype):
        if dtype == torch.bool:
            return f"({x} != 0)"
        return f"{x}.to({triton_compute_type(dtype)})"

    @staticmethod
    def constant(value, dtype):
        return triton_constant(value)

    @staticmethod
    def abs(x):
        return f"tl.abs({x})"

    @staticmethod
    def exp(x):
        return f"tl.exp({x})"

    @staticmethod
    def sqrt(x):
        return f"tl.sqrt({x})"

    @staticmethod
    def relu(x):
        return ops.maximum("0", x)

    @staticmethod
    def minimum(a, b):
        return f"tl.minimum({a}, {b})"

    @staticmethod
    def maximum(a, b):
        return f"tl.maximum({a}, {b})"

    @staticmethod
    def where(a, b, c):
        # wonkyness to work around https://github.com/openai/triton/issues/532
        # identity calls to force new triton variables (and get access to .shape/.dtype/.numel
        a = ops.identity(a)
        b = ops.identity(b)
        c = ops.identity(c)
        a = ops.identity(
            f"{a} | tl.zeros({b}.shape, {a}.dtype) if {b}.numel > 1 else {a}"
        )
        a = ops.identity(
            f"{a} | tl.zeros({c}.shape, {a}.dtype) if {c}.numel > 1 else {a}"
        )
        return f"tl.where({a}, {b}, {c})"

    @staticmethod
    def cos(x):
        return f"tl.cos({x})"

    @staticmethod
    def sin(x):
        return f"tl.sin({x})"

    @staticmethod
    def index_expr(expr, dtype):
        return V.kernel.indexing(expr)[0]

    @staticmethod
    def masked(mask, body, other):
        with V.kernel.mask_loads(mask) as new_mask:
            result = body()
        return ops.where(
            new_mask, result, TritonOverrides.constant(other, torch.float32)
        )

    @staticmethod
    def logical_and(a, b):
        return f"{a} & {b}"

    @staticmethod
    def logical_or(a, b):
        return f"{a} | {b}"

    @staticmethod
    def rand(seed, offset, _):  # _ here to keep the contract identical to CPU rand op
        return f"tl.rand({seed}, {offset})"

    @staticmethod
    def log(x):
        if has_triton_libdevice():
            return f"tl.libdevice.log({x}) if {x}.dtype is tl.float64 else tl.log({x})"
        else:
            # workaround https://github.com/openai/triton/issues/543
            return f"tl.log({x}.to(tl.float32))"

    @staticmethod
    def isinf(x):
        if has_triton_libdevice():
            return f"tl.libdevice.isinfd({x}) if {x}.dtype is tl.float64 else tl.libdevice.isinff({x})"
        else:
            return f"{x}+1 == {x}"

    @staticmethod
    def isnan(x):
        if has_triton_libdevice():
            return f"tl.libdevice.isnand({x}) if {x}.dtype is tl.float64 else tl.libdevice.isnanf({x})"
        else:
            return f"{x} != {x}"

    @staticmethod
    def round(x):
        if has_triton_libdevice():
            return f"tl.libdevice.nearbyint({x})"
        else:
            return f"tl.where({x}<0, {x}-0.5, {x}+0.5).to(tl.int32).to(tl.float32)"

    @staticmethod
    def floor(x):
        if has_triton_libdevice():
            return f"tl.libdevice.floor({x})"
        else:
            tmp = ops.trunc(x)
            return f"tl.where({tmp}>{x}, {tmp}-1, {tmp})"

    @staticmethod
    def trunc(x):
        if has_triton_libdevice():
            return f"tl.libdevice.trunc({x})"
        else:
            return f"{x}.to(tl.int32).to(tl.float32)"

    @staticmethod
    def ceil(x):
        return f"tl.libdevice.ceil({x})"


@dataclasses.dataclass
class IterationRanges:
    """
    Each range tree represents multiple sets of iteration indexing
    in a single tiled dimension in the output kernel.

    If you have two loops ranges one (4, 3, 2) and another (4, 6),
    then the range tree will be:
            4 (i0)
        3 (i1)  6 (i3)
        2 (i2)
    Where i0 is shared between both loops, but then the split into
    different indexing vars.  All loop ranges must iterate over
    the same number of elements.
    """

    def __init__(
        self,
        name: str,
        var_list: List[sympy.Symbol],
        var_ranges: Dict[sympy.Symbol, sympy.Expr],
        numel: sympy.Expr,
        prefix: str,
        divisor=sympy.Integer(1),
        length=sympy.Integer(1),
    ):
        super(IterationRanges, self).__init__()
        self.name = name
        self.var_list = var_list
        self.var_ranges = var_ranges
        self.numel = numel
        self.prefix = prefix
        self.divisor = divisor
        self.length = length

    def is_loop(self):
        return self.prefix == "r"


class IterationRangesRoot(IterationRanges):
    def __init__(
        self, name: str, numel: sympy.Expr, prefix: str, index: int, kernel: "Kernel"
    ):
        super(IterationRangesRoot, self).__init__(
            name=name,
            var_list=[],
            var_ranges={},
            numel=numel,
            prefix=prefix,
        )
        self.index = index
        self.kernel = kernel
        # Store all the nodes in one flat list
        self.nodes: Dict[sympy.Expr, IterationRangesEntry] = {}

    def cache_clear(self):
        for node in self.nodes.values():
            node.cache_clear()

    def lookup(self, divisor, length):
        """
        Lookup a given RangeTreeEntry, creating it if needed
        """
        if V.graph.sizevars.maybe_guard_equals(divisor * length, self.numel):
            expr = ir.IndexingDiv(sympy.Symbol(f"{self.prefix}index"), divisor)
        else:
            expr = ir.ModularIndexing(
                sympy.Symbol(f"{self.prefix}index"), divisor, length
            )

        if expr not in self.nodes:
            node = IterationRangesEntry(
                f"{self.prefix}{next(V.kernel.iter_vars_count)}",
                divisor,
                length,
                expr,
                self,
            )
            V.kernel.range_tree_nodes[node.symbol()] = node
            self.var_list.append(node.symbol())
            self.var_ranges[node.symbol()] = length
            self.nodes[expr] = node
        return self.nodes[expr]

    def construct(self, lengths: List[sympy.Expr]):
        divisor = sympy.Integer(1)
        itervars = []
        for length in reversed(lengths):
            itervars.append(self.lookup(divisor, length).symbol())
            divisor = divisor * length
        return list(reversed(itervars))

    def vars_and_sizes(self, index: sympy.Expr):
        """Figure out vars from this tree used in index"""
        nodes = [V.kernel.range_tree_nodes.get(s) for s in index.free_symbols]
        nodes = [n for n in nodes if n and n.prefix == self.prefix]
        nodes.sort(key=lambda x: V.graph.sizevars.size_hint(x.divisor))
        divisor = sympy.Integer(1)
        index_vars = []
        sizes = []

        def add(node):
            nonlocal divisor
            index_vars.append(node.symbol())
            sizes.append(node.length)
            divisor = divisor * node.length

        for node in nodes:
            if not V.graph.sizevars.maybe_guard_equals(node.divisor, divisor):
                # fill in unused index var
                add(self.lookup(divisor, ir.IndexingDiv(node.divisor, divisor)))
                divisor = node.divisor
            add(node)
        if not V.graph.sizevars.maybe_guard_equals(self.numel, divisor):
            # fill in unused index var
            add(self.lookup(divisor, ir.IndexingDiv(self.numel, divisor)))

        return list(reversed(index_vars)), list(reversed(sizes))

    def ranges_code(self):
        size = self.kernel.reshape_size_str(self.index, self.prefix)
        return f"tl.reshape(tl.arange(0, {self.prefix.upper()}BLOCK), {size})"

    def codegen_header(self, code):
        x = self.prefix
        if self.is_loop():
            code.writeline(f"{self.name} = {x}offset + {x}base")
        else:
            code.writelines(
                [
                    f"{x}offset = tl.program_id({self.index}) * {x.upper()}BLOCK",
                    f"{self.name} = {x}offset + {self.ranges_code()}",
                ]
            )
        code.writeline(f"{x}mask = {self.name} < {x}numel")


class IterationRangesEntry(IterationRanges):
    def __init__(
        self,
        name: str,
        divisor: sympy.Expr,
        length: sympy.Expr,
        expr: sympy.Expr,
        parent: IterationRanges,
    ):
        super(IterationRangesEntry, self).__init__(
            name=name,
            numel=parent.numel / length,
            var_list=parent.var_list,
            var_ranges=parent.var_ranges,
            prefix=parent.prefix,
            divisor=divisor,
            length=length,
        )
        self.parent = parent
        self.codegen = functools.lru_cache(None)(self._codegen)
        self.expr = expr

    def cache_clear(self):
        self.codegen.cache_clear()

    def writeline(self, line):
        if self.is_loop():
            V.kernel.indexing_code.writeline(line)
        else:
            # lift non-reduction stores outside loop
            V.kernel.body.writeline(line)

    def _codegen(self):
        self.writeline(f"{self.name} = " + texpr(V.kernel.rename_indexing(self.expr)))
        return self.name

    def symbol(self):
        return sympy.Symbol(self.name)

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return self.name == other.name


def zero_vars(it):
    return {k: 0 for k in it}


class TritonKernel(Kernel):
    overrides = TritonOverrides
    sexpr = texpr

    def __init__(self, *groups):
        super(TritonKernel, self).__init__()
        self.numels = [V.graph.sizevars.simplify(s) for s in groups]
        self.range_trees = []
        self.range_tree_nodes = {}
        self.iter_vars_count = itertools.count()
        self.inside_reduction = self.numels[-1] != 1
        self._load_mask = None
        self.body = IndentedBuffer()
        self.indexing_code = IndentedBuffer()
        self.suffix = IndentedBuffer()
        self.outside_loop_vars = set()
        self.initialize_range_tree()

    def initialize_range_tree(self):
        names = ["xindex", "yindex", "zindex"][: len(self.numels) - 1] + ["rindex"]
        for i in range(len(self.numels)):
            self.range_trees.append(
                IterationRangesRoot(names[i], self.numels[i], names[i][0], i, self)
            )
        for tree in self.range_trees:
            # reduction indexing goes inside a loop
            if tree.prefix != "r":
                tree.codegen_header(self.body)
        if self.inside_reduction and self.range_trees[-1].is_loop():
            # workaround for this issue:
            # https://gist.github.com/jansel/6527126f781559095c5531f98a4235a7
            self.body.writeline(f"rbase = {self.range_trees[-1].ranges_code()}")

    def disable_reduction(self):
        @contextlib.contextmanager
        def ctx():
            if not self.inside_reduction:
                yield
                return
            # calling codegen_body() will flush all the pending buffers
            # and write out a reduction loop
            self.codegen_body()
            self.inside_reduction = False
            yield
            self.inside_reduction = True

        return ctx()

    def set_ranges(self, *lengths):
        assert len(lengths) == len(self.range_trees)
        return [
            ranges.construct(length)
            for length, ranges in zip(lengths, self.range_trees)
        ]

    @staticmethod
    def _split_iteration_ranges(
        groups: List[sympy.Expr], lengths: List[List[sympy.Expr]]
    ):
        sv = V.graph.sizevars
        new_ranges = [[] for _ in groups]
        remaining = [sv.simplify(g) for g in groups]
        var_count = itertools.count()

        def add_range(i, expr):
            expr = sv.simplify(expr)
            if not sv.maybe_guard_multiple_of(remaining[i], expr):
                raise CantSplit()
            # guard on the last item out
            sv.maybe_guard_equals(remaining[i], expr)
            remaining[i] = ir.IndexingDiv(remaining[i], expr)
            new_ranges[i].append(expr)
            return next(var_count)

        def make_combined(size, idx1, idx2):
            def getter(flat_vars):
                return size * flat_vars[idx1] + flat_vars[idx2]

            return getter

        return_getters_groups = []
        current_group = 0
        for length_group in lengths:
            return_getters = []
            for size in length_group:
                if sv.maybe_guard_equals(size, 1):
                    return_getters.append(lambda _: sympy.Integer(0))
                    continue

                while (
                    current_group < len(remaining)
                    and sv.size_hint(remaining[current_group]) == 1
                ):
                    # scroll to next group with remaining elements
                    current_group += 1

                if sv.size_hint(size) > sv.size_hint(remaining[current_group]):
                    # need to break size in two
                    if not sv.maybe_guard_multiple_of(size, remaining[current_group]):
                        raise CantSplit()
                    size1 = remaining[current_group]
                    size2 = ir.IndexingDiv(size, remaining[current_group])
                    return_getters.append(
                        make_combined(
                            size2,
                            add_range(current_group, size1),
                            add_range(current_group + 1, size2),
                        )
                    )
                else:
                    return_getters.append(
                        operator.itemgetter(add_range(current_group, size))
                    )
            return_getters_groups.append(return_getters)

        assert all(
            V.graph.sizevars.size_hint(s) == 1 for s in remaining
        ), f"failed to set ranges {remaining} {lengths}"

        return new_ranges, return_getters_groups

    @classmethod
    def is_compatible(cls, groups: List[sympy.Expr], lengths: List[List[sympy.Expr]]):
        try:
            cls._split_iteration_ranges(groups, lengths)
            return True
        except CantSplit:
            return False

    def split_and_set_ranges(self, lengths: List[List[sympy.Expr]]):
        """
        We may want to fuse `for i0 in s0*s1` into a tiled kernel with groups (s0, s1).

        To do this we need to split up the iteration space of i0 into something like:
            for i1 in s0:
              for i2 in s1:
                i0 = i1*s1 + i2
                ....

        This function matches and resplits lengths to the groups of
        this kernel to enable tiled + non-tiled fusions.
        """
        groups = [rt.numel for rt in self.range_trees]
        if not self.inside_reduction:
            groups[-1] = sympy.Integer(1)

        if len(lengths) == len(self.range_trees) and all(
            V.graph.sizevars.simplify(sympy_product(x) - g) == 0
            for x, g in zip(lengths, groups)
        ):
            return self.set_ranges(*lengths)

        new_ranges, return_getters_groups = self._split_iteration_ranges(
            groups, lengths
        )
        itervars = list(itertools.chain(*self.set_ranges(*new_ranges)))
        return [[fn(itervars) for fn in fns] for fns in return_getters_groups]

    def is_indirect_indexing(self, index: sympy.Expr):
        index_vars = set(index.free_symbols)
        return any(
            # tmpX  means indirect indexing
            str(v).startswith("tmp")
            for v in index_vars
        )

    def combine_contiguous_dims(self, index: sympy.Expr, tree: IterationRangesRoot):
        """
        More aggressive simplification to merge contiguous dims
        """
        index_vars, sizes = tree.vars_and_sizes(index)
        if not sizes:
            return index
        new_sizes, reindex, prune = ir._simplify_loops(
            index_vars,
            sizes,
            [
                index,
                # added contiguous index prevents reordering
                sympy_dot(index_vars, ir.FlexibleLayout.contiguous_strides(sizes)),
            ],
        )
        if new_sizes == sizes:
            return index
        new_index_vars = tree.construct(new_sizes)
        new_index = index.subs(dict(zip(index_vars, reindex(new_index_vars))))
        return new_index

    def indexing(self, index: sympy.Expr, copy_shape=None):
        """
        Compute the index and mask to pass to tl.load() or tl.store()
        """
        index = V.graph.sizevars.simplify_with_ranges(index, self.var_ranges())
        for tree in self.range_trees:
            index = self.combine_contiguous_dims(index, tree)
        index_vars = set(index.free_symbols)
        index_str = texpr(self.rename_indexing(self.codegen_indexing(index)))
        indirect_indexing = self.is_indirect_indexing(index)
        need_dense = (config.triton.dense_indexing or indirect_indexing) and index != 0
        have_dense = True
        have_loop_vars = False
        mask = []
        dense_mask = []

        for tree in self.range_trees:
            if tree.prefix == "r" and not self.inside_reduction:
                continue
            if index_vars.intersection(tree.var_list):
                have_loop_vars = True
                have_dense = False
                mask.append(f"{tree.prefix}mask")
            dense_mask.append(f"{tree.prefix}mask")

        if need_dense and not have_dense:
            mask = dense_mask
            index_str = f"{index_str} + tl.zeros({self.dense_size_str()}, tl.int32)"
        elif not have_loop_vars and copy_shape:
            mask = dense_mask
            index_str = f"{index_str} + tl.zeros({copy_shape}.shape, tl.int32)"
        elif indirect_indexing:
            mask = dense_mask

        if self._load_mask:
            mask.append(self._load_mask)
        elif not mask:
            mask = ["None"]

        return index_str, " & ".join(mask)

    def var_ranges(self):
        return (
            dict(
                itertools.chain.from_iterable(
                    tree.var_ranges.items() for tree in self.range_trees
                )
            ),
        )

    def codegen_indexing(self, expr: sympy.Expr):
        expr = V.graph.sizevars.simplify_with_ranges(expr, self.var_ranges())
        for sym in sorted(expr.free_symbols, key=str):
            if sym in self.range_tree_nodes:
                self.range_tree_nodes[sym].codegen()
        return expr

    @contextlib.contextmanager
    def mask_loads(self, mask):
        """Context manager to add an additional mask to tl.load/store"""
        assert self._load_mask is None, "TODO: nesting"
        prior = self._load_mask
        self._load_mask = mask
        with self.swap_buffers(self.compute, self.compute):
            # TODO(jansel): do we need a reshape here?
            yield mask
        self._load_mask = prior

    def load(self, name: str, index: sympy.Expr, upcast: bool = False):
        var = self.args.input(name)
        indirect_indexing = self.is_indirect_indexing(index)
        index, mask = self.indexing(index)
        if "rmask" in mask:
            # This eviction policy heuristic is untested.
            # ptillet suggested we should try only doing this for
            # the first N-1 loops and not for the final loop.
            ep = ", eviction_policy='evict_last'"
        else:
            ep = ""
        line = f"tl.load({var} + {index}, {mask}{ep})"
        if upcast:
            line += ".to(tl.float32)"

        if self.inside_reduction and "rmask" not in mask and not indirect_indexing:
            # can lift a common load outside of reduction loop
            # One exception is when this is an indirect_load.
            tmp = self.cse.generate(self.body, line)
        else:
            tmp = self.cse.generate(self.loads, line)

        if not self.inside_reduction or "rmask" not in mask:
            self.outside_loop_vars.add(tmp)
        return tmp

    def store(self, name, index, value, mode=None):
        var = self.args.output(name)
        index, mask = self.indexing(index, value)
        if mode is None:
            line = f"tl.store({var} + {index}, {value}, {mask})"
        elif mode == "atomic_add":
            line = f"tl.atomic_add({var} + {index}, {value}, {mask})"
        else:
            raise NotImplementedError(f"store mode={mode}")
        self.stores.writeline(name, line)
        if not self.inside_reduction:
            self.outside_loop_vars.add(value)

    def reduction(self, name, dtype, reduction_type, index, value):
        assert self.inside_reduction
        default = triton_constant(ir.Reduction.default_value(reduction_type, dtype))
        masks = [f"{tree.prefix}mask" for tree in self.range_trees]
        if self._load_mask:
            masks.append(self._load_mask)
        sizes = [f"{tree.prefix.upper()}BLOCK" for tree in self.range_trees]
        sizes[-1] = "1"
        if reduction_type == "any":
            reduction_type = "max"

        dim = len(self.range_trees) - 1
        result_var = self.cse.newvar()
        if (dtype, reduction_type, value) not in self.cse.reduction_cache:
            self.cse.reduction_cache[(dtype, reduction_type, value)] = result_var
            accumulator = f"_{result_var}"
            self.body.writeline(
                f"{accumulator} = tl.zeros({self.dense_size_str()}, {triton_compute_type(dtype)}) + {default}"
            )

            updated = value
            if reduction_type == "min":
                masks.append(f"({accumulator} > {value})")
            elif reduction_type == "max":
                masks.append(f"({accumulator} < {value})")
            elif reduction_type == "sum":
                updated = f"{accumulator} + {value}"
            else:
                raise NotImplementedError(f"reduction_type {reduction_type}")

            cond = " & ".join(masks)
            self.compute.writeline(
                f"{accumulator} = tl.where({cond}, {updated}, {accumulator})"
            )

            self.suffix.writeline(
                f"{result_var} = tl.reshape(tl.{reduction_type}({accumulator}, {dim}), [{', '.join(sizes)}])"
            )
        else:
            var_name = self.cse.reduction_cache[(dtype, reduction_type, value)]
            self.suffix.writeline(f"{result_var} = {var_name}")
        self.inside_reduction = False
        index, mask = self.indexing(index, result_var)
        assert "rmask" not in index
        self.inside_reduction = True
        self.outside_loop_vars.add(result_var)
        self.cse.store_cache[name] = result_var
        if name not in V.graph.removed_buffers:
            var = self.args.output(name)
            self.suffix.writeline(
                DeferredLine(name, f"tl.store({var} + {index}, {result_var}, {mask})")
            )

    def codegen_body(self):
        """
        Concat output code from index_code, loads, compute, stores,
        suffix into self.body.

        For pointwise kernels, this is called just once at the end.

        For reduction kernels, this generates a loop over the reduction
        axis.
        """
        if not (
            self.indexing_code
            or self.loads
            or self.stores
            or self.compute
            or self.suffix
        ):
            return

        if self.inside_reduction:
            self.body.writeline("for roffset in range(0, rnumel, RBLOCK):")
            with self.body.indent():
                # last range tree is always reduction
                self.range_trees[-1].codegen_header(self.body)
                self.body.splice(self.indexing_code)
                self.body.splice(self.loads)
                self.body.splice(self.compute)
                self.body.splice(self.stores)

            # invalidate any caches that came from inside the reduction loop
            self.cse.invalidate(self.outside_loop_vars)
            self.range_trees[-1].cache_clear()
        else:
            self.body.splice(self.indexing_code)
            self.body.splice(self.loads)
            self.body.splice(self.compute)
            self.body.splice(self.stores)
        self.body.splice(self.suffix)
        self.indexing_code.clear()
        self.loads.clear()
        self.compute.clear()
        self.stores.clear()
        self.suffix.clear()

    def codegen_kernel(self, name=None):
        from triton import next_power_of_2

        code = IndentedBuffer()
        size_hints = [
            next_power_of_2(V.graph.sizevars.size_hint(numel)) for numel in self.numels
        ]
        if self.inside_reduction:
            heuristics = "reduction_heuristics"
        else:
            heuristics = "pointwise_heuristics"
            size_hints = size_hints[:-1]

        if name is None:
            code.splice(
                f"""
                    import triton
                    import triton.language as tl
                    from {codecache.__name__} import {heuristics}

                """
            )

        code.splice(
            f"""
                @{heuristics}(size_hints={size_hints!r})
                @triton.jit
            """
        )

        argdefs, _ = self.args.python_argdefs()

        if config.dynamic_shapes:
            maybe_const = ""
        else:
            maybe_const = ": tl.constexpr"

        for tree in self.range_trees:
            if tree.prefix != "r" or self.inside_reduction:
                argdefs.append(f"{tree.prefix}numel{maybe_const}")

        for tree in self.range_trees:
            if tree.prefix != "r" or self.inside_reduction:
                argdefs.append(f"{tree.prefix.upper()}BLOCK : tl.constexpr")

        code.writeline(f"def {name or 'kernel'}({', '.join(argdefs)}):")
        self.codegen_body()
        with code.indent():
            for old, new in self.args.aliases():
                code.writeline(f"{old} = {new}")
            code.splice(self.body)

        if name is not None:
            return code.getvalue()

        wrapper = IndentedBuffer()
        wrapper.writeline("TritonCodeCache.load('''")
        wrapper.splice(code.getvalue(), strip=True)
        wrapper.writeline("''').kernel")
        return wrapper.getvalue()

    def reshape_size_str(self, i=None, x=None):
        sizes = ["1"] * (len(self.range_trees) - int(self.numels[-1] == 1))
        if i is not None:
            sizes[i] = f"{x.upper()}BLOCK"
        return f"[{', '.join(sizes)}]"

    def dense_size_str(self):
        sizes = []
        for tree in self.range_trees:
            if tree.prefix != "r" or self.inside_reduction:
                sizes.append(f"{tree.prefix.upper()}BLOCK")
            elif tree.prefix == "r" and tree.numel != 1:
                sizes.append("1")
        return f"[{', '.join(sizes)}]"

    def call_kernel(self, code, name: str):
        _, call_args = self.args.python_argdefs()
        grid = []
        # TODO(jansel): if there are constants, we shouldn't bother passing them as args
        for tree in self.range_trees:
            if isinstance(tree.numel, (sympy.Integer, sympy.Symbol)):
                expr = texpr(tree.numel)
            else:
                expr = f"{name}_{tree.prefix}numel"
                code.writeline(f"{expr} = {texpr(tree.numel)}")
            if tree.prefix != "r" or self.inside_reduction:
                call_args.append(expr)
            if tree.prefix != "r":
                grid.append(expr)
        call_args = ", ".join(call_args)
        code.writeline(f"{name}[grid({', '.join(grid)})]({call_args})")


class TritonScheduling:
    def __init__(self, scheduler):
        self.scheduler = scheduler

    def group_fn(self, sizes):
        return tuple(V.graph.sizevars.simplify(sympy_product(s)) for s in sizes)

    def group_fn_NHW_C(self, sizes):
        # group to size of NHW, C for 4d tensor
        group = ()
        for s in sizes:
            if len(s) == 4:
                group += (
                    V.graph.sizevars.simplify(sympy_product([s[0], s[2], s[3]])),
                    s[1],
                )
            else:
                group += (V.graph.sizevars.simplify(sympy_product(s)),)
        return group

    def group_fn_M_N(self, sizes):
        group = ()
        for s in sizes:
            if len(s) == 2:
                group += (s[0], s[1])
            else:
                group += (V.graph.sizevars.simplify(sympy_product(s)),)
        return group

    def create_node_schedule_pointwise(self, numel: sympy.Expr):
        """
        Get a list of SchedulerNode to execute in a single triton kernel.

        `numel` is the number of elements in the input/output
        """
        node_schedule = []
        for node in self.scheduler.pop_group(
            (numel, sympy.Integer(1)),
        ):
            node.mark_run()
            node_schedule.append(node)
            node.mark_fusable()
        return node_schedule

    def create_node_schedule_reduction(
        self, numel: sympy.Expr, reduction_numel: sympy.Expr
    ):
        """
        Get a list of SchedulerNode to execute in a single triton kernel.

        `numel * reduction_numel` is the elements in the input
        `numel` is the number of elements in the output
        """
        node_schedule = []
        # nodes with incompatible dimensions we failed to schedule
        nodes_to_reschedule = []

        for _ in self.scheduler.iter_fixed_point():
            for node in self.scheduler.pop_group(
                (numel * reduction_numel, sympy.Integer(1)),
            ):
                if TritonKernel.is_compatible(
                    (numel, reduction_numel), node.get_ranges()
                ):
                    node.mark_run()
                    node_schedule.append(node)
                    node.mark_fusable()
                else:
                    log.debug(
                        "rescheduling due to not is_compatible(%s, %s)",
                        (numel, reduction_numel),
                        node.get_ranges(),
                    )
                    nodes_to_reschedule.append(node)

            # scheduler.pop_group will keep iterating all reachable fusable nodes
            reductions_to_mark_fusable = []
            for node in self.scheduler.pop_group((numel, reduction_numel)):
                node.mark_run()
                node_schedule.append(node)
                reductions_to_mark_fusable.append(node)
            # mark reductions fusable later as they rely on the loop break below
            for node in reductions_to_mark_fusable:
                node.mark_fusable(broadcast_after_reduce=True)

            node_schedule.append(DisableReduction)  # close reduction loop
            # Add more pointwise with fewer dimensions
            for node in self.scheduler.pop_group((numel, sympy.Integer(1))):
                node.mark_run()
                node_schedule.append(node)
                node.mark_fusable()
            node_schedule.append(EnableReduction)  # open new reduction loop

            if self.is_better_tiling_ready(numel, reduction_numel):
                # early exit to prevent a fusion that would result in worse tiling
                break

        self.scheduler.enqueue(nodes_to_reschedule)
        return node_schedule

    def codegen(self, numel, reduction_numel):
        """
        Generate a single triton kernel.  If reduction_numel != 1 this is
        a reduction kernel, otherwise pointwise.
        """
        if reduction_numel == 1:
            node_schedule = self.create_node_schedule_pointwise(numel)
        else:
            if self.is_better_tiling_ready(numel, reduction_numel):
                # preempt this reduction kernel with a tiled pointwise
                self.codegen(numel * reduction_numel, sympy.Integer(1))
            node_schedule = self.create_node_schedule_reduction(numel, reduction_numel)

        nodes = [
            n
            for n in node_schedule
            if isinstance(n, torchinductor.scheduler.SchedulerNode)
        ]
        log.info(
            f"codegen numel={numel} reduction_numel={reduction_numel} nodes={len(nodes)}"
        )

        tiled_groups = self.select_tiling(node_schedule, numel, reduction_numel)

        with self.scheduler.kernel(TritonKernel(*tiled_groups)) as kernel:
            stack = contextlib.ExitStack()
            for node in node_schedule:
                if node is DisableReduction:
                    stack.enter_context(kernel.disable_reduction())
                elif node is EnableReduction:
                    stack.close()
                else:
                    node.codegen(kernel.split_and_set_ranges(node.get_ranges()))

        wrapper = V.graph.wrapper_code
        if config.triton.many_files:
            kernel_name = wrapper.next_kernel_name()
            wrapper.define_kernel(kernel_name, kernel.codegen_kernel())
        else:
            src_code = kernel.codegen_kernel("{kernel_name}")
            if src_code in wrapper.kernels:
                kernel_name = wrapper.kernels[src_code]
            else:
                kernel_name = wrapper.next_kernel_name()
                wrapper.kernels[src_code] = kernel_name
                code = src_code.format(kernel_name=kernel_name)
                wrapper.header.splice(code)
        kernel.call_kernel(wrapper, kernel_name)

        self.scheduler.barrier()
        self.scheduler.maybe_free_buffers()

    @staticmethod
    @functools.lru_cache(32)
    def select_node_tiling(node):
        ranges, reduction_ranges = node.get_ranges()
        if len(ranges) <= 1:
            return None

        rw = node.pointwise_read_writes()
        assert len(rw.range_vars) == len(ranges)

        deps = [
            dep
            for dep in itertools.chain(rw.reads, rw.writes)
            if dep.name not in V.graph.removed_buffers
        ]
        strides = [
            V.graph.sizevars.stride_hints(dep.index, rw.range_vars) for dep in deps
        ]

        if strides:
            tiled_ranges = ir.ComputedBuffer._tile_contiguous(ranges, strides)
            tiled_groups = tuple(
                V.graph.sizevars.simplify(sympy_product(x)) for x in tiled_ranges
            )
            if len(tiled_ranges) > 1:
                return tiled_groups

            # TODO(jansel): ir.ComputedBuffer._tile_broadcasting()?

    @staticmethod
    def select_tiling(node_schedule, numel, reduction_numel):
        """
        Heuristics to decide how to tile kernels.
        Currently, we tile based on stride-1 dimensions.

        Returns:
            `(tile1, tile2, reduction_numel)` s.t. `tile1 * tile2 == numel`

        """
        if reduction_numel != 1:
            # TODO(jansel): should we tile reductions?
            return (numel, reduction_numel)

        candidate_tiles = collections.Counter()
        for node in EnableReduction.filter(node_schedule):
            tiled_groups = TritonScheduling.select_node_tiling(node)
            if tiled_groups:
                candidate_tiles[tiled_groups] += 1

        # TODO(jansel): join two 2D tiles into a 3D tile
        # TODO(jansel): add a cost function for tiling instead of most_common
        for tiled_groups, count in candidate_tiles.most_common():
            new_groups = (*tiled_groups, reduction_numel)
            if all(
                TritonKernel.is_compatible(new_groups, node.get_ranges())
                for node in node_schedule
                if isinstance(node, torchinductor.scheduler.SchedulerNode)
            ):
                return new_groups
        return (numel, reduction_numel)

    def is_better_tiling_ready(self, numel, reduction_numel):
        """
        Check for a pending node wanting a different tiling strategy
        than the given reduction.
        """
        better_tiled = False
        nodes_to_reschedule = []
        for node in self.scheduler.pop_group(
            (numel * reduction_numel, sympy.Integer(1))
        ):
            tiling = self.select_node_tiling(node)
            if tiling and tuple(tiling) != (numel, reduction_numel):
                better_tiled = True
            nodes_to_reschedule.append(node)
        self.scheduler.enqueue(nodes_to_reschedule)
        return better_tiled

    def flush(self):
        pass


class DisableReduction:
    """
    Marker to invoke `kernel.disable_reduction()`.  This closes a
    reduction loop and allows for pointwise ops to occur on the output
    of a reduction.
    """


class EnableReduction:
    """
    Marker to end a DisableReduction block.
    """

    @staticmethod
    def filter(node_schedule):
        """
        Get the nodes from node_schedule skipping those in a
        DisableReduction block.
        """
        disabled = False
        for node in node_schedule:
            if node in (EnableReduction, DisableReduction):
                # Don't tile stuff outside the main reduction loop
                disabled = node is DisableReduction
            elif disabled:
                pass
            else:
                yield node


class CantSplit(Exception):
    pass

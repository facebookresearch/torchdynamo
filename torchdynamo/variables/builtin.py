import functools
import inspect
import itertools
import math
import operator
import types
from typing import Dict
from typing import List

import torch

from torchdynamo.guards import GuardBuilder
from torchdynamo.variables.tensor import DynamicShapeVariable

from .. import config
from .. import variables
from ..allowed_functions import is_allowed
from ..exc import Unsupported
from ..exc import unimplemented
from ..source import AttrSource
from ..source import TypeSource
from ..utils import check_constant_args
from ..utils import istype
from ..utils import proxy_args_kwargs
from .base import MutableLocal
from .base import VariableTracker


class BuiltinVariable(VariableTracker):
    @staticmethod
    @functools.lru_cache(None)
    def _constant_fold_functions():
        fns = {
            abs,
            all,
            any,
            bool,
            callable,
            chr,
            dict,
            divmod,
            float,
            int,
            len,
            list,
            max,
            min,
            ord,
            pow,
            repr,
            round,
            set,
            str,
            str.format,
            sum,
            tuple,
            type,
            operator.pos,
            operator.neg,
            operator.not_,
            operator.invert,
            operator.pow,
            operator.mul,
            operator.matmul,
            operator.floordiv,
            operator.truediv,
            operator.mod,
            operator.add,
            operator.sub,
            operator.getitem,
            operator.lshift,
            operator.rshift,
            operator.and_,
            operator.or_,
            operator.xor,
            operator.ipow,
            operator.imul,
            operator.imatmul,
            operator.ifloordiv,
            operator.itruediv,
            operator.imod,
            operator.iadd,
            operator.isub,
            operator.ilshift,
            operator.irshift,
            operator.iand,
            operator.ixor,
            operator.ior,
            operator.index,
        }
        fns.update(x for x in math.__dict__.values() if isinstance(x, type(math.sqrt)))
        return fns

    def can_constant_fold_through(self):
        return self.fn in self._constant_fold_functions()

    @staticmethod
    @functools.lru_cache(None)
    def _fx_graph_functions():
        fns = {
            operator.pos,
            operator.neg,
            operator.not_,
            operator.invert,
            operator.pow,
            operator.mul,
            operator.matmul,
            operator.floordiv,
            operator.truediv,
            operator.mod,
            operator.add,
            operator.sub,
            operator.getitem,
            operator.lshift,
            operator.rshift,
            operator.and_,
            operator.or_,
            operator.xor,
            operator.ipow,
            operator.imul,
            operator.imatmul,
            operator.ifloordiv,
            operator.itruediv,
            operator.imod,
            operator.iadd,
            operator.isub,
            operator.ilshift,
            operator.irshift,
            operator.iand,
            operator.ixor,
            operator.ior,
        }
        return fns

    def can_insert_in_graph(self):
        return self.fn in self._fx_graph_functions()

    def __init__(self, fn, **kwargs):
        super(BuiltinVariable, self).__init__(**kwargs)
        self.fn = fn

    def __str__(self):
        return f"{self.__class__.__name__}({self.fn.__name__})"

    def python_type(self):
        return type(self.fn)

    def as_python_constant(self):
        return self.fn

    def reconstruct(self, codegen):
        name = self.fn.__name__
        assert self.fn.__module__ == "builtins"
        assert name not in codegen.tx.f_globals, "shadowed global"
        return [codegen.create_load_global(name, add=True)]

    def constant_args(self, *args, **kwargs):
        return check_constant_args(args, kwargs)

    def tensor_args(self, *args, **kwargs):
        return any(
            isinstance(i, variables.TensorVariable)
            for i in itertools.chain(args, kwargs.values())
        )

    def wrap_as_unspecialized_if_needed(self, tensor_variable, *args, **kwargs):
        """
        Wrap a `TensorVariable` as an `UnspecializedNumpyVariable` or `UnspecializedPythonVariable` if needed.
        """
        if all(
            isinstance(
                i,
                (
                    variables.UnspecializedNumpyVariable,
                    variables.UnspecializedPythonVariable,
                    variables.ConstantVariable,
                ),
            )
            for i in itertools.chain(args, kwargs.values())
        ):
            if any(
                isinstance(x, variables.UnspecializedNumpyVariable)
                for x in itertools.chain(args, kwargs.values())
            ):
                return variables.UnspecializedNumpyVariable.from_tensor_variable(
                    tensor_variable
                )
            else:
                need_unwrap = any(
                    x.need_unwrap
                    for x in itertools.chain(args, kwargs.values())
                    if isinstance(x, variables.UnspecializedPythonVariable)
                )
                return variables.UnspecializedPythonVariable.from_tensor_variable(
                    tensor_variable, need_unwrap
                )
        else:
            return tensor_variable

    def call_function(
        self, tx, args: "List[VariableTracker]", kwargs: "Dict[str, VariableTracker]"
    ) -> "VariableTracker":
        constant_args = check_constant_args(args, kwargs)
        tensor_args = self.tensor_args(*args, **kwargs)
        options = VariableTracker.propagate(self, args, kwargs.values())
        has_constant_handler = self.can_constant_fold_through() and constant_args
        assert isinstance(args, list)
        assert isinstance(kwargs, dict)

        if (
            self.fn is operator.getitem
            and len(args) == 2
            and isinstance(args[1], variables.TensorVariable)
            and args[1].dtype == torch.bool
            and not config.dynamic_shapes
        ):
            unimplemented("dynamic Tensor.__getitem__(bool[])")

        if self.can_insert_in_graph() and tensor_args:
            try:
                fn = self.fn
                if self.fn is operator.iadd and isinstance(
                    args[0], variables.ConstantVariable
                ):
                    # Work around weird bug in hf_T5
                    fn, args = operator.add, [args[1], args[0]]
                result = variables.TensorVariable.create(
                    tx,
                    tx.output.create_proxy(
                        "call_function",
                        fn,
                        *proxy_args_kwargs(args, kwargs),
                    ),
                    **options,
                )
                return self.wrap_as_unspecialized_if_needed(result, *args, **kwargs)

            except NotImplementedError:
                unimplemented(f"partial tensor op: {self} {args} {kwargs}")

        # Handle cases like int(torch.seed())
        if self.fn is int and isinstance(args[0], DynamicShapeVariable):
            return args[0]

        handler = getattr(self, f"call_{self.fn.__name__}", None)

        if handler:
            try:
                inspect.signature(handler).bind(tx, *args, **kwargs)
            except TypeError as exc:
                if config.debug:
                    print("WARN: incorrect arg count", handler, exc)
                handler = None

        if handler:
            try:
                result = handler(tx, *args, **kwargs)
                if result is not None:
                    return result.add_options(options)
            except Unsupported as exc:
                if not has_constant_handler:
                    raise
                # Actually, we will handle this just fine
                exc.remove_from_stats()

        if has_constant_handler:
            # constant fold
            return variables.ConstantVariable(
                self.as_python_constant()(
                    *[x.as_python_constant() for x in args],
                    **{k: v.as_python_constant() for k, v in kwargs.items()},
                ),
                **options,
            )

        return super().call_function(tx, args, kwargs)

    def _call_min_max(self, tx, a, b):
        if self.tensor_args(a, b):
            if not isinstance(a, variables.TensorVariable):
                a, b = b, a
            assert isinstance(a, variables.TensorVariable)

            # convert min/max to torch ops
            if b.is_python_constant():
                kwargs = {"min": b} if (self.fn is max) else {"max": b}
                result = variables.TorchVariable(torch.clamp).call_function(
                    tx, [a], kwargs
                )
            else:
                fn = {max: torch.maximum, min: torch.minimum}[self.fn]
                result = variables.TorchVariable(fn).call_function(tx, [a, b], {})

            return self.wrap_as_unspecialized_if_needed(result, a, b)
        else:
            unimplemented(f"unsupported min / max over args {str(a)}, {str(b)}")

    call_min = _call_min_max
    call_max = _call_min_max

    def call_range(self, tx, *args, **kwargs):
        if self.constant_args(*args, **kwargs):
            return variables.RangeVariable(
                value=range(
                    *[x.value for x in args],
                    **{k: v.value for k, v in kwargs.items()},
                ),
            )

    def call_slice(self, tx, *args):
        return variables.SliceVariable(args)

    def _call_iter_tuple_list(self, tx, obj=None):
        cls = variables.BaseListVariable.cls_for(self.fn)
        if obj is None:
            return cls(
                [],
                mutable_local=MutableLocal(),
            )
        elif obj.has_unpack_var_sequence(tx):
            guards = set()
            if obj.source:
                guards.add(obj.source.create_guard(GuardBuilder.LIST_LENGTH))
            return cls(
                list(obj.unpack_var_sequence(tx)),
                mutable_local=MutableLocal(),
                guards=guards,
            ).add_options(self, obj)

    call_iter = _call_iter_tuple_list
    call_tuple = _call_iter_tuple_list
    call_list = _call_iter_tuple_list

    def call_zip(self, tx, *args):
        options = VariableTracker.propagate(self, args)
        if all(x.has_unpack_var_sequence(tx) for x in args):
            items = [
                variables.TupleVariable(list(item), **options)
                for item in zip(*[arg.unpack_var_sequence(tx) for arg in args])
            ]
            return variables.TupleVariable(items, **options)

    def call_enumerate(self, tx, *args):
        options = VariableTracker.propagate(self, args)
        if len(args) == 1:
            start = 0
        else:
            assert len(args) == 2
            assert isinstance(args[1], variables.ConstantVariable)
            start = args[1].as_python_constant()
        if args[0].has_unpack_var_sequence(tx):
            items = [
                variables.TupleVariable(
                    [variables.ConstantVariable(idx, **options), var],
                    **options,
                )
                for idx, var in enumerate(args[0].unpack_var_sequence(tx), start)
            ]
            return variables.TupleVariable(items, **options)

    def call_mul(self, tx, a, b):
        if isinstance(
            a, (variables.ListVariable, variables.TupleVariable)
        ) and isinstance(b, variables.ConstantVariable):
            return a.__class__(
                items=a.items * b.as_python_constant(), mutable_local=MutableLocal()
            ).add_options(self, a, b)
        elif isinstance(
            b, (variables.ListVariable, variables.TupleVariable)
        ) and isinstance(a, variables.ConstantVariable):
            return b.__class__(
                items=b.items * a.as_python_constant(), mutable_local=MutableLocal()
            ).add_options(self, a, b)
        else:
            return a.call_method(tx, "__mul__", [b], {})

    def call_len(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__len__", args[1:], kwargs)

    def call_add(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__add__", args[1:], kwargs)

    def call_sub(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__sub__", args[1:], kwargs)

    def call_truediv(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__truediv__", args[1:], kwargs)

    def call_floordiv(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__floordiv__", args[1:], kwargs)

    def call_iadd(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__iadd__", args[1:], kwargs)

    def call_getitem(self, tx, *args, **kwargs):
        return args[0].call_method(tx, "__getitem__", args[1:], kwargs)

    def call_isinstance(self, tx, arg, isinstance_type):
        arg_type = arg.python_type()
        isinstance_type = isinstance_type.as_python_constant()

        if isinstance(arg, variables.TensorVariable) and arg.dtype is not None:
            return variables.ConstantVariable(arg.call_isinstance(isinstance_type))
        # UserDefinedObject with C extensions can have torch.Tensor attributes,
        # so break graph.
        if isinstance(arg, variables.UserDefinedObjectVariable) and isinstance(
            arg.value, types.MemberDescriptorType
        ):
            unimplemented(
                f"isinstance called on UserDefinedClass {arg} {isinstance_type}"
            )
        try:
            val = issubclass(arg_type, isinstance_type)
        except TypeError:
            val = arg_type is isinstance_type
        return variables.ConstantVariable(val)

    def call_super(self, tx, a, b):
        return variables.SuperVariable(a, b)

    def call_next(self, tx, arg):
        if isinstance(arg, variables.ListIteratorVariable):
            val, next_iter = arg.next_variables()
            tx.replace_all(arg, next_iter)
            return val
        elif isinstance(arg, variables.BaseListVariable):
            return arg.items[0].add_options(self, arg)

    def call_hasattr(self, tx, obj, attr):
        if attr.is_python_constant():
            name = attr.as_python_constant()
            return obj.call_hasattr(tx, name).add_options(self, obj, attr)

    def call_map(self, tx, fn, seq):
        if seq.has_unpack_var_sequence(tx):
            items = [fn.call_function(tx, [x], {}) for x in seq.unpack_var_sequence(tx)]
            return variables.TupleVariable(items).add_options(self, fn, seq)

    def call_sum(self, tx, seq, **kwargs):
        if seq.has_unpack_var_sequence(tx):
            start = kwargs.pop(
                "start", variables.ConstantVariable(0)
            ).as_python_constant()
            assert not kwargs
            items = seq.unpack_var_sequence(tx)[start:]
            return BuiltinVariable(functools.reduce).call_function(
                tx,
                [
                    BuiltinVariable(operator.add),
                    variables.TupleVariable(items),
                    variables.ConstantVariable(0).add_options(self, seq),
                ],
                {},
            )

    def call_reduce(self, tx, function, iterable, initializer=None):
        if iterable.has_unpack_var_sequence(tx):
            items = iterable.unpack_var_sequence(tx)
            if initializer is None:
                value, items = items[0], items[1:]
            else:
                value = initializer
            for element in items:
                value = function.call_function(tx, [value, element], {})
            return value

    def call_getattr(
        self, tx, obj: VariableTracker, name_var: VariableTracker, default=None
    ):
        from . import ConstantVariable
        from . import GetAttrVariable
        from . import PythonModuleVariable
        from . import TorchVariable
        from . import UserFunctionVariable
        from .builder import VariableBuilder

        options = VariableTracker.propagate(self, obj, name_var)
        guards = options["guards"]
        name = name_var.as_python_constant()

        if not name_var.is_python_constant():
            unimplemented("non-const getattr() name")

        if tx.output.side_effects.is_attribute_mutation(obj):
            try:
                # re-read a pending side effect?
                return tx.output.side_effects.load_attr(obj, name).add_options(options)
            except KeyError:
                pass

        if default is not None:
            hasattr_var = self.call_hasattr(tx, obj, name_var)
            guards.update(hasattr_var.guards)
            assert hasattr_var.as_python_constant() in (True, False)
            if not hasattr_var.as_python_constant():
                return default.add_guards(guards)

        if obj.source:
            source = AttrSource(obj.source, name)
            options["source"] = source
        else:
            source = None

        if isinstance(obj, variables.NNModuleVariable):
            return obj.var_getattr(tx, name).add_options(options)
        elif isinstance(obj, variables.TensorVariable) and name == "grad":
            if source:
                example_value = obj.proxy.node.meta["example_value"].grad
                return VariableBuilder(tx, source)(example_value).add_options(options)
            else:
                unimplemented("tensor grad")
        elif isinstance(
            obj,
            (
                variables.TensorVariable,
                variables.NamedTupleVariable,
                variables.ConstantVariable,
                variables.UserDefinedClassVariable,
                variables.UserDefinedObjectVariable,
            ),
        ):
            try:
                return (
                    obj.var_getattr(tx, name).clone(source=source).add_options(options)
                )
            except NotImplementedError:
                return GetAttrVariable(obj, name, **options)
        elif isinstance(obj, TorchVariable):
            member = getattr(obj.value, name)
            if is_allowed(member):
                return TorchVariable(member, **options)
            elif ConstantVariable.is_literal(member):
                return ConstantVariable(member, **options)
            else:
                return VariableBuilder(tx, source)(member).add_guards(guards)
        elif isinstance(obj, PythonModuleVariable):
            member = obj.value.__dict__[name]
            return VariableBuilder(tx, source)(member).add_guards(guards)
        elif istype(obj, UserFunctionVariable) and name in ("__name__", "__module__"):
            return ConstantVariable(
                getattr(obj.fn, name), **VariableTracker.propagate(obj)
            )
        else:
            try:
                return (
                    obj.var_getattr(tx, name).clone(source=source).add_options(options)
                )
            except NotImplementedError:
                return GetAttrVariable(obj, name, **options)

    def call_setattr(
        self, tx, obj: VariableTracker, name_var: VariableTracker, val: VariableTracker
    ):
        if isinstance(obj, (variables.BlackHoleVariable, variables.DataClassVariable)):
            return obj.call_method(tx, "__setattr__", [name_var, val], {})
        elif (
            tx.output.side_effects.is_attribute_mutation(obj)
            and name_var.is_python_constant()
        ):
            tx.output.side_effects.store_attr(obj, name_var.as_python_constant(), val)
            return val.add_options(self, obj, name_var)
        elif isinstance(obj, variables.UserDefinedObjectVariable):
            unimplemented(
                f"setattr(UserDefinedObjectVariable) {type(obj.value).__setattr__}"
            )
        elif isinstance(obj, variables.NNModuleVariable):
            obj.convert_to_unspecialized(tx)

    def call_type(self, tx, obj: VariableTracker):
        from .builder import VariableBuilder

        try:
            py_type = obj.python_type()
        except NotImplementedError:
            py_type = None

        if istype(obj, variables.TupleVariable):
            return BuiltinVariable(py_type).add_options(self, obj)

        if py_type is not None and obj.source:
            return VariableBuilder(tx, TypeSource(obj.source))(py_type).add_options(
                self, obj
            )

        unimplemented(f"type({obj})")

    def call_reversed(self, tx, obj: VariableTracker):
        if obj.has_unpack_var_sequence(tx):
            items = list(reversed(obj.unpack_var_sequence(tx)))
            return variables.TupleVariable(
                items, **VariableTracker.propagate(self, obj)
            )

    def call_chain(self, tx, *args):
        if all(obj.has_unpack_var_sequence(tx) for obj in args):
            items = []
            for obj in args:
                items.extend(obj.unpack_var_sequence(tx))
            return variables.TupleVariable(
                items, **VariableTracker.propagate(self, *args)
            )

    def call_islice(self, tx, iterable, *args):
        if iterable.has_unpack_var_sequence(tx) and all(
            x.is_python_constant() for x in args
        ):
            const_args = [x.as_python_constant() for x in args]
            items = iterable.unpack_var_sequence(tx)
            items = list(itertools.islice(items, *const_args))
            return variables.TupleVariable(
                items, **VariableTracker.propagate(self, iterable, *args)
            )

    def call_id(self, tx, *args):
        if len(args) > 0 and isinstance(args[0], variables.NNModuleVariable):
            nn_mod_variable = args[0]
            mod = tx.output.get_submodule(nn_mod_variable.module_key)
            return variables.ConstantVariable(id(mod))
        else:
            unimplemented(f"call_id with args {args}")

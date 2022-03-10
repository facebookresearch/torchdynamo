import dis
import functools
import inspect
import itertools
import os
import sys
import traceback
import types
import typing
from typing import Callable
from typing import List

import torch
from torch import fx
from torch.fx.graph_module import _forward_from_src as original_forward_from_src

from . import config
from ._eval_frame import skip_code
from .bytecode_analysis import remove_dead_code
from .bytecode_analysis import remove_pointless_jumps
from .bytecode_transformation import is_generator
from .bytecode_transformation import transform_code_object
from .guards import GuardedCode
from .symbolic_convert import InstructionTranslator
from .utils import CleanupManager
from .utils import RestartAnalysis
from .utils import Unsupported
from .utils import counters
from .utils import unimplemented


class SkipContext:
    enabled = False

    @staticmethod
    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args):
            prior = SkipContext.enabled
            SkipContext.enabled = True
            try:
                return fn(*args)
            finally:
                SkipContext.enabled = prior

        return inner


class Tracker:
    def __init__(self):
        self.seen = []
        self.seen_ids = set()

    def add(self, obj):
        if obj not in self:
            self.seen.append(obj)
            self.seen_ids.add(id(obj))

    def __contains__(self, item):
        return id(item) in self.seen_ids

    def clear(self):
        self.seen.clear()
        self.seen_ids.clear()


input_codes = Tracker()
output_codes = Tracker()


@functools.wraps(original_forward_from_src)
def fx_forward_from_src_skip_result(*args, **kwargs):
    # we monkey patch FX to prevent infinite loop of trying to convert
    # our generated code
    result: types.FunctionType = original_forward_from_src(*args, **kwargs)
    skip_code(result.__code__)
    return result


def wrap_compiler_fn(compiler_fn):
    """Shim to convert 1 arg to 2 arg compiler_fn"""
    if len(inspect.signature(compiler_fn).parameters) == 1:
        # older 1-arg version

        @functools.wraps(compiler_fn)
        def inner(gm: fx.GraphModule, example_inputs: List):
            return compiler_fn(gm)

        return inner
    else:
        return compiler_fn


def wrap_convert_context(fn):
    """
    Context manager to:
        1) Save/restore torch random state
        2) Save/restore torch.is_grad_enabled() state
        3) Monkey patch torch.fx.graph_module._forward_from_src
    """

    @functools.wraps(fn)
    def _fn(*args, **kwargs):
        prior_grad_mode = torch.is_grad_enabled()
        rng_state = torch.clone(torch.random.get_rng_state())
        prior_fwd_from_src = torch.fx.graph_module._forward_from_src
        torch.fx.graph_module._forward_from_src = fx_forward_from_src_skip_result
        try:
            return fn(*args, **kwargs)
        finally:
            torch._C._set_grad_enabled(prior_grad_mode)
            torch.random.set_rng_state(rng_state)
            torch.fx.graph_module._forward_from_src = prior_fwd_from_src

    return _fn


def convert_frame_assert(compiler_fn: Callable, one_graph=True):
    """Fully convert a frame into an FX graph"""
    compiler_fn = wrap_compiler_fn(compiler_fn)

    def _convert_frame_assert(frame: types.FrameType, cache_size: int):
        code = frame.f_code
        input_codes.add(code)
        if code in output_codes or SkipContext.enabled:
            return None
        if (
            os.environ.get("TORCHDYNAMO_DEBUG_FUNCTION")
            and os.environ.get("TORCHDYNAMO_DEBUG_FUNCTION") != code.co_name
        ):
            return None
        if code.co_name == "<genexpr>" and code.co_filename.endswith(
            "transformers/file_utils.py"
        ):
            # not needed, but cleans up torchbench error stats
            return None
        if is_generator(code):
            unimplemented("generator")
        if cache_size >= config.cache_size_limit:
            unimplemented("cache_size_limit reached")
        output = None

        # from .utils import print_once;  print_once(code.co_filename)

        def transform(instructions, code_options):
            nonlocal output
            tracer = InstructionTranslator(
                instructions,
                frame.f_code,
                frame.f_locals,
                frame.f_globals,
                frame.f_builtins,
                code_options,
                compiler_fn,
                one_graph,
            )
            tracer.run()
            output = tracer.output
            assert output.output_instructions
            instructions[:] = output.output_instructions
            code_options.update(output.code_options)

            if config.dead_code_elimination:
                instructions[:] = remove_pointless_jumps(remove_dead_code(instructions))

        try:
            for attempt in itertools.count():
                try:
                    code = transform_code_object(frame.f_code, transform)
                    break
                except RestartAnalysis:
                    if attempt > 100:
                        unimplemented("100+ RestartAnalysis() calls")
            output_codes.add(code)
            if config.debug:
                print(
                    "\nORIGINAL BYTECODE",
                    code.co_name,
                    code.co_filename,
                    code.co_firstlineno,
                )
                # print(dis.Bytecode(frame.f_code).info())
                print(dis.Bytecode(frame.f_code).dis())
                print("MODIFIED BYTECODE")
                # print(dis.Bytecode(code).info())
                print(dis.Bytecode(code).dis())
                print("\nGUARDS:")
                for guard in sorted(output.guards):
                    print(" -", str(guard))
                print()
            assert output.guards is not None
            CleanupManager.instance[code] = output.cleanups
            return GuardedCode(code, output.guards, frame.f_locals, frame.f_globals)
        except Exception as e:
            if config.debug:
                print(
                    "\nWONT CONVERT",
                    e,
                    code.co_name,
                    code.co_filename,
                    code.co_firstlineno,
                )
                # print(dis.Bytecode(frame.f_code).info())
                print(dis.Bytecode(frame.f_code).dis())
                traceback.print_exc()
            raise

    return wrap_convert_context(_convert_frame_assert)


def convert_frame(compiler_fn: typing.Callable):
    """Try to convert a frame into an FX graph, if error leave frame unmodified"""
    inner_convert = convert_frame_assert(compiler_fn, one_graph=False)

    def _convert_frame(frame: types.FrameType, cache_size: int):
        counters["frames"]["total"] += 1
        try:
            result = inner_convert(frame, cache_size)
            counters["frames"]["ok"] += 1
            return result
        except Unsupported:
            pass
        except Exception:
            sys.stderr.write("=" * 10 + " Stack Trace " + "=" * 10 + "\n")
            traceback.print_exc()
            if config.debug:
                sys.stderr.write(
                    "=" * 10 + " Exception (above) while processing " + "=" * 10 + "\n"
                )
                sys.stderr.write(
                    dis.Bytecode(frame.f_code).info()
                    + "\n"
                    + dis.Bytecode(frame.f_code).dis()
                )
                sys.stderr.write("=" * 10 + " End debug info " + "=" * 10 + "\n")
        return None

    return _convert_frame

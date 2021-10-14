import dis
import os.path
import sys
import types
import unittest

import torch

import torchdynamo
from torchdynamo import eval_frame
from torchdynamo.bytecode_transformation import create_instruction, is_generator
from torchdynamo.bytecode_transformation import debug_checks
from torchdynamo.bytecode_transformation import transform_code_object
from torchdynamo.convert_frame import convert_frame_assert
from torchdynamo.guards import GuardedCode

unsupported = torchdynamo._eval_frame.unsupported


def exc_bytecode_offset():
    dis.Bytecode.from_traceback(sys.exc_info()[2]).current_offset


def same(a, b):
    """Check correctness to see if a and b match"""
    if isinstance(a, (list, tuple)):
        assert isinstance(b, (list, tuple))
        return all(same(ai, bi) for ai, bi in zip(a, b))
    elif isinstance(a, torch.Tensor):
        assert isinstance(b, torch.Tensor)
        return torch.allclose(a, b, atol=1e-4, rtol=1e-4)
    elif isinstance(a, (int, float, type(None), bool)):
        return a == b
    elif type(a).__name__ == "SquashedNormal":
        return same(a.mean, b.mean)
    elif type(a).__name__ in (
        "MaskedLMOutput",
        "Seq2SeqLMOutput",
        "CausalLMOutputWithCrossAttentions",
        "LongformerMaskedLMOutput",
    ):
        assert type(a) is type(b)
        return all(same(getattr(a, key), getattr(b, key)) for key in a.__dict__.keys())
    else:
        raise RuntimeError(f"unsupported type: {type(a).__name__}")


def debug_dir():
    path = os.path.join(os.path.dirname(__file__), "../debug")
    if not os.path.exists(path):
        os.mkdir(path)
    return path


def debug_dump(name, code: types.CodeType, extra=""):
    with open(os.path.join(debug_dir(), name), "w") as fd:
        fd.write(
            f"{dis.Bytecode(code).info()}\n\n{dis.Bytecode(code).dis()}\n\n{extra}\n"
        )


def debug_insert_nops(frame):
    """used to debug jump updates"""

    def insert_nops(instructions, code_options):
        instructions.insert(0, create_instruction("NOP"))
        instructions.insert(0, create_instruction("NOP"))

    if is_generator(frame.f_code):
        return None

    debug_checks(frame.f_code)
    code = transform_code_object(frame.f_code, insert_nops)

    return GuardedCode(code)


class CompileCounter:
    def __init__(self):
        self.frame_count = 0
        self.op_count = 0

    def __call__(self, gm: torch.fx.GraphModule):
        self.frame_count += 1
        for node in gm.graph.nodes:
            if "call" in node.op:
                self.op_count += 1
        return gm.forward


def standard_test(self, fn, nargs, expected_ops=None):
    actual = CompileCounter()
    if expected_ops is None:
        expected = CompileCounter()
        try:
            gm = torch.fx.symbolic_trace(fn)
            expected(gm)
            print("\nfx.symbolic_trace graph:")
            gm.graph.print_tabular()
            expected_ops = expected.op_count
        except Exception:
            pass  # Silently ignore FX errors (not our issue)

    args1 = [torch.randn(10, 10) for _ in range(nargs)]
    args2 = [torch.randn(10, 10) for _ in range(nargs)]
    correct1 = fn(*args1)
    correct2 = fn(*args2)
    with eval_frame.optimize(convert_frame_assert(actual)):
        val1a = fn(*args1)
        val2a = fn(*args2)
        val1b = fn(*args1)
        val2b = fn(*args2)
    self.assertTrue(same(val1a, correct1))
    self.assertTrue(same(val1b, correct1))
    self.assertTrue(same(val2a, correct2))
    self.assertTrue(same(val2b, correct2))
    self.assertEqual(actual.frame_count, 1)
    if expected_ops is not None:
        self.assertEqual(actual.op_count, expected_ops)


class TestCase(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        torchdynamo.reset()
        torchdynamo.config.debug = cls.prior_debug

    @classmethod
    def setUpClass(cls):
        torchdynamo.reset()
        cls.prior_debug = torchdynamo.config.debug
        torchdynamo.config.debug = True

    def setUp(self):
        torchdynamo.symbolic_convert.counters.clear()

    def tearDown(self):
        for k, v in torchdynamo.symbolic_convert.counters.items():
            print(k, v.most_common())
        torchdynamo.symbolic_convert.counters.clear()

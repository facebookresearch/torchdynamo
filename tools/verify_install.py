import glob
import os
import re
import subprocess
import sys
import traceback

from pkg_resources import packaging

MIN_CUDA_VERSION = packaging.version.parse("11.6")
MIN_PYTHON_VERSION = (3, 7)
# NOTE: dev version required, e.g. Version('1.13.0.dev123') < Version('1.13.0')
MIN_TORCH_VERSION = packaging.version.parse("1.13.0.dev0")


class VerifyInstallError(BaseException):
    pass


# from torch/utils/cpp_extension.py
IS_WINDOWS = sys.platform == "win32"
SUBPROCESS_DECODE_ARGS = ("oem",) if IS_WINDOWS else ()

# from torch/utils/cpp_extension.py
def find_cuda_home():
    import torch

    r"""Finds the CUDA install path."""
    # Guess #1
    cuda_home = os.environ.get("CUDA_HOME") or os.environ.get("CUDA_PATH")
    if cuda_home is None:
        # Guess #2
        try:
            which = "where" if IS_WINDOWS else "which"
            with open(os.devnull, "w") as devnull:
                nvcc = (
                    subprocess.check_output([which, "nvcc"], stderr=devnull)
                    .decode(*SUBPROCESS_DECODE_ARGS)
                    .rstrip("\r\n")
                )
                cuda_home = os.path.dirname(os.path.dirname(nvcc))
        except Exception:
            # Guess #3
            if IS_WINDOWS:
                cuda_homes = glob.glob(
                    "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v*.*"
                )
                if len(cuda_homes) == 0:
                    cuda_home = ""
                else:
                    cuda_home = cuda_homes[0]
            else:
                cuda_home = "/usr/local/cuda"
            if not os.path.exists(cuda_home):
                cuda_home = None
    if cuda_home and not torch.cuda.is_available():
        print(
            f"No CUDA runtime is found, using CUDA_HOME='{cuda_home}'", file=sys.stderr
        )
    return cuda_home


# from torch/utils/cpp_extension.py
def get_cuda_version():
    CUDA_HOME = find_cuda_home()
    if not CUDA_HOME:
        raise VerifyInstallError(
            "CUDA was not found on the system, please set the CUDA_HOME or the CUDA_PATH"
            "environment variable or add NVCC to your system PATH. The extension compilation will fail."
        )

    nvcc = os.path.join(CUDA_HOME, "bin", "nvcc")
    cuda_version_str = (
        subprocess.check_output([nvcc, "--version"])
        .strip()
        .decode(*SUBPROCESS_DECODE_ARGS)
    )
    cuda_version = re.search(r"release (\d+[.]\d+)", cuda_version_str)
    if cuda_version is None:
        raise VerifyInstallError("CUDA version not found in `nvcc --version` output")

    cuda_str_version = cuda_version.group(1)
    return packaging.version.parse(cuda_str_version)


def check_python():
    if sys.version_info < MIN_PYTHON_VERSION:
        raise VerifyInstallError(
            f"Python version not supported: {sys.version_info} "
            f"- minimum requirement: {MIN_PYTHON_VERSION}"
        )
    return sys.version_info


def check_torchdynamo():
    proc = subprocess.run(
        ["python", "-m", "pip", "show", "torchdynamo"], capture_output=True
    )
    if proc.returncode != 0:
        raise VerifyInstallError("`torchdynamo` is not installed")

    dynamo_version = re.search(r"Version: (.*)", proc.stdout.decode("utf-8"))
    dynamo_version_str = dynamo_version.group(1)
    if not dynamo_version_str:
        raise VerifyInstallError("`torchdynamo` version not found in `pip show` output")

    return packaging.version.parse(dynamo_version_str)


# Checks for correct pip dependencies, according to `install_requires` in setup.py.
def check_pip_deps():
    proc = subprocess.run(["python", "-m", "pip", "check"], capture_output=True)
    if proc.returncode != 0:
        dynamo_broken_deps = []
        for line in proc.stdout.decode("utf-8").strip().split("\n"):
            if line.startswith("torchdynamo"):
                dynamo_broken_deps.append(line)
        if len(dynamo_broken_deps) > 0:
            raise VerifyInstallError(
                "`torchdynamo` requirements not met:\n"
                + "\n".join(map(str, dynamo_broken_deps))
            )


# Checks for correct torch version.
# Using check_pip_deps does not work if the minimum required torch version
# is not present in PyPI.
def check_torch():
    import torch

    if packaging.version.parse(torch.__version__) < MIN_TORCH_VERSION:
        raise VerifyInstallError(
            f"`torch` version not supported: {torch.__version__} "
            f"- minimum requirement: {MIN_TORCH_VERSION}"
        )

    return torch.__version__


def check_cuda():
    import torch

    if not torch.cuda.is_available():
        return None

    torch_cuda_ver = packaging.version.parse(torch.version.cuda)
    if torch_cuda_ver < MIN_CUDA_VERSION:
        raise VerifyInstallError(
            f"CUDA version not supported: {torch_cuda_ver} "
            f"- minimum requirement: {MIN_CUDA_VERSION}"
        )

    # check if torch cuda version matches system cuda version
    cuda_ver = get_cuda_version()
    if cuda_ver != torch_cuda_ver:
        raise VerifyInstallError(
            f"CUDA version mismatch, torch version: {torch_cuda_ver}, env version: {cuda_ver}"
        )

    return cuda_ver


def check_dynamo(backend, device, err_msg):
    import torch

    if device == "cuda" and not torch.cuda.is_available():
        print(f"CUDA not available -- skipping CUDA check on {backend} backend\n")
        return

    try:
        import torchdynamo

        torchdynamo.reset()

        @torchdynamo.optimize(backend, nopython=True)
        def fn(x):
            return x + x

        class Module(torch.nn.Module):
            def __init__(self):
                super().__init__()

            def forward(self, x):
                return x + x

        mod = Module()
        opt_mod = torchdynamo.optimize(backend, nopython=True)(mod)

        for f in (fn, opt_mod):
            x = torch.randn(10, 10).to(device)
            x.requires_grad = True
            y = f(x)
            torch.testing.assert_close(y, x + x)
            z = y.sum()
            z.backward()
            torch.testing.assert_close(x.grad, 2 * torch.ones_like(x))
    except Exception:
        sys.stderr.write(traceback.format_exc() + "\n" + err_msg + "\n\n")
        sys.exit(1)


_SANITY_CHECK_ARGS = (
    ("eager", "cpu", "CPU eager sanity check failed"),
    ("eager", "cuda", "CUDA eager sanity check failed"),
    ("aot_eager", "cpu", "CPU aot_eager sanity check failed"),
    ("aot_eager", "cuda", "CUDA aot_eager sanity check failed"),
    ("inductor", "cpu", "CPU inductor sanity check failed"),
    (
        "inductor",
        "cuda",
        "CUDA inductor sanity check failed\n"
        + "NOTE: Please check that you installed the correct hash/version of `triton`",
    ),
)


def main():
    python_ver = check_python()
    torchdynamo_ver = check_torchdynamo()
    check_pip_deps()
    torch_ver = check_torch()
    cuda_ver = check_cuda()
    print(
        f"Python version: {python_ver.major}.{python_ver.minor}.{python_ver.micro}\n"
        f"`torch` version: {torch_ver}\n"
        f"CUDA version: {cuda_ver}\n"
        f"`torchdynamo` version: {torchdynamo_ver}"
    )
    for args in _SANITY_CHECK_ARGS:
        check_dynamo(*args)
    print("All required checks passed")


if __name__ == "__main__":
    main()

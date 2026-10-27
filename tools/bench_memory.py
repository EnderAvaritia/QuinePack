"""Measure QuinePack peak working set and wall time for one payload.

Usage:
    python tools/bench_memory.py --size 256 --format 7z

The payload is a file of incompressible random bytes created under
``_tmp/bench`` (git-ignored).  Results are printed to stdout; the exit code is
the CLI exit code.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quinepack import cli


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _peak_working_set_mib() -> float:
    if os.name == "nt":
        counters = _ProcessMemoryCounters()
        size = ctypes.sizeof(counters)
        counters.cb = size
        function = getattr(
            ctypes.windll.psapi, "K32GetProcessMemoryInfo", None
        ) or ctypes.windll.psapi.GetProcessMemoryInfo
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.c_uint32,
        ]
        function.restype = ctypes.c_int
        ctypes.windll.kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        process = ctypes.windll.kernel32.GetCurrentProcess()
        if not function(process, ctypes.byref(counters), size):
            raise ctypes.WinError()
        return counters.PeakWorkingSetSize / (1 << 20)

    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1 << 20 if sys.platform == "darwin" else 1 << 10
    return usage / divisor


def _make_payload(path: Path, size: int) -> None:
    if path.exists() and path.stat().st_size == size:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        for _ in range(size >> 20):
            handle.write(os.urandom(1 << 20))
        remainder = size % (1 << 20)
        if remainder:
            handle.write(os.urandom(remainder))
        handle.flush()
        os.fsync(handle.fileno())


def _artifact(outdir: Path, name: str, format_key: str) -> Path:
    suffix = {"7z": ".7z", "7z-store": ".store.7z", "txz": ".tar.xz"}[format_key]
    return outdir / f"{name}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, required=True, help="payload size in MiB")
    parser.add_argument("--format", required=True, choices=("7z", "7z-store", "txz"))
    parser.add_argument("--workdir", type=Path, default=Path("_tmp/bench"))
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--7zr", dest="seven_zip", default=None)
    args = parser.parse_args()

    size = args.size << 20
    payload = args.workdir / f"random-{args.size}.bin"
    _make_payload(payload, size)
    outdir = args.workdir / "out"

    command = [
        "pack",
        str(payload),
        "--format",
        args.format,
        "--outdir",
        str(outdir),
        "--quiet",
        "--force",
    ]
    if args.verify:
        command.append("--verify")
    if args.seven_zip is not None:
        command.extend(["--7zr", args.seven_zip])

    started = time.perf_counter()
    code = cli.main(command)
    elapsed = time.perf_counter() - started
    artifact = _artifact(outdir, payload.name, args.format)
    artifact_size = artifact.stat().st_size if artifact.exists() else -1
    print(
        f"format={args.format} payload={args.size}MiB code={code} "
        f"time={elapsed:.1f}s peak={_peak_working_set_mib():.0f}MiB "
        f"artifact={artifact_size}B"
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
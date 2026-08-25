"""
================================================================================
calibrate_external.py  -  Part 2a: External-Reference Calibration (no Linux needed)
================================================================================
Answers the reviewer question: "How do you know YOUR measurement is correct?"

THE IDEA
    Do not compare our profiler against another *sampling* profiler (that only
    compares two guesses). Compare it against the value the OS KERNEL ITSELF
    recorded - the authoritative peak-memory accounting that the operating
    system maintains for every process it runs:

        Windows : GetProcessMemoryInfo() -> PROCESS_MEMORY_COUNTERS.PeakWorkingSetSize
                  (Win32 kernel API; the OS's own high-water mark)
        macOS   : /usr/bin/time -l  -> "maximum resident set size"
        Linux   : /usr/bin/time -v  -> "Maximum resident set size"
                  (both read the kernel's rusage.ru_maxrss)

    These are the SAME class of reference as Valgrind/GNU-time and are available
    on Windows and macOS, so the calibration experiment can be run WITHOUT a
    Linux box or a Raspberry Pi.

WHAT IT PROVES
    Our profiler samples RSS at ~1 ms intervals; the kernel tracks the true peak
    continuously. If our sampled peak tracks the kernel's true peak with
    R^2 >= 0.95 and slope ~ 1.0, then our sampling is dense enough to catch the
    real high-water mark - i.e. the "measuring tape" is calibrated against a
    reference we did not write.

USAGE
    python calibrate_external.py                  # 5 workloads x 3 repeats
    python calibrate_external.py --repeats 5
    python calibrate_external.py --out results/calibration.json

Python 3.9+   -   stdlib only (psutil optional, not required here)
================================================================================
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# ============================================================================
# §1  KERNEL-TRUTH READERS (one per OS)
# ============================================================================

def _kernel_peak_windows(cmd: list) -> tuple:
    """
    Run cmd; return (peak_bytes_from_kernel, wall_s).
    Uses the Win32 GetProcessMemoryInfo API on the child's handle, which reports
    PeakWorkingSetSize - the kernel's own continuously-maintained high-water mark.
    """
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    GetProcessMemoryInfo = psapi.GetProcessMemoryInfo
    GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                                     wintypes.DWORD]
    GetProcessMemoryInfo.restype = wintypes.BOOL

    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    wall = time.perf_counter() - t0

    # proc._handle stays valid until the Popen object is garbage-collected.
    counters = PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
    ok = GetProcessMemoryInfo(int(proc._handle), ctypes.byref(counters), counters.cb)
    if not ok:
        raise OSError(f"GetProcessMemoryInfo failed: {ctypes.get_last_error()}")
    return int(counters.PeakWorkingSetSize), wall


_TIME_RE = re.compile(r"^\s*([\d,]+)\s+maximum resident set size", re.I | re.M)


def _kernel_peak_unix(cmd: list) -> tuple:
    """
    Run cmd under /usr/bin/time -l (macOS) or -v (Linux); parse the kernel's
    'maximum resident set size' from rusage.
    NOTE: macOS reports BYTES; Linux reports KILOBYTES.
    """
    flag = "-l" if sys.platform == "darwin" else "-v"
    t0 = time.perf_counter()
    proc = subprocess.run(["/usr/bin/time", flag] + cmd,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    wall = time.perf_counter() - t0
    err = proc.stderr or ""

    m = _TIME_RE.search(err)
    if not m:
        m = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", err)
        if not m:
            raise RuntimeError("Could not parse 'maximum resident set size' from "
                               f"/usr/bin/time output:\n{err[:500]}")
        return int(m.group(1)) * 1024, wall           # Linux: kB -> bytes

    raw = int(m.group(1).replace(",", ""))
    # macOS ru_maxrss is in bytes; Linux in kB. Guard with a sanity check.
    return (raw if sys.platform == "darwin" else raw * 1024), wall


def kernel_peak(cmd: list) -> tuple:
    if os.name == "nt":
        return _kernel_peak_windows(cmd)
    return _kernel_peak_unix(cmd)


# ============================================================================
# §2  OUR PROFILER'S MEASUREMENT (perf_bench.py, imported as a library)
# ============================================================================

def our_peak(cmd: list, trials: int = 3) -> tuple:
    """Return (rss_peak_net_bytes_mean, wall_s_mean) from our own profiler."""
    from perf_bench import PerfBench, ProfilerConfig

    cfg = ProfilerConfig(cmd_list=cmd, trials=trials, warmup_drop_trials=1)
    report = PerfBench(cfg).run()
    tr = report.steady_state_trials or ([report.cold_start_trial]
                                        if report.cold_start_trial else [])
    if not tr:
        raise RuntimeError("profiler returned no trials")
    return (statistics.mean(t.rss_peak_net_bytes for t in tr),
            statistics.mean(t.wall_time_s for t in tr))


# ============================================================================
# §3  WORKLOADS - allocate a known, increasing amount of memory
# ============================================================================

WORKLOAD_SRC = """
import sys, time
mb = int(sys.argv[1])
# Touch every page so the allocation becomes RESIDENT (not just reserved).
buf = bytearray(mb * 1024 * 1024)
for i in range(0, len(buf), 4096):
    buf[i] = 1
time.sleep(0.15)
print(len(buf))
"""


def make_workload_file() -> Path:
    p = Path(tempfile.gettempdir()) / "greenprof_calib_workload.py"
    p.write_text(WORKLOAD_SRC, encoding="utf-8")
    return p


# ============================================================================
# §4  STATISTICS
# ============================================================================

def linfit(xs, ys) -> tuple:
    """OLS y = m*x + b -> (slope, intercept, r2, pearson_r)."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0 or syy == 0:
        return 0.0, my, 0.0, 0.0
    m = sxy / sxx
    b = my - m * mx
    ss_res = sum((y - (m * x + b)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / syy
    r = sxy / math.sqrt(sxx * syy)
    return m, b, r2, r


# ============================================================================
# §5  MAIN
# ============================================================================

def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="External-reference calibration of the profiler")
    ap.add_argument("--sizes", type=int, nargs="+", default=[25, 50, 100, 200, 400],
                    help="workload sizes in MB")
    ap.add_argument("--repeats", type=int, default=3, help="kernel-reference repeats per size")
    ap.add_argument("--trials", type=int, default=3, help="profiler trials per size")
    ap.add_argument("--out", default="results/calibration_external.json")
    ap.add_argument("--md", default="results/calibration_external.md")
    args = ap.parse_args()

    wl = make_workload_file()
    # Use the BASE interpreter, never a virtualenv shim. On Windows a venv's
    # Scripts/python.exe is a ~4 MB redirector that re-execs the real
    # interpreter as a CHILD process: the kernel counters for the process we
    # spawned would then describe the shim (~4 MB), not the 400 MB worker.
    # The workload is stdlib-only, so the base interpreter is sufficient.
    py = getattr(sys, "_base_executable", None) or sys.executable
    if py != sys.executable:
        print(f"  (using base interpreter, not the venv shim)")

    print("=" * 74)
    print("  EXTERNAL-REFERENCE CALIBRATION")
    print(f"  Platform : {platform.system()} {platform.release()} ({platform.machine()})")
    ref = ("Win32 GetProcessMemoryInfo -> PeakWorkingSetSize" if os.name == "nt"
           else f"/usr/bin/time {'-l' if sys.platform == 'darwin' else '-v'} -> max RSS (kernel rusage)")
    print(f"  Reference: {ref}")
    print(f"  Our tool : perf_bench.py (psutil sampling)")
    print("=" * 74)

    rows = []
    for mb in args.sizes:
        cmd = [py, str(wl), str(mb)]

        peaks = []
        for _ in range(args.repeats):
            pk, _ = kernel_peak(cmd)
            peaks.append(pk)
        kern = statistics.median(peaks)

        ours, wall = our_peak(cmd, trials=args.trials)

        ratio = ours / kern if kern else 0.0
        rows.append({"workload_mb": mb, "kernel_peak_bytes": kern,
                     "our_peak_net_bytes": ours, "ratio": ratio,
                     "our_wall_s": wall})
        print(f"\n  [{mb:>4} MB]  kernel={kern/1048576:8.1f} MB   "
              f"ours(net)={ours/1048576:8.1f} MB   ratio={ratio:5.3f}")

    xs = [r["kernel_peak_bytes"] / 1048576 for r in rows]
    ys = [r["our_peak_net_bytes"] / 1048576 for r in rows]
    slope, intercept, r2, pearson = linfit(xs, ys)

    print("\n" + "=" * 74)
    print("  REGRESSION  (ours = slope * kernel + intercept), MB")
    print(f"    slope     = {slope:.4f}      (1.000 == perfect agreement in scale)")
    print(f"    intercept = {intercept:+.2f} MB  (constant observer offset)")
    print(f"    R^2       = {r2:.4f}")
    print(f"    Pearson r = {pearson:.4f}")
    verdict = "PASS" if (r2 >= 0.95 and 0.8 <= slope <= 1.2) else "REVIEW"
    print(f"    VERDICT   = {verdict}   (target: R^2 >= 0.95, slope 0.8-1.2)")
    print("=" * 74)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "platform": {"system": platform.system(), "release": platform.release(),
                     "machine": platform.machine(), "python": platform.python_version()},
        "reference_method": ref,
        "rows": rows,
        "regression": {"slope": slope, "intercept_mb": intercept,
                       "r2": r2, "pearson_r": pearson, "verdict": verdict},
    }
    outp = Path(args.out); outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    md = ["# External-Reference Calibration\n",
          f"_Platform: {platform.system()} {platform.release()} ({platform.machine()}), "
          f"Python {platform.python_version()}_  ",
          f"_Reference: {ref}_\n",
          "| Workload (MB) | Kernel peak (MB) | Our net peak (MB) | Ratio |",
          "|---:|---:|---:|---:|"]
    for r in rows:
        md.append(f"| {r['workload_mb']} | {r['kernel_peak_bytes']/1048576:.1f} | "
                  f"{r['our_peak_net_bytes']/1048576:.1f} | {r['ratio']:.3f} |")
    md += ["", f"**Regression:** ours = {slope:.4f} x kernel {intercept:+.2f} MB  ",
           f"**R² = {r2:.4f}**, Pearson r = {pearson:.4f} -> **{verdict}**", ""]
    Path(args.md).write_text("\n".join(md), encoding="utf-8")

    print(f"\n[OK] JSON -> {outp}")
    print(f"[OK] MD   -> {args.md}")


if __name__ == "__main__":
    main()

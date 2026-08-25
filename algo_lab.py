"""
================================================================================
algo_lab.py  -  Case-Study Micro-Benchmark Harness
================================================================================
Green Software Diagnostics Architecture - the algorithm-level companion to the
whole-program isolated-process profiler (perf_bench.py).

WHY A SECOND ENGINE?
    perf_bench.py profiles an entire *program* from the outside (RSS via psutil).
    That is the right tool for a model or a service, but its ~30 MB interpreter
    footprint and OS-scheduling noise swamp the signal of a tiny algorithm.
    For algorithm-vs-algorithm comparison we instead measure IN-PROCESS with:
        - time.perf_counter_ns()  for latency  (timer-overhead subtracted)
        - tracemalloc             for peak heap (interpreter footprint excluded
                                   BY CONSTRUCTION - intrinsic baseline isolation)
    Both feed the SAME statistical pipeline (N-trial averaging, z-score outlier
    removal, CV) and the SAME green_metrics energy/carbon/grade model.

EXPERIMENTAL CONTROLS (the "measuring-tape" rigour)
    1. Baseline subtraction
         - Time:   a null-call baseline is timed under identical looping and
                   subtracted   ->   Net = Measured - Overhead.
         - Memory: tracemalloc only counts allocations made AFTER it starts, so
                   the interpreter/stdlib baseline is excluded automatically.
    2. Replication:   up to 30 trials per cell (auto-reduced only when a single
                      execution exceeds the per-cell time budget; actual count
                      is recorded).
    3. Outlier control:  z-score filter (|z| > 2.5 removed) before aggregation.
    4. Reproducibility:  RNG seeded per (case, size).
    5. Warm-up:          one discarded call before timing (cold-start isolation).

Python 3.9+   -   stdlib only (imports green_metrics)
================================================================================
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import random
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import green_metrics as gm


# ============================================================================
# §1  THE ALGORITHMS  (canonical, deliberately un-optimised reference forms)
# ============================================================================

def bubble_sort(a: list) -> list:
    """O(n^2) time, O(1) extra space. Classic adjacent-swap sort."""
    n = len(a)
    for i in range(n):
        swapped = False
        for j in range(0, n - i - 1):
            if a[j] > a[j + 1]:
                a[j], a[j + 1] = a[j + 1], a[j]
                swapped = True
        if not swapped:
            break
    return a


def quick_sort(a: list) -> list:
    """O(n log n) average time. Simple Lomuto-style recursive partition."""
    if len(a) <= 1:
        return a
    pivot = a[len(a) // 2]
    left = [x for x in a if x < pivot]
    mid = [x for x in a if x == pivot]
    right = [x for x in a if x > pivot]
    return quick_sort(left) + mid + quick_sort(right)


def linear_search(a: list, key) -> int:
    """O(n) time. Scan until found."""
    for i, v in enumerate(a):
        if v == key:
            return i
    return -1


def binary_search(a: list, key) -> int:
    """O(log n) time. Requires a sorted list."""
    lo, hi = 0, len(a) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if a[mid] == key:
            return mid
        if a[mid] < key:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1


def fib_naive(n: int) -> int:
    """O(phi^n) time - exponential double recursion. Feasible only for small n."""
    if n < 2:
        return n
    return fib_naive(n - 1) + fib_naive(n - 2)


def fib_dp(n: int) -> int:
    """O(n) time, O(n) space - bottom-up dynamic programming."""
    if n < 2:
        return n
    table = [0] * (n + 1)
    table[1] = 1
    for i in range(2, n + 1):
        table[i] = table[i - 1] + table[i - 2]
    return table[n]


# ============================================================================
# §2  WORKLOAD DESCRIPTORS
# ============================================================================

@dataclass
class Workload:
    """
    Bundles an algorithm with an input factory so timing can exclude input
    construction. `make_input(n)` returns the argument tuple for `call`.
    """
    case: str
    label: str                       # human name, e.g. "Bubble Sort"
    complexity: str                  # theoretical Big-O, e.g. "O(n^2)"
    call: Callable
    make_input: Callable[[int], tuple]
    sizes: list


def _sorted_list(n: int, seed: int) -> list:
    rng = random.Random(seed)
    return sorted(rng.sample(range(n * 4), n))


def _shuffled_list(n: int, seed: int) -> list:
    rng = random.Random(seed)
    data = list(range(n))
    rng.shuffle(data)
    return data


def build_workloads(sort_sizes, search_sizes, fib_sizes, dp_extra_sizes) -> list:
    """Construct the six case-study workloads."""
    wl: list = []

    # -- Case Study 1: Sorting (input excluded from timing; fresh copy per call)
    wl.append(Workload(
        "sort", "Bubble Sort", "O(n^2)",
        call=lambda arr: bubble_sort(arr),
        make_input=lambda n: (_shuffled_list(n, seed=1000 + n),),
        sizes=list(sort_sizes),
    ))
    wl.append(Workload(
        "sort", "Quick Sort", "O(n log n)",
        call=lambda arr: quick_sort(arr),
        make_input=lambda n: (_shuffled_list(n, seed=1000 + n),),
        sizes=list(sort_sizes),
    ))

    # -- Case Study 2: Searching (worst case: key absent -> full scan / full depth)
    wl.append(Workload(
        "search", "Linear Search", "O(n)",
        call=lambda arr, key: linear_search(arr, key),
        make_input=lambda n: (_sorted_list(n, seed=2000 + n), -1),
        sizes=list(search_sizes),
    ))
    wl.append(Workload(
        "search", "Binary Search", "O(log n)",
        call=lambda arr, key: binary_search(arr, key),
        make_input=lambda n: (_sorted_list(n, seed=2000 + n), -1),
        sizes=list(search_sizes),
    ))

    # -- Case Study 3: Fibonacci
    wl.append(Workload(
        "fib", "Naive Recursive Fibonacci", "O(phi^n)",
        call=lambda n: fib_naive(n),
        make_input=lambda n: (n,),
        sizes=list(fib_sizes),
    ))
    wl.append(Workload(
        "fib", "Dynamic-Programming Fibonacci", "O(n)",
        call=lambda n: fib_dp(n),
        make_input=lambda n: (n,),
        sizes=sorted(set(list(fib_sizes) + list(dp_extra_sizes))),
    ))
    return wl


# ============================================================================
# §3  STATISTICAL CORE  (shared methodology with perf_bench.py)
# ============================================================================

def zscore_filter(values: list, threshold: float = 2.5) -> tuple:
    """Remove |z| > threshold. Returns (clean, n_removed)."""
    if len(values) < 3:
        return values, 0
    m = statistics.mean(values)
    try:
        s = statistics.stdev(values)
    except statistics.StatisticsError:
        return values, 0
    if s == 0.0:
        return values, 0
    clean = [v for v in values if abs((v - m) / s) <= threshold]
    return clean, len(values) - len(clean)


@dataclass
class CellResult:
    case: str
    label: str
    complexity: str
    n: int
    trials_run: int
    outliers_removed: int
    time_mean_ms: float
    time_std_ms: float
    time_cv: float
    time_ci95_ms: tuple
    mem_mean_bytes: float
    mem_std_bytes: float
    energy_j: float
    carbon_g: float
    power_w: float

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["time_ci95_ms"] = list(self.time_ci95_ms)
        return d


# ============================================================================
# §4  MEASUREMENT ENGINE
# ============================================================================

def _time_once(call, args) -> float:
    """Single timed call in seconds, using the ns clock."""
    t0 = time.perf_counter_ns()
    call(*args)
    return (time.perf_counter_ns() - t0) / 1e9


def measure_time_cell(
    wl: Workload, n: int, *, budget_s: float, max_trials: int, min_trials: int,
    z_threshold: float,
) -> tuple:
    """
    Returns (time_samples_ms:list, trials_run:int, outliers:int).

    Adaptive replication + inner repetition + null-baseline subtraction.
    """
    args0 = wl.make_input(n)
    wl.call(*args0)                                   # warm-up (discarded)

    est = _time_once(wl.call, wl.make_input(n))       # rough single-call estimate
    est = max(est, 1e-7)
    # Inner reps so each measurement clears timer noise (aim >= 5 ms), capped.
    reps = 1 if est >= 0.005 else min(2000, max(1, int(0.005 / est)))
    # Auto-scale trial count to the per-cell wall budget.
    per_trial = est * reps
    trials = int(budget_s / per_trial) if per_trial > 0 else max_trials
    trials = max(min_trials, min(max_trials, trials))

    def _null(*_a):
        return None

    samples_ms: list = []
    for _ in range(trials):
        # Pre-build `reps` fresh inputs OUTSIDE the timed region.
        inputs = [wl.make_input(n) for _ in range(reps)]
        gc.collect()
        gc.disable()
        # Null-baseline (loop + call-dispatch overhead) over identical structure.
        t0 = time.perf_counter_ns()
        for a in inputs:
            _null(*a)
        null_dt = time.perf_counter_ns() - t0
        # Real workload.
        t0 = time.perf_counter_ns()
        for a in inputs:
            wl.call(*a)
        work_dt = time.perf_counter_ns() - t0
        gc.enable()
        net_ms = max(0.0, (work_dt - null_dt) / reps) / 1e6
        samples_ms.append(net_ms)

    clean, removed = zscore_filter(samples_ms, z_threshold)
    return clean, trials, removed


def measure_mem_cell(wl: Workload, n: int, mem_trials: int = 3) -> tuple:
    """
    Peak heap (bytes) of one call, INCLUDING the input data structure (working
    set / space complexity). tracemalloc excludes the interpreter baseline by
    construction. Returns (mean_bytes, std_bytes).
    """
    peaks: list = []
    for _ in range(mem_trials):
        gc.collect()
        tracemalloc.start()
        tracemalloc.reset_peak()
        args = wl.make_input(n)      # counted: the data structure itself
        wl.call(*args)               # counted: algorithm's own allocations
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peaks.append(float(peak))
    mean = statistics.mean(peaks)
    std = statistics.stdev(peaks) if len(peaks) > 1 else 0.0
    return mean, std


def run_cell(
    wl: Workload, n: int, cfg: gm.EnergyModelConfig, *,
    budget_s: float, max_trials: int, min_trials: int, z_threshold: float,
) -> CellResult:
    t_samples, trials_run, outliers = measure_time_cell(
        wl, n, budget_s=budget_s, max_trials=max_trials,
        min_trials=min_trials, z_threshold=z_threshold,
    )
    mem_mean, mem_std = measure_mem_cell(wl, n)

    time_mean_ms = statistics.mean(t_samples) if t_samples else 0.0
    time_std_ms = statistics.stdev(t_samples) if len(t_samples) > 1 else 0.0
    cv = (time_std_ms / time_mean_ms) if time_mean_ms > 0 else 0.0
    # 95% CI (normal approx; adequate for n>=8).
    if len(t_samples) > 1:
        se = time_std_ms / math.sqrt(len(t_samples))
        ci = (time_mean_ms - 1.96 * se, time_mean_ms + 1.96 * se)
    else:
        ci = (time_mean_ms, time_mean_ms)

    green = gm.estimate_energy(
        elapsed_s=time_mean_ms / 1000.0,
        mem_bytes=mem_mean,
        cpu_percent=100.0,        # single-threaded CPU-bound -> one saturated core
        cfg=cfg,
    )

    return CellResult(
        case=wl.case, label=wl.label, complexity=wl.complexity, n=n,
        trials_run=trials_run, outliers_removed=outliers,
        time_mean_ms=time_mean_ms, time_std_ms=time_std_ms, time_cv=cv,
        time_ci95_ms=ci, mem_mean_bytes=mem_mean, mem_std_bytes=mem_std,
        energy_j=green.energy_joules, carbon_g=green.carbon_g,
        power_w=green.power_avg_w,
    )


# ============================================================================
# §5  ORCHESTRATION + SCALING GRADES
# ============================================================================

@dataclass
class AlgoReport:
    generated_at: str
    energy_model: dict
    cells: list = field(default_factory=list)         # list[CellResult]
    scaling: dict = field(default_factory=dict)       # label -> grade dicts


def run_all(cfg: gm.EnergyModelConfig, wl_list: list, *,
            budget_s: float, max_trials: int, min_trials: int,
            z_threshold: float) -> AlgoReport:
    report = AlgoReport(
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        energy_model=cfg.__dict__.copy(),
    )
    for wl in wl_list:
        print(f"\n[{wl.case}] {wl.label}  ({wl.complexity})")
        cells_for_wl: list = []
        for n in wl.sizes:
            cell = run_cell(
                wl, n, cfg, budget_s=budget_s, max_trials=max_trials,
                min_trials=min_trials, z_threshold=z_threshold,
            )
            cells_for_wl.append(cell)
            report.cells.append(cell)
            print(f"  N={n:<7} t={cell.time_mean_ms:10.4f} ms "
                  f"(CV {cell.time_cv*100:4.1f}%, {cell.trials_run} trials) "
                  f"mem={cell.mem_mean_bytes/1024:9.1f} KB "
                  f"E={cell.energy_j:.4g} J  C={cell.carbon_g:.4g} g")

        # Scaling grade from the time curve (primary), and the memory curve.
        sizes = [c.n for c in cells_for_wl]
        t_grade = gm.grade_scaling(sizes, [c.time_mean_ms for c in cells_for_wl])
        m_grade = gm.grade_scaling(sizes, [c.mem_mean_bytes for c in cells_for_wl])
        report.scaling[wl.label] = {
            "complexity_theoretical": wl.complexity,
            "time_grade": t_grade.as_dict(),
            "mem_grade": m_grade.as_dict(),
        }
        print(f"  -> TIME grade {t_grade.grade} (k={t_grade.exponent_k:.2f}, "
              f"class {t_grade.inferred_class}, R2={t_grade.r2_powerlaw:.3f})")
    return report


# ============================================================================
# §6  MARKDOWN TABLE EXPORT
# ============================================================================

def _fmt_mem(bytes_val: float) -> str:
    mb = bytes_val / (1024 ** 2)
    if mb >= 0.1:
        return f"{mb:.3f} MB"
    return f"{bytes_val / 1024:.1f} KB"


def export_markdown(report: AlgoReport) -> str:
    out: list = []
    out.append("# Green Profiler - Case Study Benchmark Results\n")
    out.append(f"_Generated: {report.generated_at}_  ")
    out.append(f"_Grid intensity: {report.energy_model['grid_intensity_g_per_kwh']} "
               f"gCO2e/kWh - CPU/core: "
               f"{report.energy_model['cpu_package_tdp_w']}W/"
               f"{report.energy_model['physical_cores']}cores - "
               f"RAM: {report.energy_model['ram_watts_per_gb']} W/GB_\n")

    # Group cells by case, pairing the two algorithms per size.
    cases = {}
    for c in report.cells:
        cases.setdefault(c.case, []).append(c)

    case_titles = {
        "sort": "Case Study 1 - Bubble Sort vs. Quick Sort",
        "search": "Case Study 2 - Linear Search vs. Binary Search",
        "fib": "Case Study 3 - Naive Recursive vs. Dynamic-Programming Fibonacci",
    }

    for case, title in case_titles.items():
        cells = [c for c in report.cells if c.case == case]
        if not cells:
            continue
        out.append(f"\n## {title}\n")
        out.append("| Algorithm | Input N | Time (ms) | ±SD | CV% | Peak RAM | "
                   "Energy (J) | Carbon (gCO2e) | Trials |")
        out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for c in cells:
            out.append(
                f"| {c.label} | {c.n:,} | {c.time_mean_ms:.4f} | "
                f"{c.time_std_ms:.4f} | {c.time_cv*100:.1f} | {_fmt_mem(c.mem_mean_bytes)} | "
                f"{c.energy_j:.4g} | {c.carbon_g:.4g} | {c.trials_run} |"
            )

        # Per-size % reduction (slow vs fast algorithm) where both share an N.
        labels = list(dict.fromkeys(c.label for c in cells))
        if len(labels) >= 2:
            slow, fast = labels[0], labels[1]
            shared = sorted(set(c.n for c in cells if c.label == slow)
                            & set(c.n for c in cells if c.label == fast))
            if shared:
                out.append(f"\n**Resource reduction ({fast} vs {slow}):**\n")
                out.append("| Input N | Time reduction | Energy reduction |")
                out.append("|---:|---:|---:|")
                for n in shared:
                    cs = next(c for c in cells if c.label == slow and c.n == n)
                    cf = next(c for c in cells if c.label == fast and c.n == n)
                    tr = gm.pct_reduction(cs.time_mean_ms, cf.time_mean_ms)
                    er = gm.pct_reduction(cs.energy_j, cf.energy_j)
                    out.append(f"| {n:,} | {tr:.2f}% | {er:.2f}% |")

    # Scaling-grade summary.
    out.append("\n## Sustainability Grades (complexity-scaling exponent)\n")
    out.append("| Algorithm | Theoretical | Fitted k | Inferred class | "
               "Time Grade | Fit R² |")
    out.append("|---|---|---:|---|:--:|---:|")
    for label, g in report.scaling.items():
        tg = g["time_grade"]
        out.append(
            f"| {label} | {g['complexity_theoretical']} | {tg['exponent_k']:.2f} | "
            f"{tg['inferred_class']} | **{tg['grade']}** | {tg['r2_powerlaw']:.3f} |"
        )
    return "\n".join(out) + "\n"


# ============================================================================
# §7  CLI
# ============================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Green Profiler case-study benchmark harness")
    p.add_argument("--trials", type=int, default=30, help="max trials per cell")
    p.add_argument("--min-trials", type=int, default=8)
    p.add_argument("--budget", type=float, default=20.0, help="per-cell wall-time budget (s)")
    p.add_argument("--zthr", type=float, default=2.5)
    p.add_argument("--cores", type=int, default=8, help="physical cores for energy model")
    p.add_argument("--tdp", type=float, default=28.0, help="CPU package TDP (W)")
    p.add_argument("--grid", type=float, default=475.0, help="grid intensity gCO2e/kWh")
    p.add_argument("--out", type=str, default="results/case_studies.json")
    p.add_argument("--md", type=str, default="results/case_studies_tables.md")
    p.add_argument("--quick", action="store_true", help="smaller sizes for a fast smoke run")
    return p


def main() -> None:
    # Windows consoles default to a legacy code page (cp1252) that cannot
    # encode non-ASCII characters that may appear in the resolved output
    # path (e.g. a Thai directory name). Force UTF-8 so the final status
    # prints never abort an otherwise-successful run.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = _build_parser().parse_args()

    cfg = gm.EnergyModelConfig(
        cpu_package_tdp_w=args.tdp, physical_cores=args.cores,
        grid_intensity_g_per_kwh=args.grid,
    )

    if args.quick:
        wl = build_workloads(sort_sizes=[100, 500, 1000],
                             search_sizes=[100, 1000, 10000],
                             fib_sizes=[18, 22, 26, 30],
                             dp_extra_sizes=[1000, 10000])
    else:
        wl = build_workloads(sort_sizes=[100, 1000, 10000],
                             search_sizes=[100, 1000, 10000],
                             fib_sizes=[20, 25, 30, 35],
                             dp_extra_sizes=[1000, 10000, 100000])

    print("=" * 72)
    print("  GREEN PROFILER - CASE STUDY BENCHMARK SUITE")
    print(f"  trials<={args.trials}  budget={args.budget}s/cell  z={args.zthr}  "
          f"grid={args.grid} gCO2e/kWh")
    print("=" * 72)

    report = run_all(cfg, wl, budget_s=args.budget, max_trials=args.trials,
                     min_trials=args.min_trials, z_threshold=args.zthr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": report.generated_at,
        "energy_model": report.energy_model,
        "cells": [c.as_dict() for c in report.cells],
        "scaling": report.scaling,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    md_path = Path(args.md)
    md_path.write_text(export_markdown(report), encoding="utf-8")

    print(f"\n[OK] JSON -> {out_path.resolve()}")
    print(f"[OK] Markdown tables -> {md_path.resolve()}")


if __name__ == "__main__":
    main()

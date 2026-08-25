"""
================================================================================
AI-PerfBench v3.0 — High-Precision Cross-Platform Isolated Process Profiler
================================================================================
Green AI & Sustainable Metrics Research Tool

Architecture: Zero-intrusion Isolated Process Monitoring
  The target process is spawned as a child. The profiler observes it from the
  outside via psutil kernel APIs — no source code instrumentation required.

Cross-platform: Windows 10/11 · macOS (Apple Silicon M-series & Intel) · Linux
                (Ubuntu · Raspberry Pi 5 / ARM)

Python: 3.9+
Deps (required): psutil >= 5.9
Deps (optional): numpy >= 1.24, scipy >= 1.10  → enables t-distribution CI;
                 falls back to normal approximation when absent.

CLI Quick-start:
    python perf_bench.py -- python heavy_math.py
    python perf_bench.py --trials 7 --output results/run1.json -- python model.py
    python perf_bench.py --cmd "python model.py --epochs 5" --trials 7

Library Quick-start:
    from perf_bench import PerfBench, ProfilerConfig
    cfg = ProfilerConfig(cmd_list=["python", "model.py"], trials=7)
    report = PerfBench(cfg).run()

Accuracy integration (Green AI Optimization Matrix):
    Have your workload print:   PERFBENCH_ACCURACY=95.2
    The profiler captures it and includes it in results.json automatically.
================================================================================
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shlex
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Optional numerical deps (graceful degradation) ───────────────────────────

try:
    import numpy as np
    import scipy.stats as _scipy_stats
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

try:
    import psutil
except ImportError:
    sys.exit("[FATAL] psutil is required:  pip install psutil")

# Force UTF-8 output on Windows (avoids cp1252 UnicodeEncodeError for box-drawing chars)
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Platform flags ─────────────────────────────────────────────────────────

IS_WINDOWS = sys.platform == "win32"
IS_MACOS   = sys.platform == "darwin"
IS_LINUX   = sys.platform.startswith("linux")


# ============================================================================
# §1  CONFIGURATION
# ============================================================================

@dataclass
class ProfilerConfig:
    """
    Single source of truth for all tunable parameters.
    Pass an instance to PerfBench, or let the CLI populate it.
    """

    # ── Target ─────────────────────────────────────────────────────────────
    cmd_list: list = field(default_factory=lambda: ["python", "heavy_math.py"])
    trials: int = 7

    # ── Sampling ────────────────────────────────────────────────────────────
    sample_interval_s: float = 0.001       # 1 ms base polling interval
    spinlock_threshold_s: float = 0.0002   # intervals below this → busy-wait
                                           # (avoids Windows 15 ms sleep floor)

    # ── Adaptive precision loop ─────────────────────────────────────────────
    # When RSS jumps by spike_rss_delta_bytes between consecutive samples, the
    # loop temporarily oversamples at (base_interval / spike_boost_factor).
    spike_rss_delta_bytes: int = 2 * 1024 * 1024   # 2 MB → trigger boost
    spike_boost_factor: float = 10.0               # 10× faster during spike
    spike_cooldown_samples: int = 30               # samples before reverting

    # ── Baseline ────────────────────────────────────────────────────────────
    # Sampled in the 50 ms window immediately before child spawn.
    baseline_samples: int = 30
    baseline_window_s: float = 0.05

    # ── Statistics ──────────────────────────────────────────────────────────
    warmup_drop_trials: int = 1            # first N trials = cold-start (isolated)
    outlier_z_threshold: float = 2.5      # z-score threshold for sample removal

    # ── Accuracy (Green AI) ─────────────────────────────────────────────────
    # The child script should print  PERFBENCH_ACCURACY=95.2  to stdout.
    # Override with a custom regex (group 1 must capture the float value).
    accuracy_pattern: str = r"PERFBENCH_ACCURACY\s*=\s*([0-9.]+)"

    # ── Output ──────────────────────────────────────────────────────────────
    output_path: Optional[Path] = None    # None → console summary only
    verbose: bool = False


# ============================================================================
# §2  DATA STRUCTURES
# ============================================================================

@dataclass
class SamplePoint:
    t_rel_s: float           # seconds since child process started
    rss_bytes: int           # Resident Set Size — physical RAM only
    vms_bytes: int           # Virtual Memory Size (mapped, not necessarily resident)
    cpu_percent: float       # instantaneous CPU % from psutil (interval=None)
    actual_interval_s: float # real elapsed time since last sample (shows adaptive bursts)


@dataclass
class AccuracyMetrics:
    """Accuracy parsed from child stdout. Enables the Green AI Optimization Matrix."""
    accuracy_percent: Optional[float] = None
    raw_match: Optional[str] = None


@dataclass
class TrialResult:
    trial_index: int
    is_cold_start: bool
    wall_time_s: float
    exit_code: int
    stdout: str
    stderr: str
    samples: list = field(default_factory=list)        # list[SamplePoint]
    accuracy: AccuracyMetrics = field(default_factory=AccuracyMetrics)

    # ── Derived metrics — populated by _analyse_trial() ──────────────────
    rss_baseline_bytes: int = 0        # child RSS at first sample (boot footprint)
    rss_peak_net_bytes: int = 0        # max(RSS[i] - baseline)
    rss_mean_net_bytes: float = 0.0
    rss_p95_net_bytes: float = 0.0
    rss_std_bytes: float = 0.0
    cpu_mean_percent: float = 0.0
    cpu_peak_percent: float = 0.0
    sample_count: int = 0
    outlier_sample_count: int = 0
    adaptive_boost_count: int = 0      # times the loop boosted sampling frequency


@dataclass
class SystemBaseline:
    """
    OS-level snapshot captured in the 50 ms window before child spawn.
    Provides the true ambient RAM state for delta subtraction.
    """
    profiler_rss_bytes: int            # this profiler process's RSS
    system_available_bytes: int        # psutil.virtual_memory().available
    system_used_bytes: int             # psutil.virtual_memory().used
    sampled_at_perf: float             # time.perf_counter() at capture moment


@dataclass
class BenchmarkReport:
    title: str = "AI-PerfBench v3.0 Report"
    generated_at: str = ""
    config: dict = field(default_factory=dict)
    system_info: dict = field(default_factory=dict)
    pre_launch_baseline: dict = field(default_factory=dict)

    cold_start_trial: Optional[TrialResult] = None
    steady_state_trials: list = field(default_factory=list)  # list[TrialResult]

    # ── Cross-trial steady-state aggregate ───────────────────────────────
    ss_wall_time_mean_s: float = 0.0
    ss_wall_time_std_s: float = 0.0
    ss_wall_time_cv: float = 0.0
    ss_wall_time_ci95_low: float = 0.0
    ss_wall_time_ci95_high: float = 0.0
    ss_rss_peak_mean_bytes: float = 0.0
    ss_rss_peak_std_bytes: float = 0.0
    ss_rss_ci95_low: float = 0.0
    ss_rss_ci95_high: float = 0.0
    ss_cpu_mean_percent: float = 0.0

    # ── Green AI Optimization Matrix ──────────────────────────────────────
    ss_accuracy_mean: Optional[float] = None
    ss_accuracy_std: Optional[float] = None

    calibration_note: str = ""


# ============================================================================
# §3  PRE-LAUNCH BASELINE SAMPLER
# ============================================================================

class BaselineSampler:
    """
    Captures ambient OS memory state in a tight window before child spawn.

    Rationale: sampling system available RAM right before launch (not minutes
    earlier) gives an accurate zero-point. Background OS fluctuations within
    the 50 ms window are averaged out over baseline_samples readings.
    """

    def __init__(self, cfg: ProfilerConfig) -> None:
        self._cfg = cfg
        self._self_proc = psutil.Process(os.getpid())

    def sample(self) -> SystemBaseline:
        interval = self._cfg.baseline_window_s / max(self._cfg.baseline_samples, 1)
        profiler_rss: list[int] = []
        avail_ram: list[int] = []
        used_ram: list[int] = []

        for _ in range(self._cfg.baseline_samples):
            try:
                profiler_rss.append(self._self_proc.memory_info().rss)
            except psutil.NoSuchProcess:
                pass
            vm = psutil.virtual_memory()
            avail_ram.append(vm.available)
            used_ram.append(vm.used)
            time.sleep(interval)

        def _mean(lst: list[int]) -> int:
            return int(statistics.mean(lst)) if lst else 0

        return SystemBaseline(
            profiler_rss_bytes=_mean(profiler_rss),
            system_available_bytes=_mean(avail_ram),
            system_used_bytes=_mean(used_ram),
            sampled_at_perf=time.perf_counter(),
        )


# ============================================================================
# §4  ADAPTIVE PROCESS MONITOR  —  the hot sampling loop
# ============================================================================

class AdaptiveProcessMonitor:
    """
    Spawns the target command and polls its kernel metrics at adaptive frequency.

    Sampling strategy:
        Base state   : sleep cfg.sample_interval_s between reads
        Spike state  : when RSS changes by >= spike_rss_delta_bytes, divide
                       the interval by spike_boost_factor for spike_cooldown_samples
                       consecutive reads — catching transient peaks that a 1-second
                       profiler (Task Manager, top) would miss entirely
        Sub-ms mode  : intervals below spinlock_threshold_s use a busy-wait loop
                       instead of time.sleep() to avoid the Windows scheduler floor
                       (~15.6 ms per OS timer tick)

    Command execution:
        Uses subprocess.Popen with a structured list — never shell=True.
        This is cross-platform safe: no CMD vs bash interpretation differences.
    """

    def __init__(
        self,
        cfg: ProfilerConfig,
        trial_index: int,
        baseline: SystemBaseline,
    ) -> None:
        self._cfg = cfg
        self._trial_index = trial_index
        self._baseline = baseline
        self._samples: list[SamplePoint] = []
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._child: Optional[subprocess.Popen] = None
        self._child_ps: Optional[psutil.Process] = None
        self._t_origin = 0.0
        self._boost_count = 0

    def run(self) -> TrialResult:
        cmd = self._cfg.cmd_list
        executable = cmd[0]
        if not shutil.which(executable):
            raise FileNotFoundError(
                f"Executable not found on PATH: {executable!r}\n"
                f"Full command: {cmd}"
            )

        t_wall_start = time.monotonic()
        self._t_origin = time.perf_counter()

        # No shell=True — cmd is a clean list, safe on all platforms.
        # CREATE_NO_WINDOW suppresses a console flash on Windows.
        popen_kwargs: dict = {}
        if IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        self._child = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )

        try:
            self._child_ps = psutil.Process(self._child.pid)
            # First cpu_percent call always returns 0.0 — prime it now
            self._child_ps.cpu_percent(interval=None)
        except psutil.NoSuchProcess:
            self._child_ps = None  # workload finished before we attached (rare)

        # Start background sampler thread
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._adaptive_loop,
            daemon=True,
            name=f"perfbench-{self._trial_index}",
        )
        self._thread.start()

        stdout_b, stderr_b = self._child.communicate()
        wall_time_s = time.monotonic() - t_wall_start

        self._stop_event.set()
        self._thread.join(timeout=2.0)

        stdout_str = stdout_b.decode("utf-8", errors="replace")
        stderr_str = stderr_b.decode("utf-8", errors="replace")

        result = TrialResult(
            trial_index=self._trial_index,
            is_cold_start=(self._trial_index < self._cfg.warmup_drop_trials),
            wall_time_s=wall_time_s,
            exit_code=self._child.returncode,
            stdout=stdout_str,
            stderr=stderr_str,
            samples=self._samples[:],
            accuracy=_parse_accuracy(stdout_str, self._cfg.accuracy_pattern),
            adaptive_boost_count=self._boost_count,
        )

        _analyse_trial(result, self._cfg)
        return result

    # ── Hot loop ──────────────────────────────────────────────────────────

    def _adaptive_loop(self) -> None:
        base = self._cfg.sample_interval_s
        current = base
        cooldown = 0
        prev_rss: Optional[int] = None
        prev_t = time.perf_counter()

        while not self._stop_event.is_set():
            t_now = time.perf_counter()
            t_rel = t_now - self._t_origin
            actual_dt = t_now - prev_t
            prev_t = t_now

            sp = self._read_sample(t_rel, actual_dt)
            if sp is not None:
                self._samples.append(sp)

                # ── Spike detection ──────────────────────────────────────
                if prev_rss is not None:
                    delta = abs(sp.rss_bytes - prev_rss)
                    if delta >= self._cfg.spike_rss_delta_bytes:
                        current = base / self._cfg.spike_boost_factor
                        cooldown = self._cfg.spike_cooldown_samples
                        self._boost_count += 1
                    elif cooldown > 0:
                        cooldown -= 1
                        if cooldown == 0:
                            current = base
                prev_rss = sp.rss_bytes

            # ── Precision sleep ───────────────────────────────────────────
            elapsed = time.perf_counter() - t_now
            remaining = current - elapsed
            if remaining > 0:
                _precision_sleep(remaining, self._cfg.spinlock_threshold_s)

    def _read_sample(self, t_rel: float, actual_dt: float) -> Optional[SamplePoint]:
        """
        Sample the WHOLE PROCESS TREE (target + all descendants), not just the
        direct child.

        Why this matters: many real targets do their work in a subprocess rather
        than in the process we spawned. A virtualenv's Scripts/python.exe on
        Windows is a ~4 MB redirector that re-execs the real interpreter as a
        child; shell wrappers, `npm`/`uv` launchers, ML dataloader workers and
        anything using multiprocessing behave the same way. Measuring only the
        direct child under-reports such workloads by orders of magnitude
        (measured here: 4.0 MB reported vs 414.8 MB actually resident).

        RSS/VMS are SUMMED across the tree and CPU% likewise, which is the
        correct accounting for "what did this command cost the machine".
        Descendants that exit between enumeration and reading are skipped.
        """
        if self._child_ps is None:
            return None
        try:
            mem = self._child_ps.memory_info()
            rss = mem.rss
            vms = mem.vms
            cpu = self._child_ps.cpu_percent(interval=None)

            try:
                descendants = self._child_ps.children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                descendants = []

            for child in descendants:
                try:
                    cmem = child.memory_info()
                    rss += cmem.rss
                    vms += cmem.vms
                    cpu += child.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied,
                        psutil.ZombieProcess):
                    continue        # died mid-enumeration; nothing to add

            return SamplePoint(
                t_rel_s=t_rel,
                rss_bytes=rss,
                vms_bytes=vms,
                cpu_percent=cpu,
                actual_interval_s=actual_dt,
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None


# ============================================================================
# §5  PLATFORM UTILITIES
# ============================================================================

def _precision_sleep(duration_s: float, spinlock_threshold_s: float) -> None:
    """
    Hybrid sleep: OS scheduler for intervals > threshold, busy-wait below it.

    Windows scheduler granularity is ~15.6 ms. Any target interval below that
    will overshoot badly with time.sleep(). Busy-waiting burns one CPU thread
    but gives true sub-ms accuracy — acceptable for short spike bursts.
    """
    if duration_s <= 0:
        return
    if duration_s < spinlock_threshold_s:
        deadline = time.perf_counter() + duration_s
        while time.perf_counter() < deadline:
            pass
    else:
        time.sleep(duration_s)


def parse_cmd_string(cmd_str: str) -> list[str]:
    """
    Split a command string into a token list, platform-safely.

    posix=True  on Mac/Linux: handles single-quoted strings and backslash escapes
    posix=False on Windows:   preserves backslash path separators (C:\\...),
                              then strips residual surrounding quotes
    """
    if IS_WINDOWS:
        tokens = shlex.split(cmd_str, posix=False)
        return [t.strip('"').strip("'") for t in tokens]
    return shlex.split(cmd_str, posix=True)


# ============================================================================
# §6  ACCURACY PARSER
# ============================================================================

def _parse_accuracy(stdout: str, pattern: str) -> AccuracyMetrics:
    """
    Search child stdout for the accuracy marker (default: PERFBENCH_ACCURACY=95.2).

    Add this one-liner to your workload script to enable Green AI tracking:
        print(f"PERFBENCH_ACCURACY={val_accuracy * 100:.2f}")

    Use --accuracy-pattern to override for frameworks that emit their own format,
    e.g.  r"val_acc[:=\\s]+([0-9.]+)"  for Keras-style logs.
    """
    try:
        match = re.search(pattern, stdout, re.IGNORECASE | re.MULTILINE)
        if match:
            return AccuracyMetrics(
                accuracy_percent=float(match.group(1)),
                raw_match=match.group(0),
            )
    except (re.error, ValueError, IndexError):
        pass
    return AccuracyMetrics()


# ============================================================================
# §7  STATISTICAL ENGINE
# ============================================================================

def _zscore_filter(
    values: list[float], threshold: float
) -> tuple[list[float], int]:
    """Remove samples whose z-score exceeds threshold. Returns (clean, n_removed)."""
    if len(values) < 3:
        return values, 0
    mean = statistics.mean(values)
    try:
        std = statistics.stdev(values)
    except statistics.StatisticsError:
        return values, 0
    if std == 0.0:
        return values, 0
    clean = [v for v in values if abs((v - mean) / std) <= threshold]
    return clean, len(values) - len(clean)


def _percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    if _HAS_SCIPY:
        return float(np.percentile(data, p))
    s = sorted(data)
    n = len(s)
    rank = (p / 100.0) * (n - 1)
    lo, hi = int(rank), min(int(rank) + 1, n - 1)
    return s[lo] + (rank - lo) * (s[hi] - s[lo])


def _confidence_interval(
    data: list[float], confidence: float = 0.95
) -> tuple[float, float]:
    """
    Two-sided confidence interval.
    scipy t-distribution when available; normal approximation (z=1.96) as fallback.
    Normal approximation is adequate for n >= 5, which is typical for benchmarks.
    """
    n = len(data)
    if n < 2:
        v = data[0] if data else 0.0
        return v, v
    mean = statistics.mean(data)
    se = statistics.stdev(data) / math.sqrt(n)
    if _HAS_SCIPY:
        lo, hi = _scipy_stats.t.interval(confidence, df=n - 1, loc=mean, scale=se)
        return float(lo), float(hi)
    return mean - 1.96 * se, mean + 1.96 * se


def _analyse_trial(trial: TrialResult, cfg: ProfilerConfig) -> None:
    """
    Compute derived metrics for a single trial.

    Delta subtraction logic:
        baseline_rss = first captured RSS sample (child process boot footprint)
        net_rss[i]   = max(0, sample[i].rss - baseline_rss)

    This removes the Python interpreter + stdlib footprint, isolating only the
    incremental RAM consumed by the workload's data structures and allocations.
    """
    trial.sample_count = len(trial.samples)
    if not trial.samples:
        return

    baseline = trial.samples[0].rss_bytes
    trial.rss_baseline_bytes = baseline

    rss_net = [max(0, s.rss_bytes - baseline) for s in trial.samples]
    cpu_vals = [s.cpu_percent for s in trial.samples]

    rss_clean, n_out = _zscore_filter(
        [float(v) for v in rss_net], cfg.outlier_z_threshold
    )
    trial.outlier_sample_count = n_out
    if not rss_clean:
        return

    trial.rss_peak_net_bytes = int(max(rss_clean))
    trial.rss_mean_net_bytes = statistics.mean(rss_clean)
    trial.rss_std_bytes = statistics.stdev(rss_clean) if len(rss_clean) > 1 else 0.0
    trial.rss_p95_net_bytes = _percentile(rss_clean, 95)
    trial.cpu_mean_percent = statistics.mean(cpu_vals) if cpu_vals else 0.0
    trial.cpu_peak_percent = max(cpu_vals) if cpu_vals else 0.0


def _aggregate_steady_state(report: BenchmarkReport) -> None:
    """Compute cross-trial statistics and the calibration verdict."""
    ss = report.steady_state_trials
    if not ss:
        return

    wall_times = [t.wall_time_s for t in ss]
    rss_peaks  = [float(t.rss_peak_net_bytes) for t in ss]
    cpu_means  = [t.cpu_mean_percent for t in ss]
    accuracies = [
        t.accuracy.accuracy_percent for t in ss
        if t.accuracy.accuracy_percent is not None
    ]

    n = len(wall_times)
    report.ss_wall_time_mean_s    = statistics.mean(wall_times)
    report.ss_wall_time_std_s     = statistics.stdev(wall_times) if n > 1 else 0.0
    report.ss_wall_time_cv        = (
        report.ss_wall_time_std_s / report.ss_wall_time_mean_s
        if report.ss_wall_time_mean_s else 0.0
    )
    report.ss_rss_peak_mean_bytes = statistics.mean(rss_peaks)
    report.ss_rss_peak_std_bytes  = statistics.stdev(rss_peaks) if n > 1 else 0.0
    report.ss_cpu_mean_percent    = statistics.mean(cpu_means)

    report.ss_wall_time_ci95_low, report.ss_wall_time_ci95_high = (
        _confidence_interval(wall_times)
    )
    report.ss_rss_ci95_low, report.ss_rss_ci95_high = (
        _confidence_interval(rss_peaks)
    )

    if accuracies:
        report.ss_accuracy_mean = statistics.mean(accuracies)
        report.ss_accuracy_std  = (
            statistics.stdev(accuracies) if len(accuracies) > 1 else 0.0
        )

    cv = report.ss_wall_time_cv
    if cv < 0.02:
        report.calibration_note = (
            f"STABLE — CV = {cv:.2%}. Results are highly repeatable. Target met (CV < 2 %)."
        )
    elif cv < 0.10:
        report.calibration_note = (
            f"ACCEPTABLE — CV = {cv:.2%}. "
            "Consider more trials or closing competing processes for tighter bounds."
        )
    else:
        report.calibration_note = (
            f"HIGH VARIANCE — CV = {cv:.2%}. "
            "Background interference likely. Increase trials or isolate the environment."
        )


# ============================================================================
# §8  SYSTEM INFO  (fully cross-platform)
# ============================================================================

def collect_system_info() -> dict:
    """
    Collect host metadata. Works on all target platforms:
      - socket.gethostname() replaces os.uname() (Windows-incompatible)
      - platform.machine() reports 'arm64' for Apple M-series, 'AMD64' on Windows x86
      - /proc/device-tree/model is probed for Raspberry Pi identification
    """
    vm = psutil.virtual_memory()
    cpu_freq = psutil.cpu_freq()
    cpu_arch = platform.machine()

    is_apple_silicon = IS_MACOS and cpu_arch in ("arm64", "arm")

    is_rpi = False
    if IS_LINUX and ("arm" in cpu_arch.lower() or "aarch" in cpu_arch.lower()):
        try:
            model = Path("/proc/device-tree/model").read_text(errors="ignore")
            is_rpi = "raspberry pi" in model.lower()
        except OSError:
            pass

    return {
        "hostname":           socket.gethostname(),
        "platform":           sys.platform,
        "os_detail":          f"{platform.system()} {platform.release()}",
        "cpu_arch":           cpu_arch,
        "is_apple_silicon":   is_apple_silicon,
        "is_raspberry_pi":    is_rpi,
        "python_version":     sys.version.split()[0],
        "cpu_logical_cores":  psutil.cpu_count(logical=True),
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "cpu_freq_mhz":       round(cpu_freq.current, 1) if cpu_freq else None,
        "total_ram_gb":       round(vm.total / 1024 ** 3, 3),
        "available_ram_gb":   round(vm.available / 1024 ** 3, 3),
        "scipy_available":    _HAS_SCIPY,
    }


# ============================================================================
# §9  REPORTER  —  console output + JSON serialisation
# ============================================================================

def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


def _fmt_time(s: float) -> str:
    if s < 0.001:
        return f"{s * 1e6:.1f} µs"
    if s < 1.0:
        return f"{s * 1000:.2f} ms"
    return f"{s:.4f} s"


def _print_report(report: BenchmarkReport) -> None:
    SEP    = "─" * 72
    DOUBLE = "═" * 72

    def row(label: str, value: str, w: int = 36) -> None:
        print(f"  {label:<{w}} {value}")

    print(f"\n{DOUBLE}")
    print(f"  {report.title}")
    print(f"  Generated  : {report.generated_at}")
    print(f"  Command    : {' '.join(report.config.get('cmd_list', []))}")
    print(DOUBLE)

    # ── System info ───────────────────────────────────────────────────────
    si = report.system_info
    print(f"\n  SYSTEM INFORMATION")
    print(SEP)
    row("Hostname",        si.get("hostname", "?"))
    row("OS",              si.get("os_detail", "?"))
    row("CPU Architecture", si.get("cpu_arch", "?"))
    if si.get("is_apple_silicon"):
        row("Silicon",    "Apple M-series (ARM64) ✓")
    if si.get("is_raspberry_pi"):
        row("Board",      "Raspberry Pi ✓")
    row("CPU Cores",
        f"{si.get('cpu_logical_cores')} logical / {si.get('cpu_physical_cores')} physical")
    freq = si.get("cpu_freq_mhz")
    row("CPU Frequency",   f"{freq} MHz" if freq else "N/A")
    row("RAM Total",       _fmt_bytes(si.get("total_ram_gb", 0) * 1024 ** 3))
    row("RAM Available",   _fmt_bytes(si.get("available_ram_gb", 0) * 1024 ** 3))
    row("SciPy / NumPy",   "available" if si.get("scipy_available") else "not installed (normal-approx CI)")

    # ── Cold-start trial ──────────────────────────────────────────────────
    if report.cold_start_trial:
        cs = report.cold_start_trial
        print(f"\n  COLD-START TRIAL  (Trial #{cs.trial_index + 1})")
        print(SEP)
        row("Wall time",      _fmt_time(cs.wall_time_s))
        row("Peak RSS (net)", _fmt_bytes(cs.rss_peak_net_bytes))
        row("P95  RSS (net)", _fmt_bytes(cs.rss_p95_net_bytes))
        row("Mean RSS (net)", _fmt_bytes(cs.rss_mean_net_bytes))
        row("CPU (mean)",     f"{cs.cpu_mean_percent:.1f} %")
        row("Samples",        f"{cs.sample_count}  (outliers removed: {cs.outlier_sample_count})")
        row("Adaptive boosts", f"{cs.adaptive_boost_count}  (spike oversampling events)")
        row("Exit code",      str(cs.exit_code))
        if cs.accuracy.accuracy_percent is not None:
            row("Accuracy",   f"{cs.accuracy.accuracy_percent:.2f} %")

    # ── Steady-state per-trial table ──────────────────────────────────────
    print(f"\n  STEADY-STATE TRIALS")
    print(SEP)
    print(f"  {'#':<5} {'Wall time':>11} {'Peak RSS':>12} {'Mean RSS':>12} "
          f"{'CPU%':>7} {'Acc%':>7} {'Boosts':>7} {'Exit':>5}")
    print(f"  {'─'*5} {'─'*11} {'─'*12} {'─'*12} {'─'*7} {'─'*7} {'─'*7} {'─'*5}")

    for t in report.steady_state_trials:
        acc = (
            f"{t.accuracy.accuracy_percent:.1f}"
            if t.accuracy.accuracy_percent is not None else "  N/A"
        )
        print(
            f"  {t.trial_index + 1:<5} "
            f"{_fmt_time(t.wall_time_s):>11} "
            f"{_fmt_bytes(t.rss_peak_net_bytes):>12} "
            f"{_fmt_bytes(t.rss_mean_net_bytes):>12} "
            f"{t.cpu_mean_percent:>6.1f}% "
            f"{acc:>7} "
            f"{t.adaptive_boost_count:>7} "
            f"{t.exit_code:>5}"
        )

    # ── Aggregate statistics ──────────────────────────────────────────────
    n_ss = len(report.steady_state_trials)
    print(f"\n  STEADY-STATE AGGREGATE  (N = {n_ss} trials)")
    print(SEP)
    row("Wall time — mean",   _fmt_time(report.ss_wall_time_mean_s))
    row("Wall time — std",    _fmt_time(report.ss_wall_time_std_s))
    row("Wall time — CV",     f"{report.ss_wall_time_cv:.3%}")
    row("Wall time — 95% CI",
        f"[{_fmt_time(report.ss_wall_time_ci95_low)} – {_fmt_time(report.ss_wall_time_ci95_high)}]")
    row("Peak RSS — mean",    _fmt_bytes(report.ss_rss_peak_mean_bytes))
    row("Peak RSS — std",     _fmt_bytes(report.ss_rss_peak_std_bytes))
    row("Peak RSS — 95% CI",
        f"[{_fmt_bytes(report.ss_rss_ci95_low)} – {_fmt_bytes(report.ss_rss_ci95_high)}]")
    row("CPU — mean",         f"{report.ss_cpu_mean_percent:.1f} %")

    # ── Green AI Optimization Matrix ──────────────────────────────────────
    if report.ss_accuracy_mean is not None:
        print(f"\n  GREEN AI OPTIMIZATION MATRIX")
        print(SEP)
        row("Accuracy — mean",     f"{report.ss_accuracy_mean:.2f} %")
        if report.ss_accuracy_std is not None:
            row("Accuracy — std",  f"{report.ss_accuracy_std:.2f} %")
        if report.ss_wall_time_mean_s > 0:
            eff = report.ss_accuracy_mean / report.ss_wall_time_mean_s
            row("Efficiency index (acc% / s)", f"{eff:.3f}  (higher = better)")
        if report.ss_rss_peak_mean_bytes > 0:
            ram_eff = report.ss_accuracy_mean / (report.ss_rss_peak_mean_bytes / 1024 ** 2)
            row("RAM efficiency (acc% / MB)", f"{ram_eff:.3f}  (higher = better)")

    print(f"\n  CALIBRATION: {report.calibration_note}")
    print(f"{DOUBLE}\n")


def report_to_dict(report: BenchmarkReport) -> dict:
    """Convert BenchmarkReport to a JSON-serialisable dict."""

    def trial_dict(t: TrialResult) -> dict:
        return {
            "trial_index":          t.trial_index,
            "is_cold_start":        t.is_cold_start,
            "wall_time_s":          t.wall_time_s,
            "exit_code":            t.exit_code,
            "sample_count":         t.sample_count,
            "outlier_sample_count": t.outlier_sample_count,
            "adaptive_boost_count": t.adaptive_boost_count,
            "rss_baseline_bytes":   t.rss_baseline_bytes,
            "rss_peak_net_bytes":   t.rss_peak_net_bytes,
            "rss_mean_net_bytes":   t.rss_mean_net_bytes,
            "rss_p95_net_bytes":    t.rss_p95_net_bytes,
            "rss_std_bytes":        t.rss_std_bytes,
            "cpu_mean_percent":     t.cpu_mean_percent,
            "cpu_peak_percent":     t.cpu_peak_percent,
            "accuracy_percent":     t.accuracy.accuracy_percent,
            "samples_timeline": {
                "count":     t.sample_count,
                "first_t_s": t.samples[0].t_rel_s if t.samples else None,
                "last_t_s":  t.samples[-1].t_rel_s if t.samples else None,
            },
        }

    return {
        "title":               report.title,
        "generated_at":        report.generated_at,
        "config":              report.config,
        "system_info":         report.system_info,
        "pre_launch_baseline": report.pre_launch_baseline,
        "cold_start_trial":    trial_dict(report.cold_start_trial) if report.cold_start_trial else None,
        "steady_state_trials": [trial_dict(t) for t in report.steady_state_trials],
        "aggregate": {
            "ss_wall_time_mean_s":    report.ss_wall_time_mean_s,
            "ss_wall_time_std_s":     report.ss_wall_time_std_s,
            "ss_wall_time_cv":        report.ss_wall_time_cv,
            "ss_wall_time_ci95_low":  report.ss_wall_time_ci95_low,
            "ss_wall_time_ci95_high": report.ss_wall_time_ci95_high,
            "ss_rss_peak_mean_bytes": report.ss_rss_peak_mean_bytes,
            "ss_rss_peak_std_bytes":  report.ss_rss_peak_std_bytes,
            "ss_rss_ci95_low":        report.ss_rss_ci95_low,
            "ss_rss_ci95_high":       report.ss_rss_ci95_high,
            "ss_cpu_mean_percent":    report.ss_cpu_mean_percent,
            "ss_accuracy_mean":       report.ss_accuracy_mean,
            "ss_accuracy_std":        report.ss_accuracy_std,
            "calibration_note":       report.calibration_note,
        },
    }


def write_json_report(report: BenchmarkReport, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report_to_dict(report), indent=2), encoding="utf-8")
    print(f"  [✓] JSON report → {path.resolve()}")


def _cfg_to_dict(cfg: ProfilerConfig) -> dict:
    """asdict() wrapper that converts Path → str for JSON compatibility."""
    d = asdict(cfg)
    if d.get("output_path") is not None:
        d["output_path"] = str(d["output_path"])
    return d


# ============================================================================
# §10  ORCHESTRATOR
# ============================================================================

class PerfBench:
    """
    Top-level orchestrator.

    Per-trial sequence:
        1. Sample pre-launch system baseline (50 ms tight window).
        2. AdaptiveProcessMonitor spawns child, polls metrics adaptively.
        3. Child exits → collect stdout/stderr → parse accuracy marker.
        4. _analyse_trial() computes net RSS, p95, outlier filtering.
        5. Classify as cold-start or steady-state.

    Post-loop:
        6. _aggregate_steady_state() → CV, CI, accuracy aggregate, calibration.
        7. Console report + optional JSON output.
    """

    def __init__(self, cfg: ProfilerConfig, on_trial_complete=None) -> None:
        self._cfg = cfg
        self._baseline_sampler = BaselineSampler(cfg)
        self._on_trial_complete = on_trial_complete  # (trial_num, total, TrialResult) → None

    def run(self) -> BenchmarkReport:
        cfg = self._cfg

        report = BenchmarkReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            config=_cfg_to_dict(cfg),
            system_info=collect_system_info(),
        )

        print(f"\n[AI-PerfBench v3.0]  {cfg.trials} trial(s)  |  {' '.join(cfg.cmd_list)}")
        print(f"  Sample interval  : {cfg.sample_interval_s * 1000:.2f} ms  "
              f"(spinlock below {cfg.spinlock_threshold_s * 1000:.2f} ms)")
        print(f"  Spike threshold  : {cfg.spike_rss_delta_bytes // (1024 * 1024)} MB RSS delta "
              f"→ {cfg.spike_boost_factor:.0f}× boost for {cfg.spike_cooldown_samples} samples")
        print(f"  Cold-start drop  : first {cfg.warmup_drop_trials} trial(s)\n")

        for idx in range(cfg.trials):
            label = "COLD" if idx < cfg.warmup_drop_trials else f"SS-{idx}"
            print(f"  → Trial {idx + 1:>2}/{cfg.trials}  [{label:<6}]  ", end="", flush=True)

            baseline = self._baseline_sampler.sample()
            if idx == 0:
                report.pre_launch_baseline = {
                    "profiler_rss_bytes":     baseline.profiler_rss_bytes,
                    "system_available_bytes": baseline.system_available_bytes,
                    "system_used_bytes":      baseline.system_used_bytes,
                }

            try:
                monitor = AdaptiveProcessMonitor(cfg, idx, baseline)
                result = monitor.run()
            except FileNotFoundError as exc:
                print("FATAL")
                sys.exit(f"\n[FATAL] {exc}")
            except Exception as exc:
                print("ERROR")
                traceback.print_exc()
                sys.exit(f"\n[FATAL] Trial {idx + 1}: {exc}")

            status = "✓" if result.exit_code == 0 else f"✗(exit {result.exit_code})"
            acc_str = (
                f"  acc={result.accuracy.accuracy_percent:.1f}%"
                if result.accuracy.accuracy_percent is not None else ""
            )
            print(
                f"{status}  "
                f"wall={_fmt_time(result.wall_time_s):>10}  "
                f"peak-RSS={_fmt_bytes(result.rss_peak_net_bytes):>10}  "
                f"samples={result.sample_count:>6}  "
                f"boosts={result.adaptive_boost_count}"
                f"{acc_str}"
            )

            if cfg.verbose and result.stderr.strip():
                print(f"     STDERR ↓  {result.stderr.strip()[:300]}")

            if result.is_cold_start:
                report.cold_start_trial = result
            else:
                report.steady_state_trials.append(result)

            if self._on_trial_complete is not None:
                try:
                    self._on_trial_complete(idx + 1, cfg.trials, result)
                except Exception:
                    pass

        _aggregate_steady_state(report)
        _print_report(report)

        if cfg.output_path:
            write_json_report(report, cfg.output_path)

        return report


# ============================================================================
# §11  CLI
# ============================================================================

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="perf_bench",
        description=(
            "AI-PerfBench v3.0 — High-Precision Cross-Platform Isolated Process Profiler\n"
            "Green AI & Sustainable Metrics Research Tool\n\n"
            "Pass the target command after '--' (recommended — no shell quoting needed):\n"
            "    python perf_bench.py --trials 7 -- python model.py --epochs 5\n\n"
            "Or use --cmd for a quoted string:\n"
            "    python perf_bench.py --cmd \"python model.py\" --trials 7"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Accuracy (Green AI Optimization Matrix):
  Add one line to your workload script:
      print(f"PERFBENCH_ACCURACY={accuracy * 100:.2f}")
  The profiler will capture it and include it in results.json.

Examples:
  python perf_bench.py --trials 7 --output results/run1.json -- python heavy_math.py
  python perf_bench.py --trials 5 --sample-ms 0.5 -- python model.py --epochs 10
  python perf_bench.py --cmd "python model.py" --trials 7 --verbose
  python perf_bench.py --spike-mb 1.0 --spike-boost 20 -- python data_loader.py
        """,
    )

    # ── Target command ──────────────────────────────────────────────────────
    p.add_argument("--cmd", metavar="CMD",
                   help="Command as a quoted string (alternative to '-- cmd args')")

    # ── Trial configuration ─────────────────────────────────────────────────
    p.add_argument("--trials", "-n", type=int, default=7, metavar="N",
                   help="Total sequential runs, including cold-start (default: 7)")
    p.add_argument("--warmup-drop", type=int, default=1, metavar="N",
                   help="Leading trials classified as cold-start, excluded from aggregate (default: 1)")

    # ── Sampling ────────────────────────────────────────────────────────────
    p.add_argument("--sample-ms", type=float, default=1.0, metavar="MS",
                   help="Base polling interval in milliseconds (default: 1.0 ms)")
    p.add_argument("--spinlock-ms", type=float, default=0.2, metavar="MS",
                   help="Intervals below this use busy-wait instead of sleep() "
                        "for sub-ms precision (default: 0.2 ms)")

    # ── Adaptive spike detection ────────────────────────────────────────────
    p.add_argument("--spike-mb", type=float, default=2.0, metavar="MB",
                   help="RSS change in MB that triggers adaptive oversampling (default: 2.0 MB)")
    p.add_argument("--spike-boost", type=float, default=10.0, metavar="X",
                   help="Frequency multiplier during spike detection (default: 10×)")
    p.add_argument("--spike-cooldown", type=int, default=30, metavar="N",
                   help="Samples at boosted rate before reverting to base interval (default: 30)")

    # ── Statistics ──────────────────────────────────────────────────────────
    p.add_argument("--outlier-z", type=float, default=2.5, metavar="Z",
                   help="Z-score threshold for sample outlier removal (default: 2.5)")

    # ── Accuracy ────────────────────────────────────────────────────────────
    p.add_argument("--accuracy-pattern", metavar="REGEX",
                   default=r"PERFBENCH_ACCURACY\s*=\s*([0-9.]+)",
                   help="Regex with one capture group (float) to extract accuracy from child stdout")

    # ── Output ──────────────────────────────────────────────────────────────
    p.add_argument("--output", "-o", metavar="PATH",
                   help="Write JSON report to PATH (parent directories auto-created)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print child stderr to console after each trial")

    # ── Pass-through: everything after '--' ─────────────────────────────────
    p.add_argument("remainder", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Resolve target command — passthrough after '--' takes priority over --cmd
    remainder = [a for a in (args.remainder or []) if a != "--"]

    if remainder:
        cmd_list = remainder
    elif args.cmd:
        cmd_list = parse_cmd_string(args.cmd)
    else:
        parser.error(
            "No target command specified.\n"
            "Usage:  python perf_bench.py --trials 7 -- python script.py\n"
            "    or  python perf_bench.py --cmd \"python script.py\" --trials 7"
        )

    output_path = Path(args.output) if args.output else None

    cfg = ProfilerConfig(
        cmd_list=cmd_list,
        trials=args.trials,
        sample_interval_s=args.sample_ms / 1000.0,
        spinlock_threshold_s=args.spinlock_ms / 1000.0,
        spike_rss_delta_bytes=int(args.spike_mb * 1024 * 1024),
        spike_boost_factor=args.spike_boost,
        spike_cooldown_samples=args.spike_cooldown,
        warmup_drop_trials=args.warmup_drop,
        outlier_z_threshold=args.outlier_z,
        accuracy_pattern=args.accuracy_pattern,
        output_path=output_path,
        verbose=args.verbose,
    )

    PerfBench(cfg).run()


if __name__ == "__main__":
    main()

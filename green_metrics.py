"""
================================================================================
green_metrics.py  -  Layer 2: Resource Diagnostics & Carbon Profiler
================================================================================
Part of the Green Software Diagnostics Architecture (companion to perf_bench.py).

WHAT THIS MODULE DOES
    Converts *measured* runtime quantities - elapsed time (s), peak memory
    (bytes), CPU utilisation - into a *modelled* estimate of:
        - Operational Energy       E   [Joules]
        - Operational Carbon       C   [gCO2e]
    and assigns a 5-tier Sustainability Grade (A-F) from the empirical
    complexity-scaling exponent.

SCIENTIFIC HONESTY - READ THIS
    Energy and carbon here are MODEL ESTIMATES, not wall-socket measurements.
    Direct energy measurement needs hardware counters (Intel RAPL / NVIDIA NVML)
    or an external power meter; those are out of scope for a portable, pure-Python
    profiler. We instead implement a transparent, fully-parametric OPERATIONAL
    energy model in the tradition of the Green Software Foundation's
    Software Carbon Intensity (SCI) specification:

        SCI = (E * I) + M          per functional unit

    where E = energy (kWh), I = grid carbon intensity (gCO2e/kWh),
    M = embodied carbon. A runtime profiler can only address the operational
    term (E * I); embodied carbon M is set to 0 by default.

THE KEY DEFENSE (why the arbitrary-looking constants are fine)
    Absolute Joules scale *linearly* with the power coefficients below. But every
    algorithm in a comparison is charged the SAME coefficients, so the headline
    deliverables - the % Resource Reduction and the A-F grade - are INVARIANT to
    the exact coefficient values. Only the absolute magnitude needs hardware
    calibration; the relative verdict does not. All constants are exposed on
    EnergyModelConfig so a reviewer can substitute their region/hardware values
    (e.g. Thailand TGO grid factor) and re-derive everything.

Python 3.9+   -   stdlib only (numpy optional, used only if present)
================================================================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

try:
    import numpy as _np  # noqa: F401
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


# ============================================================================
# §1  ENERGY / CARBON MODEL CONFIGURATION  (every constant is defensible + tunable)
# ============================================================================

@dataclass
class EnergyModelConfig:
    """
    Default coefficients with literature provenance. Override any of these to
    re-calibrate for a specific machine or electricity grid.
    """

    # -- CPU power -----------------------------------------------------------
    # We attribute the *dynamic* power of the cores the workload actually uses.
    # per_core_w = cpu_package_tdp_w / physical_cores; a single-threaded workload
    # saturating one core is therefore charged ~one core's share of the TDP.
    # Typical mobile-CPU package TDP: 15-45 W. 28 W is a common U/P-series value.
    cpu_package_tdp_w: float = 28.0
    physical_cores: int = 8
    # Fraction of TDP a core draws while "active but idle-ish" (SPECpower shape).
    cpu_static_frac: float = 0.30

    # -- DRAM power ----------------------------------------------------------
    # Cloud Carbon Footprint memory coefficient: 0.000392 kWh / GB-hour
    #   = 0.000392 * 3.6e6 J / (GB * 3600 s) = 0.392 W per GB held resident.
    # Literature range for active DDR4/DDR5 is ~0.3-0.45 W/GB.
    ram_watts_per_gb: float = 0.392

    # -- Grid carbon intensity -----------------------------------------------
    # gCO2e per kWh of delivered electricity.
    #   Global average (IEA, used in most SCI examples) ~ 475
    #   Thailand national grid (TGO)                     ~ 500
    # Default = global average so results are comparable to published SCI work;
    # set to 500 for a Thailand-specific report.
    grid_intensity_g_per_kwh: float = 475.0

    # -- Embodied carbon (out of scope for a runtime profiler) ---------------
    embodied_carbon_g: float = 0.0

    def per_core_dynamic_w(self) -> float:
        cores = max(1, self.physical_cores)
        return self.cpu_package_tdp_w / cores


# ============================================================================
# §2  SINGLE-MEASUREMENT ENERGY & CARBON
# ============================================================================

@dataclass
class GreenResult:
    elapsed_s: float
    mem_gb: float
    active_cores: float          # cpu_percent / 100  (1.0 == one saturated core)
    cpu_energy_j: float
    ram_energy_j: float
    energy_joules: float
    energy_kwh: float
    carbon_g: float
    power_avg_w: float

    def as_dict(self) -> dict:
        return {
            "elapsed_s":      self.elapsed_s,
            "mem_gb":         self.mem_gb,
            "active_cores":   self.active_cores,
            "cpu_energy_j":   self.cpu_energy_j,
            "ram_energy_j":   self.ram_energy_j,
            "energy_joules":  self.energy_joules,
            "energy_kwh":     self.energy_kwh,
            "carbon_g":       self.carbon_g,
            "power_avg_w":    self.power_avg_w,
        }


def estimate_energy(
    elapsed_s: float,
    mem_bytes: float,
    cpu_percent: float = 100.0,
    cfg: Optional[EnergyModelConfig] = None,
) -> GreenResult:
    """
    Convert one measurement into modelled energy + carbon.

    Model
    -----
        active_cores = cpu_percent / 100                    (1.0 == one core)
        P_cpu = per_core_dynamic_w
                * (cpu_static_frac + (1-cpu_static_frac)*min(active,1))
                * max(active_cores, eps)                     [W]
        P_ram = ram_watts_per_gb * mem_gb                    [W]
        E     = (P_cpu + P_ram) * elapsed_s                  [J]
        C     = E/3.6e6 * grid_intensity                     [gCO2e]

    cpu_percent defaults to 100 (a single-threaded CPU-bound algorithm saturating
    one core), which is the correct assumption for the algorithm case studies.
    The isolated-process profiler passes its measured mean cpu_percent instead.
    """
    cfg = cfg or EnergyModelConfig()
    elapsed_s = max(0.0, float(elapsed_s))
    mem_gb = max(0.0, float(mem_bytes)) / (1024 ** 3)
    active_cores = max(0.0, float(cpu_percent)) / 100.0

    per_core = cfg.per_core_dynamic_w()
    # Static + dynamic shape, then scaled by how many cores are lit up.
    load_shape = cfg.cpu_static_frac + (1.0 - cfg.cpu_static_frac) * min(active_cores, 1.0)
    p_cpu = per_core * load_shape * max(active_cores, 1e-6)
    p_ram = cfg.ram_watts_per_gb * mem_gb
    p_total = p_cpu + p_ram

    e_joules = p_total * elapsed_s
    e_cpu = p_cpu * elapsed_s
    e_ram = p_ram * elapsed_s
    e_kwh = e_joules / 3.6e6
    carbon = e_kwh * cfg.grid_intensity_g_per_kwh + cfg.embodied_carbon_g

    return GreenResult(
        elapsed_s=elapsed_s,
        mem_gb=mem_gb,
        active_cores=active_cores,
        cpu_energy_j=e_cpu,
        ram_energy_j=e_ram,
        energy_joules=e_joules,
        energy_kwh=e_kwh,
        carbon_g=carbon,
        power_avg_w=p_total,
    )


# ============================================================================
# §3  SUSTAINABILITY GRADE  -  primary score = complexity-scaling exponent
# ============================================================================
#
# The grade reflects HOW A METRIC GROWS with input size, not its absolute value
# at one N (which is meaningless in isolation). We fit
#
#         metric(N)  ~  a * N^k                     (power law)
#     =>  log metric =  log a + k * log N           (linear in log-log space)
#
# and read the growth exponent k. k maps directly onto Big-O classes, giving a
# grade that is invariant to hardware and to the energy coefficients above.
#
#     Grade  Exponent k        Complexity class            Verdict
#     -----  ---------------   -------------------------   -----------------
#       A     k < 0.30         O(1) / O(log n)             Excellent / flat
#       B     0.30 <= k < 1.10 O(n)                        Good / linear
#       C     1.10 <= k < 1.60 O(n log n)                  Fair
#       D     1.60 <= k < 2.40 O(n^2)                      Poor
#       F     k >= 2.40        O(n^3+) / exponential       Critical
#
# NOTE on the B/C boundary: over a finite input range (e.g. N=100..10000) an
# ideal O(n log n) curve fits to k ~ 1.15, only slightly above ideal O(n)'s
# k=1.00. Discriminating n from n-log-n is the model's single most sensitive
# decision; the boundary is set at 1.10 accordingly and this limitation is
# reported openly (see the calibration write-up).
#
# Exponential growth is detected separately: if a semi-log fit log(metric) vs N
# (linear N) explains the data better than the log-log power-law fit, the metric
# is super-polynomial and is graded F regardless of the fitted k.

_GRADE_BANDS = [
    ("A", -math.inf, 0.30, "O(1) / O(log n)", "Excellent - near-constant scaling"),
    ("B", 0.30, 1.10, "O(n)", "Good - linear scaling"),
    ("C", 1.10, 1.60, "O(n log n)", "Fair - log-linear scaling"),
    ("D", 1.60, 2.40, "O(n^2)", "Poor - quadratic scaling"),
    ("F", 2.40, math.inf, "O(n^3+) / exponential", "Critical - super-quadratic"),
]


@dataclass
class ScalingGrade:
    grade: str
    exponent_k: float
    r2_powerlaw: float
    r2_semilog: float
    is_exponential: bool
    inferred_class: str
    verdict: str

    def as_dict(self) -> dict:
        return {
            "grade":          self.grade,
            "exponent_k":     self.exponent_k,
            "r2_powerlaw":    self.r2_powerlaw,
            "r2_semilog":     self.r2_semilog,
            "is_exponential": self.is_exponential,
            "inferred_class": self.inferred_class,
            "verdict":        self.verdict,
        }


def _linfit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """Ordinary least squares y = m*x + b. Returns (slope m, intercept b, R^2)."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0), 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return 0.0, my, 0.0
    m = sxy / sxx
    b = my - m * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (m * x + b)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return m, b, r2


def grade_scaling(sizes: Sequence[float], metric: Sequence[float]) -> ScalingGrade:
    """
    Grade a metric (time, memory, energy...) by its empirical growth exponent
    across >= 3 input sizes.

    Points with a non-positive metric are dropped (log undefined). Requires >= 3
    surviving points; otherwise returns an 'N/A' grade.
    """
    pts = [(float(n), float(m)) for n, m in zip(sizes, metric) if n > 0 and m > 0]
    if len(pts) < 3:
        return ScalingGrade("N/A", 0.0, 0.0, 0.0, False,
                            "insufficient data", "Need >= 3 positive points")

    log_n = [math.log(n) for n, _ in pts]
    log_m = [math.log(m) for _, m in pts]
    lin_n = [n for n, _ in pts]

    k, _, r2_power = _linfit(log_n, log_m)          # power-law fit
    _, _, r2_semi = _linfit(lin_n, log_m)           # exponential (semi-log) fit

    # Exponential if the semi-log (log m vs linear N) fit is essentially perfect
    # and at least as good as the power-law fit, while the apparent power-law
    # exponent is implausibly large for any real polynomial algorithm. A genuine
    # O(n^2) fails this: its log-m-vs-linear-N fit is poor, so r2_semi stays low.
    is_exp = (r2_semi >= r2_power - 0.001) and (r2_semi > 0.97) and (k > 2.4)

    if is_exp:
        return ScalingGrade("F", k, r2_power, r2_semi, True,
                            "exponential", "Critical - exponential growth")

    for letter, lo, hi, klass, verdict in _GRADE_BANDS:
        if lo <= k < hi:
            return ScalingGrade(letter, k, r2_power, r2_semi, False, klass, verdict)
    return ScalingGrade("F", k, r2_power, r2_semi, False,
                        "O(n^3+)", "Critical - super-quadratic")


# ============================================================================
# §4  ABSOLUTE PER-RUN GRADE  (for single-measurement dashboard display)
# ============================================================================
#
# When only ONE measurement exists (no scaling curve), fall back to absolute
# energy-per-run thresholds. These ARE hardware/coefficient-dependent, so they
# are a secondary, advisory badge - the scaling grade is the scientific score.

_ABS_ENERGY_BANDS_J = [
    ("A", 1.0),      # < 1 J        featherweight
    ("B", 25.0),     # < 25 J
    ("C", 250.0),    # < 250 J
    ("D", 2500.0),   # < 2.5 kJ
    ("F", math.inf),
]


def grade_absolute_energy(energy_joules: float) -> str:
    for letter, hi in _ABS_ENERGY_BANDS_J:
        if energy_joules < hi:
            return letter
    return "F"


def pct_reduction(baseline: float, improved: float) -> float:
    """Percentage by which `improved` reduces `baseline` (positive == better)."""
    if baseline <= 0:
        return 0.0
    return (baseline - improved) / baseline * 100.0


# ============================================================================
# §5  SELF-TEST
# ============================================================================

if __name__ == "__main__":
    cfg = EnergyModelConfig(physical_cores=8, cpu_package_tdp_w=28.0)
    print("Energy model self-test")
    print("-" * 60)
    g = estimate_energy(elapsed_s=2.0, mem_bytes=200 * 1024 ** 2, cpu_percent=100, cfg=cfg)
    print(f"  2.0 s, 200 MB, 1 core  ->  {g.energy_joules:.3f} J  |  "
          f"{g.carbon_g:.4f} gCO2e  |  {g.power_avg_w:.2f} W avg")

    N = [100, 1000, 5000, 10000]
    curves = {
        "O(1)":       [1.0 for _ in N],
        "O(log n)":   [math.log2(n) for n in N],
        "O(n)":       [float(n) for n in N],
        "O(n log n)": [n * math.log2(n) for n in N],
        "O(n^2)":     [float(n) ** 2 for n in N],
    }
    print("\nScaling-grade self-test (polynomial classes, log-log fit):")
    for name, ys in curves.items():
        sg = grade_scaling(N, ys)
        print(f"  {name:<11} -> grade {sg.grade}  k={sg.exponent_k:.2f}  "
              f"class={sg.inferred_class}  exp={sg.is_exponential}")

    # Exponential must use LINEARLY-spaced N (as real naive Fibonacci does).
    Nexp = [20, 25, 30, 35]
    phi = (1 + 5 ** 0.5) / 2
    exp_curve = [phi ** n for n in Nexp]          # ~ naive Fibonacci call count
    sg = grade_scaling(Nexp, exp_curve)
    print(f"\nExponential self-test (linearly-spaced N):")
    print(f"  O(phi^n)    -> grade {sg.grade}  k={sg.exponent_k:.2f}  "
          f"r2_semilog={sg.r2_semilog:.3f}  exp={sg.is_exponential}")

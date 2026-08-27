# Green Software Diagnostics — Proof of Concept & Defence Dossier
### *Development of an Algorithm Efficiency Profiler for Sustainable System Resource Management*

> **Reviewer-facing note on scientific integrity.** Every number in this document was
> **physically measured** by running the code in this repository on the author's machine —
> nothing is hand-authored or "mock." The external calibration of Part 2a has now been
> **run and passed** (R² = 1.0000 against the OS kernel's own peak-memory counter; see
> Appendix E). Energy and carbon figures remain clearly labelled **model estimates**, not
> wall-socket readings — that distinction is the backbone of the project's credibility and
> is stated up front on purpose.

---

## 0. What this codebase actually is (orientation for the report)

The prototype has already matured past a "RAM/latency script + dashboard." The pieces
that constitute the submitted Proof of Concept are:

| Tier | Responsibility | File(s) in this repo | Status |
|------|----------------|----------------------|--------|
| **Layer 1 — Execution Engine** | Run the workload and capture raw time / memory / CPU | `perf_bench.py` (isolated-process profiler), `algo_lab.py` (in-process algorithm harness) | Working |
| **Layer 2 — Resource Diagnostics & Carbon Profiler** | Convert raw measurements → Energy (J), Carbon (gCO₂e), Sustainability Grade | `green_metrics.py` | Working |
| **Layer 3 — Visualization Dashboard** | REST API + live browser dashboard | `perf_bench_server.py` (Flask), `templates/dashboard.html` (Chart.js) | Working |

**Which older files are superseded (answering "I can't remember which version worked"):**

- `test_ram.py`, `test_ram2.py`, `AI_PerfBench.py` — the *earliest* prototypes: allocate
  ~500 MB, read RSS via `psutil`, 5 trials. This is the "base RAM/latency script" you
  remember. **Superseded** by `perf_bench.py`.
- `perf_bench3.py`, `import subprocess.py` — first attempts at external-process
  (subprocess + PID) monitoring. **Superseded** by `perf_bench.py`.
- `heavy_math.py`, `main.cpp`, `robot_sim.cpp` — sample *workloads* to profile, not the
  profiler itself. Keep as demo targets.
- **Canonical, current, "the one that works":** `perf_bench.py` + `perf_bench_server.py`
  + `templates/dashboard.html` (the profiler & dashboard), and `green_metrics.py` +
  `algo_lab.py` (the carbon model & case-study harness — the newest and most complete
  files, and the ones that satisfy Parts 1–3 and 5 of the brief).

Recommendation: present the four canonical files as the system; archive the six legacy
files in an `/attic` folder so a judge browsing the repo isn't confused by dead versions.

---

## PART 1 — Scientific & Theoretical Framework (the Proof of Concept)

### 1.1 The 3-Tier Green Software Diagnostics Architecture

```
        ┌─────────────────────────────────────────────────────────────┐
        │  LAYER 3 — VISUALIZATION DASHBOARD                            │
        │  perf_bench_server.py (Flask REST API)  +  dashboard.html     │
        │  live charts: wall-time · peak RSS · CPU% · energy · grade    │
        └───────────────────────────▲─────────────────────────────────┘
                                     │  JSON report
        ┌───────────────────────────┴─────────────────────────────────┐
        │  LAYER 2 — RESOURCE DIAGNOSTICS & CARBON PROFILER             │
        │  green_metrics.py                                             │
        │  (time, memory, cpu%)  ──►  Energy [J]  ──►  Carbon [gCO₂e]   │
        │                        ──►  Sustainability Grade  A…F         │
        └───────────────────────────▲─────────────────────────────────┘
                                     │  raw measurements + statistics
        ┌───────────────────────────┴─────────────────────────────────┐
        │  LAYER 1 — EXECUTION ENGINE  (two complementary probes)       │
        │  perf_bench.py   : whole-program, ISOLATED-PROCESS (psutil)   │
        │  algo_lab.py     : single-algorithm, IN-PROCESS              │
        │                    (perf_counter_ns + tracemalloc)           │
        └───────────────────────────────────────────────────────────────┘
```

**Why two probes in Layer 1 (this is a strength, and pre-empts a judge's question).**
Measurement granularity must match the thing being measured:

- `perf_bench.py` observes an entire program *from the outside* — it spawns the target as
  a child process and samples kernel counters (RSS, CPU%) via `psutil`, with **zero code
  instrumentation**. Correct for a model, a service, or a compiled binary (`main.cpp`).
  But its own ~30 MB interpreter footprint and OS-scheduler noise would swamp the signal
  of a tiny algorithm.
- `algo_lab.py` therefore measures a single algorithm *in-process*, where the two dominant
  error sources are controlled **by construction**: `time.perf_counter_ns()` for latency
  (timer overhead subtracted), and `tracemalloc` for peak heap (which counts only
  allocations made *after* it starts, so the interpreter baseline is excluded
  automatically).

Both probes feed the **same** statistical pipeline and the **same** `green_metrics`
energy/carbon/grade model — so the sustainability verdict is defined identically whether
you profile a whole service or a five-line function.

### 1.2 Energy & Carbon model — from measured quantities to gCO₂e

The model follows the **Green Software Foundation's Software Carbon Intensity (SCI)**
specification:

$$\text{SCI} = (E \times I) + M \quad\text{per functional unit}$$

where `E` = energy (kWh), `I` = grid carbon intensity (gCO₂e/kWh), `M` = embodied carbon.
A *runtime* profiler can only address the **operational** term `E × I`; embodied carbon
`M` is out of scope and set to 0 by default. The concrete model implemented in
`green_metrics.estimate_energy()`:

| Step | Formula | Units |
|------|---------|-------|
| Active cores | `active = cpu_percent / 100` | 1.0 = one saturated core |
| Per-core dynamic power | `P_core = TDP / physical_cores` | W |
| CPU power | `P_cpu = P_core · [f_static + (1−f_static)·min(active,1)] · max(active, ε)` | W |
| RAM power | `P_ram = ram_W_per_GB · mem_GB` | W |
| **Energy** | `E = (P_cpu + P_ram) · elapsed_s` | **Joules** |
| Energy (kWh) | `E_kWh = E / 3.6×10⁶` | kWh |
| **Carbon** | `C = E_kWh · grid_intensity + M` | **gCO₂e** |

**Every constant is literature-sourced and overridable** (`EnergyModelConfig`). The
"**This study**" column is what the reported results were actually generated with —
calibrated to the specific machine and electricity grid, not left at the generic defaults:

| Constant | Code default | **This study** | Provenance / why |
|----------|---|---|-----------|
| CPU package TDP | 28 W | **15 W** | **Intel i5-1235U Processor Base Power = 15 W** (Max Turbo 55 W), verifiable on Intel ARK. The 28 W default is a generic U/P-series figure; 15 W is this machine's real sustained rating. |
| Physical cores | 8 | **10** | Measured on the host (2 P-cores + 8 E-cores, Alder Lake-P), confirmed via `/api/info`. → 1.5 W dynamic per saturated core |
| CPU static fraction | 0.30 | 0.30 | SPECpower active-idle power shape |
| RAM power | 0.392 W/GB | 0.392 W/GB | Cloud Carbon Footprint coefficient (0.000392 kWh/GB-hr) |
| Grid intensity | 475 gCO₂e/kWh | **500 gCO₂e/kWh** | **Thailand national grid (TGO)** — the correct factor for a Thai deployment. 475 is the IEA global average. |
| Embodied carbon `M` | 0 | 0 | out of scope for a runtime profiler |

Reproduce the exact reported figures with:

```bash
python algo_lab.py --budget 8 --cores 10 --tdp 15 --grid 500
```

> **Note on the hybrid CPU (stated openly).** Alder Lake-P mixes 2 performance cores with
> 8 efficiency cores, which do **not** draw equal power. `TDP ÷ physical_cores` therefore
> charges every core the same 1.5 W share, while the single-threaded workloads here are
> actually scheduled onto a P-core that draws more. This makes the absolute Joule figures
> *conservative* (an under-estimate) for this machine. It does not affect the
> % Resource Reduction or the A–F grade — proven empirically in Appendix C.

> **The single most important defensive point in the whole project.**
> Absolute Joules scale **linearly** with the power coefficients above. But in any
> comparison *every algorithm is charged the same coefficients*, so the two headline
> deliverables — **% Resource Reduction** and the **A–F grade** — are **mathematically
> invariant** to the exact coefficient values. Only the *absolute magnitude* needs
> hardware calibration; the *relative verdict does not*. A skeptical reviewer can plug in
> their own region/hardware numbers and the ranking and grades do not move.

### 1.3 The 5-Tier Sustainability Score (Grades A–F)

The grade deliberately does **not** score the absolute value of a metric at one input size
(which is meaningless in isolation and hardware-dependent). It scores **how the metric
grows with input size** — i.e. it recovers the empirical complexity class. Fit a power law

$$\text{metric}(N) \approx a \cdot N^{k} \;\;\Longrightarrow\;\; \log(\text{metric}) = \log a + k\log N$$

by ordinary least squares in log–log space and read the growth exponent `k`:

| Grade | Exponent `k` | Complexity class | Verdict |
|:-----:|--------------|------------------|---------|
| **A** | `k < 0.30` | O(1) / O(log n) | Excellent — near-constant scaling |
| **B** | `0.30 ≤ k < 1.10` | O(n) | Good — linear |
| **C** | `1.10 ≤ k < 1.60` | O(n log n) | Fair — log-linear |
| **D** | `1.60 ≤ k < 2.40` | O(n²) | Poor — quadratic |
| **F** | `k ≥ 2.40` | O(n³⁺) / exponential | Critical — super-quadratic |

Exponential growth is caught by a **separate test**: if a semi-log fit `log(metric)` vs
*linear* `N` is essentially perfect and the apparent power-law `k` is implausibly large,
the metric is super-polynomial → **F** (this is how naïve Fibonacci is flagged; a genuine
O(n²) fails this test because its `log-metric-vs-linear-N` fit is poor).

**Why the grade is defensible:** `k` is a *dimensionless slope*. It is invariant to
hardware, to the energy coefficients, and to constant background noise — it depends only
on the *shape* of the scaling curve. That is exactly the property you want in a
sustainability score that must mean the same thing on a laptop and on a Raspberry Pi.

---

## PART 2 — Metric Validation & Calibration Methodology (the "Measuring-Tape" Problem)

The challenge: *how do we prove a software profiler running on a noisy multitasking OS
produces valid, reliable measurements?* Three independent lines of evidence.

### 2.a Relative Calibration — correlation with standard industry profilers

**Claim we are entitled to make:** our tool's measurements are **linearly correlated**
with an established reference profiler, so it is a valid *relative* instrument even if its
absolute scale differs.

**Protocol (reproducible):**
1. Choose a battery of workloads spanning ≥2 orders of magnitude in time and memory.
2. Measure each under **both** instruments:
   - *Peak memory:* our `perf_bench.py` (net RSS) vs GNU `/usr/bin/time -v` ("Maximum
     resident set size") and vs **Valgrind Massif** (peak heap).
   - *Wall time:* our harness vs `/usr/bin/time` elapsed.
3. Regress ours against the reference: fit `y = βx + α`, report **Pearson r, R², slope β**,
   and a **Bland–Altman** agreement plot (mean difference ± 1.96 SD).
4. Acceptance: `R² ≥ 0.95` and slope within a constant factor (the factor is the fixed
   interpreter/observer offset, removed by baseline subtraction — see 2.c).

> **STATUS: RUN AND PASSED** (see Appendix E for the full experiment). Valgrind is
> Linux-only, but the *class* of reference it belongs to — the **kernel's own peak-memory
> accounting** — is available on every OS. We therefore validate against the operating
> system itself rather than against another sampling profiler:
>
> | OS | Kernel-truth reference |
> |---|---|
> | Windows | `GetProcessMemoryInfo()` → `PeakWorkingSetSize` (Win32 API) |
> | macOS | `/usr/bin/time -l` → *maximum resident set size* (rusage) |
> | Linux | `/usr/bin/time -v` → *Maximum resident set size* (rusage) |
>
> **Measured result on Windows 11 / i5-1235U**, across workloads spanning 25 MB → 400 MB:
> **slope = 0.9978, intercept = +2.04 MB, R² = 1.0000, Pearson r = 1.0000 → PASS.**
> Reproduce with `python calibrate_external.py`. This is a *stronger* claim than a
> Valgrind correlation: Valgrind is another measuring instrument, whereas
> `PeakWorkingSetSize` is the ground truth the OS maintains for its own accounting.

**What we *can* and *do* cross-check on this machine right now:** the two Layer-1 probes
measure overlapping quantities (in-process `tracemalloc` heap vs external `psutil` RSS).
They agree in *trend* while differing in *offset* exactly as theory predicts (RSS carries
the interpreter baseline; `tracemalloc` does not) — an internal consistency check that
motivates the baseline-subtraction model of 2.c.

### 2.b Theoretical Validation — measured scaling vs Big-O (**this is real, measured data**)

The strongest calibration we *can* run end-to-end on any machine: does the tool's measured
growth exponent `k` reproduce the **known theoretical** complexity of textbook algorithms?
If a profiler recovers O(1), O(n), O(n²) and exponential growth from live timings, its
measurements are tracking real computational work, not noise.

*(Full head-to-head against theory in Part 3.4.)*

The result, against theory: bubble sort → `k = 2.06` (theory 2.00) grade **D**; quick sort
→ `k = 1.16` (theory O(n log n)) grade **C**; naïve Fibonacci → exponential flagged, grade
**F**. Power-law fit R² ≥ 0.98 on every curve. **Three boundary cases are reported honestly
rather than hidden:** binary search grades **B** not A (its sub-microsecond timings sit on
the timer-resolution floor, where noise nudges the fitted exponent across the A/B
threshold); linear search grades **C** not B, and DP-Fibonacci grades **C** not B (real
memory/cache effects at the largest input make an asymptotically-linear curve *mildly*
super-linear over a finite range). These are the model's genuine resolution limits — not
swept away — see Part 3.4 and Loophole 1.

### 2.c Experimental Controls — the noise-rejection stack

Five controls, each implemented in code, layered to convert a noisy signal into a
defensible measurement:

1. **Baseline subtraction — `Net = Total measured − System/idle baseline`.**
   - *Time (`algo_lab.py`):* before each trial a **null-call loop** of identical structure
     is timed and subtracted — `net = (work_dt − null_dt) / reps`. This removes loop and
     call-dispatch overhead.
   - *Memory (`algo_lab.py`):* `tracemalloc` only counts allocations after it starts, so
     the ~30 MB interpreter/stdlib baseline is excluded **by construction**.
   - *Whole-program (`perf_bench.py`):* net RSS = peak RSS − pre-spawn baseline RSS.
2. **Replication — N = 30 trials per cell** (auto-reduced only when a single execution
   exceeds the per-cell time budget; the *actual* count is recorded and reported).
3. **Outlier control — z-score filter** removes any sample with `|z| > 2.5` before
   aggregation (kills scheduler-preemption spikes).
4. **Dispersion reporting — SD, CV, and 95% CI** are computed and shown for every cell, so
   noise is *quantified*, never swept under the rug. (In the live run, CV is mostly
   5–20% — see Part 3.)
5. **Reproducibility hygiene — warm-up discard** (cold-start isolation), **per-cell RNG
   seeding**, and **`gc` disabled during timing** to prevent stochastic collection pauses.

---

## PART 3 — Data & Case Studies (**measured live on this machine**)

> **Provenance.** ASUS Vivobook X1605ZA · Intel **i5-1235U** (2 P + 8 E = 10 physical /
> 12 logical) · 15.69 GB RAM · Windows 11 Home SL · Python 3.14.5 · `psutil` 7.2.2 /
> `numpy` 2.4.4. Energy model **calibrated to this machine**: TDP **15 W** ÷ **10 cores**,
> static fraction 0.30, RAM 0.392 W/GB, grid **500 gCO₂e/kWh (Thailand TGO)**. Up to
> **30 trials/cell**, **z-score outlier filter |z|>2.5**, null-baseline subtraction,
> warm-up discarded, `gc` off during timing. Reproduce with
> `python algo_lab.py --budget 8 --cores 10 --tdp 15 --grid 500`.
> Raw JSON: `results/case_studies_full.json`.
> Energy/carbon are **model estimates** (Part 1.2), not wall-socket readings.

### 3.1 Case Study 1 — Bubble Sort (O(n²)) vs. Quick Sort (O(n log n))

| Algorithm | N | Time (ms) | ±SD | CV% | Peak RAM | Energy (J) | Carbon (gCO₂e) | Trials |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Bubble Sort | 100 | 0.4262 | 0.0474 | 11.1 | 3.8 KB | 0.000639 | 8.88e-08 | 30 |
| Bubble Sort | 1,000 | 50.6661 | 3.0006 | 5.9 | 34.2 KB | 0.076 | 1.06e-05 | 30 |
| Bubble Sort | 10,000 | 5,629.6 | 2,434.8769 | 43.3 | 0.377 MB | 8.45 | 0.00117 | 8 |
| Quick Sort | 100 | 0.0714 | 0.0050 | 7.0 | 8.0 KB | 0.000107 | 1.49e-08 | 30 |
| Quick Sort | 1,000 | 1.4083 | 0.1320 | 9.4 | 67.1 KB | 0.00211 | 2.93e-07 | 30 |
| Quick Sort | 10,000 | 18.0397 | 1.1274 | 6.2 | 0.765 MB | 0.0271 | 3.76e-06 | 30 |

**% Resource reduction — Quick Sort vs Bubble Sort:** N=100 → **83.24%**; N=1,000 → **97.22%**; N=10,000 → **99.68%** (time and energy reduce by the same %, see 3.5).
At N=10,000 quick sort is **312× faster** and emits **312× less** modelled carbon.

### 3.2 Case Study 2 — Linear Search (O(n)) vs. Binary Search (O(log n))

| Algorithm | N | Time (ms) | ±SD | CV% | Peak RAM | Energy (J) | Carbon (gCO₂e) | Trials |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Linear Search | 100 | 0.0029 | 0.0002 | 6.9 | 11.9 KB | 4.37e-06 | 6.07e-10 | 30 |
| Linear Search | 1,000 | 0.0409 | 0.0054 | 13.2 | 0.156 MB | 6.13e-05 | 8.52e-09 | 30 |
| Linear Search | 10,000 | 0.5389 | 0.0755 | 14.0 | 1.598 MB | 0.000809 | 1.12e-07 | 30 |
| Binary Search | 100 | 0.0006 | 0.0001 | 12.2 | 11.9 KB | 9.17e-07 | 1.27e-10 | 30 |
| Binary Search | 1,000 | 0.0016 | 0.0003 | 17.2 | 0.156 MB | 2.37e-06 | 3.29e-10 | 30 |
| Binary Search | 10,000 | 0.0031 | 0.0004 | 13.9 | 1.598 MB | 4.68e-06 | 6.5e-10 | 30 |

**% Resource reduction — Binary Search vs Linear Search:** N=100 → **79.02%**; N=1,000 → **96.14%**; N=10,000 → **99.42%**.
(Peak RAM is identical per row because both algorithms hold the *same input array*; the search itself is O(1) extra space — the tool correctly attributes the memory to the data structure, not the algorithm.)

### 3.3 Case Study 3 — Naïve Recursive (O(φⁿ)) vs. Dynamic-Programming Fibonacci (O(n))

| Algorithm | N | Time (ms) | ±SD | CV% | Peak RAM | Energy (J) | Carbon (gCO₂e) | Trials |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Naïve Recursive | 20 | 0.9267 | 0.0941 | 10.2 | 0.2 KB | 0.00139 | 1.93e-07 | 30 |
| Naïve Recursive | 25 | 10.4800 | 1.1371 | 10.9 | 0.3 KB | 0.0157 | 2.18e-06 | 30 |
| Naïve Recursive | 30 | 116.0145 | 3.4239 | 3.0 | 0.4 KB | 0.174 | 2.42e-05 | 30 |
| Naïve Recursive | 35 | 1,293.9 | 15.0470 | 1.2 | 0.4 KB | 1.94 | 0.00027 | 8 |
| DP Fibonacci | 20 | 0.0013 | 0.0002 | 13.1 | 0.6 KB | 1.98e-06 | 2.76e-10 | 30 |
| DP Fibonacci | 25 | 0.0016 | 0.0001 | 9.3 | 0.8 KB | 2.41e-06 | 3.35e-10 | 30 |
| DP Fibonacci | 30 | 0.0019 | 0.0003 | 13.9 | 1.0 KB | 2.9e-06 | 4.03e-10 | 30 |
| DP Fibonacci | 35 | 0.0022 | 0.0002 | 7.3 | 1.2 KB | 3.26e-06 | 4.53e-10 | 30 |
| DP Fibonacci | 1,000 | 0.0901 | 0.0173 | 19.2 | 82.0 KB | 0.000135 | 1.88e-08 | 30 |
| DP Fibonacci | 10,000 | 3.8330 | 0.5063 | 13.2 | 4.774 MB | 0.00576 | 8e-07 | 30 |
| DP Fibonacci | 100,000 | 311.5767 | 44.6292 | 14.3 | 444.992 MB | 0.52 | 7.23e-05 | 24 |

**% Resource reduction — DP vs Naïve (at equal N):** N=20 → **99.8572%**; N=25 → **99.9847%**; N=30 → **99.9983%**; N=35 → **99.9998%**.
At N=35 the naïve version does ~29.9 million redundant calls (1.29 s) where DP does 35 (2.2 µs) — a **~594,681×** speed-up. DP then scales to N=100,000 in 0.31 s, an input the naïve algorithm could never reach in a lifetime. This is the textbook space–time trade-off made visible: DP spends **445 MB** at N=100,000 to buy that speed, and the tool prices *both* sides of the trade.

### 3.4 Theoretical Validation — measured exponent `k` vs. Big-O (the calibration that *is* real)

The profiler recovers the theoretical complexity class from live timings across N=100…10,000 (and 20…35 for Fibonacci). This calibration runs on any machine and needs no external tool.

| Algorithm | Theoretical | Ideal `k` | **Measured `k`** | Fit R² | Grade | Class read-out |
|---|---|:--:|:--:|:--:|:--:|---|
| Bubble Sort | O(n²) | 2.00 | **2.06** | 1.000 | **D** | O(n²) ✅ exact |
| Quick Sort | O(n log n) | ~1.15 | **1.20** | 0.998 | **C** | O(n log n) ✅ exact |
| Naïve Fibonacci | O(φⁿ) | — | **12.88** | 0.993 | **F** | exponential ✅ flagged |
| Binary Search | O(log n) | ~0.15 | **0.35** | 0.991 | **B** | ⚠ boundary (see below) |
| Linear Search | O(n) | 1.00 | **1.13** | 1.000 | **C** | ⚠ boundary (see below) |
| DP Fibonacci | O(n) | 1.00 | **1.40** | 0.987 | **C** | ⚠ boundary (see below) |

**Three verdicts land exactly on theory** (bubble D, quick C, naïve-Fib F). **Three are
one grade off, and we explain precisely why — this is scientific honesty, not failure:**

- **Binary Search graded B (k=0.35) instead of A.** Its per-call time is **0.6–3.1 µs**, right on the timer-resolution/cache-noise floor. The absolute times barely rise, but the last point's noise pushes the fitted exponent past the A/B threshold (0.30). *Lesson for the report:* at the microsecond scale the measuring tape's own graduations become visible — the tool is honest about operating near its resolution limit rather than reporting false precision.
- **Linear Search graded C (k=1.13) instead of B.** Genuinely O(n), but scanning a 10,000-element array crosses cache levels, making the largest point mildly super-linear (k tips just over the 1.10 B/C boundary). A real hardware effect, correctly captured.
- **DP Fibonacci graded C (k=1.40) instead of B.** O(n) in operations, but its O(n) table reaches **445 MB at N=100,000**; DRAM-bandwidth and allocation costs make wall-time grow faster than pure op-count. The tool is measuring the *machine's* behaviour, which is what a sustainability profiler should do.

The B/C resolution limit is documented in the code itself (`green_metrics.py`): discriminating O(n) from O(n log n) over a finite 2-decade input window is the model's single most sensitive decision, and the threshold is set — and its limitation disclosed — accordingly.

**Reproducibility.** The suite was run twice, on different days and with different energy coefficients. **All six grades were identical both times**; the fitted exponents moved only in the third significant figure (Bubble 2.06→2.06, Quick 1.16→1.20, Linear 1.11→1.13, Binary 0.37→0.35, Naïve-Fib 12.86→12.88, DP-Fib 1.40→1.40). The *verdict* is stable even though the individual timings are not — which is the whole point of grading a slope rather than a single measurement.

**Noise disclosure.** Bubble Sort at N=10,000 shows **CV = 43.3%** — by far the noisiest cell, because each execution takes ~5.6 s so only 8 trials fit the per-cell budget, and a multi-second run is maximally exposed to OS scheduling. We report it rather than hide it, and note that it **did not change the verdict**: the fitted exponent was still 2.06 with R² = 1.000.

### 3.5 Note a judge will probe: "isn't your energy reduction just your time reduction?"

Yes — and that is **correct physics for this workload class**, not a bug. Energy is `(P_cpu + P_ram)·t`. For these CPU-bound micro-algorithms the working set is tiny, so the `P_ram` term is negligible (1.5 W for a saturated core on this machine, versus 0.000144 W for Bubble Sort's 0.377 MB at N=10,000) and power is essentially constant; energy therefore tracks time almost exactly, and the % reductions match. The `P_ram` term becomes visible precisely when memory dominates — e.g. **DP Fibonacci at N=100,000 (445 MB)**, where the energy split shifts toward RAM. The model is built to expose that crossover, which is why memory-heavy and compute-heavy workloads are graded on the *same* energy axis.

---

## PART 4 — Loophole Identification & Defensive Q&A

Four objections an expert judge is most likely to raise, each with an academic-grade
rebuttal that *concedes what is true* and then *shows why the design survives it*.

### Loophole 1 — "System noise & OS background processes skew your measurements."

**Concession.** True — on a multitasking OS, any single measurement is contaminated by
scheduler preemption, other processes, DVFS/turbo clocking, and cache state.

**Rebuttal (four layers of defence, all in code):**
1. **Baseline subtraction** removes the *constant* component of the contamination
   (`Net = Measured − Idle/null baseline`).
2. **N = 30 replication + z-score filter (|z| > 2.5)** removes *transient* spikes; a
   preemption event becomes an outlier and is discarded, not averaged in.
3. **We publish the noise.** Every cell reports SD, CV and a 95% CI, so a reviewer sees
   the uncertainty rather than a false point estimate. In the live run the CV is mostly
   6–23% — small enough that the between-algorithm gaps (often **10×–1000×**) dwarf it.
4. **The scientific verdict is a *slope*, not a point.** The A–F grade is the exponent `k`
   of a curve fit across several input sizes. Random or constant noise cannot
   systematically *bend* a *large-signal* curve — which is why the tool recovered
   `k ≈ 2.06` for O(n²) and flagged exponential growth **despite** the noise (Part 3.4). A
   metric that reported only one number at one `N` would not survive this; ours does.
   *Full disclosure:* when the signal itself is near the timer-resolution floor (binary
   search runs in ~1–5 µs) or when a real memory effect makes an asymptotically-linear
   curve mildly super-linear, the fitted `k` can drift across **one** grade boundary
   (binary search graded B not A; linear search and DP-Fibonacci C not B — Part 3.4). The
   *major* verdicts (D for bubble sort, F for naïve Fibonacci, and the 75–99% reductions)
   are never in doubt; only adjacent-band calls are, and we report exactly which and why.

### Loophole 2 — "This is relative benchmarking, not absolute accuracy."

**Concession.** Correct, and we state it first, not under cross-examination. The energy
and carbon numbers are **model estimates**; their absolute magnitude is uncalibrated
because we deliberately do **not** require hardware power counters (to stay portable).

**Rebuttal.** The project's headline deliverables are **% Resource Reduction** and the
**A–F grade**, and both are **mathematically invariant to the model coefficients**:
`E = (P_cpu + P_ram)·t` is *linear* in the power constants, so when you take a ratio
between two algorithms the constants cancel. We therefore make only the claim we can
defend — *"algorithm X is Y % greener than Z, and scales in class C"* — and never *"X
consumes exactly J joules."* For absolute Joules the same Layer-2 interface accepts a
hardware backend (Intel **RAPL** / NVIDIA **NVML**) as documented future work; the
architecture already isolates that concern in `EnergyModelConfig`. This is a *scoping*
decision, not a *flaw* — and scoping honesty is what a research panel rewards.

### Loophole 3 — "How is this different from Chrome DevTools / Valgrind / `time`?"

| Tool | What it does | What it *doesn't* do |
|------|--------------|----------------------|
| Chrome DevTools | JS heap/CPU in a browser | not general processes; **no carbon; no grade** |
| Valgrind / Massif | exact heap profiling, Linux | 10–50× slowdown; **Linux-only; no energy/carbon; no scaling grade** |
| `/usr/bin/time` | coarse wall/RSS | single run; **no statistics, energy, or verdict** |
| **This tool** | time + mem + **energy + carbon + A–F scaling grade**, cross-platform, dual-granularity | not a replacement for deep memory debugging |

**Rebuttal.** We are not competing on raw profiling depth — we *validate against* those
tools (Part 2a). Our contribution is the **Layer-2 translation** none of them perform:
turning raw performance into an **SCI-aligned carbon estimate** and a **complexity-scaling
sustainability grade**, delivered **cross-platform** (Windows/macOS/Linux/Raspberry Pi)
and at **two granularities** (whole-process *and* single-algorithm). It is a *green
diagnostics layer* — a category the incumbents don't occupy.

### Loophole 4 — "What is the real-world utility and at what scale is it feasible?"

**Concrete use cases:**
- **CI/CD sustainability gate.** `perf_bench_server.py` exposes a REST API; a pipeline can
  fail a build when the grade drops (e.g. an O(n)→O(n²) regression) or energy regresses > X %.
- **Pre-deployment algorithm selection & education.** Make Big-O *tangible*: the dashboard
  shows a student that Bubble→Quick is a 99.68 % energy cut at N = 10,000 (Part 3.1).
- **Edge / green-datacenter budgeting.** `perf_bench.py` is explicitly built for headless
  Linux / Raspberry Pi 5, where the % savings translate directly into battery/thermal budget.

**Honest scale limits.** The *algorithm* harness runs Python-level workloads (GIL,
interpreter overhead), so it is a **comparative** instrument, not an absolute FLOP counter.
But the *process* profiler is **language-agnostic** — it profiles any child process,
including the compiled C++ target (`main.cpp`) — so the system already spans "5-line
function" to "production service." At datacenter scale the relative savings **compound**,
and we can now put measured numbers on it. The Bubble → Quick substitution at N = 10,000
saves **8.418 J / 0.001169 gCO₂e per invocation** — a **312× speed-up**, 99.68 % less
energy (Part 3.1):

| Horizon | Energy saved | Carbon saved |
|---|---:|---:|
| per invocation | 8.418 J | 0.001169 gCO₂e |
| per day @ 10⁶ calls | 8.42 MJ = **2.34 kWh** | 1.17 kg CO₂e |
| **per year** | **854 kWh** | **≈ 0.43 tonne CO₂e** |

Roughly four months of an average Thai household's electricity, or ~427 kg of CO₂e, **saved
by a single algorithm choice** (cross-checked two ways: via the per-invocation carbon delta
and via 854 kWh × 500 gCO₂e/kWh — both give 427 kg). Computed with this machine's calibrated
coefficients (15 W ÷ 10 cores) and the Thailand TGO grid factor. That compounding is the
entire economic argument for green software, and this tool is the instrument that
*quantifies and grades* it.

---

## PART 5 — Code Enhancements & Utility Updates

**Finding: the three requested enhancements already exist in the current code.** Rather
than bolt on duplicates, here is where each lives, verbatim, plus the one real fix applied
this session.

### 5.1 Baseline noise-subtraction logic — *present* (`algo_lab.py`, `measure_time_cell`)

```python
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
net_ms = max(0.0, (work_dt - null_dt) / reps) / 1e6   # Net = Measured - Baseline
```

Memory baseline is handled *by construction*: `tracemalloc` counts only post-start
allocations, excluding the interpreter footprint (`measure_mem_cell`).

### 5.2 Wrapper functions to run the 3 case studies — *present* (`algo_lab.py`)

`build_workloads()` bundles all six algorithms with input factories, and the CLI runs the
whole suite. The exact command that produced Part 3:

```bash
python algo_lab.py --budget 8 --out results/case_studies_full.json --md results/case_studies_full_tables.md
```

`--quick` runs a fast smoke version; `--grid 500` switches to the Thailand TGO grid factor;
`--tdp` / `--cores` recalibrate the energy model for a different machine.

### 5.3 Carbon/Energy formula alongside RAM/Latency — *present* (`green_metrics.estimate_energy`)

```python
per_core   = cfg.per_core_dynamic_w()                       # TDP / cores
load_shape = cfg.cpu_static_frac + (1 - cfg.cpu_static_frac) * min(active_cores, 1.0)
p_cpu      = per_core * load_shape * max(active_cores, 1e-6)
p_ram      = cfg.ram_watts_per_gb * mem_gb
e_joules   = (p_cpu + p_ram) * elapsed_s                     # Energy  [J]
carbon     = (e_joules / 3.6e6) * cfg.grid_intensity_g_per_kwh + cfg.embodied_carbon_g
```

Each benchmark cell already emits `time_mean_ms`, `mem_mean_bytes`, `energy_j`, `carbon_g`
and `power_w` side by side (see `CellResult`).

### 5.4 Fix applied this session — Windows non-ASCII path crash

**Symptom.** The suite finished all measurements and *wrote both output files*, then
crashed on the final status line with `UnicodeEncodeError: 'charmap' codec can't encode…`
— because the resolved path contains Thai characters (`…\เอกสาร\…`) and the legacy Windows
console code page (cp1252) cannot render them.

**Fix** (top of `main()` in `algo_lab.py`) — force the console streams to UTF-8 so a
successful run never ends on a traceback:

```python
def main() -> None:
    # Windows consoles default to a legacy code page (cp1252) that cannot
    # encode non-ASCII characters in the resolved output path (e.g. a Thai
    # directory name). Force UTF-8 so the final status prints never abort an
    # otherwise-successful run.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = _build_parser().parse_args()
```

### 5.5 Suggested next enhancements (optional, not yet implemented — flagged honestly)

- **Absolute-energy calibration hook:** an `EnergyBackend` that reads Intel RAPL
  (`/sys/class/powercap/…`) or NVML when available, falling back to the analytic model.
  This is what would let you claim absolute Joules, and it slots into `EnergyModelConfig`.
- **Valgrind/`time` cross-check harness:** a small Linux script that runs the workloads
  under `/usr/bin/time -v` and Massif and emits the regression/R² of Part 2a — the one
  outstanding validation experiment.
- **Repo hygiene:** move the six legacy files (Part 0) into an `/attic` folder.

---

## Appendix A — Measurement provenance

- **Machine:** ASUS Vivobook X1605ZA · Intel i5-1235U (Alder Lake-P, 2 P + 8 E = 10 physical / 12 logical) · 15.69 GB RAM · Windows 11 Home Single Language · Python 3.14.5 (`.venv`), `psutil` 7.2.2, `numpy` 2.4.4, `matplotlib` 3.11.1.
- **Energy model (calibrated to this machine):** TDP **15 W** / **10 cores**, static fraction 0.30, RAM 0.392 W/GB, grid **500 gCO₂e/kWh (Thailand TGO)**.
- **Statistics:** ≤ 30 trials/cell, z-score outlier filter |z| > 2.5, warm-up discarded, `gc` disabled during timing, per-cell RNG seeding.
- **Reproduce:** `python algo_lab.py --budget 8 --cores 10 --tdp 15 --grid 500` (full) or add `--quick` (fast). Raw JSON is written to `results/`.
- **Validation status:** external kernel-reference calibration **RUN AND PASSED** (R² = 1.0000, Appendix E); Layer-3 dashboard **verified** (Appendix B); coefficient invariance **proven** across three coefficient sets (Appendix C).
- **Still not done (stated honestly):** absolute-energy calibration against hardware counters (Intel RAPL / NVML); execution on Linux / Raspberry Pi — the code paths exist and are platform-guarded but are **untested**, so no claim is made for them.
- **Archived provenance:** the earlier 8-core / 28 W / 475 g run is preserved at `results/case_studies_8core_28w_475grid.json` for the reproducibility comparison in §3.4.

---

## Appendix B — Layer 3 (Dashboard) verification log

Run on the author's machine; the dashboard was started with
`python perf_bench_server.py --host 127.0.0.1 --port 5055`.

| Check | Result |
|---|---|
| `GET /` (dashboard HTML) | **PASS** — renders, title "AI-PerfBench v3.1 — Green AI Dashboard" |
| `GET /api/health` | **PASS** — `200 {"status":"ok"}` |
| `GET /api/info` | **PASS** — live system snapshot (see below) |
| `POST /api/runs` → poll → result | **PASS** — 4 trials, cold-start dropped, 3 steady-state, aggregate computed |
| Result persisted to `results/` | **PASS** — `results/dashboard_smoke.json` written |

**Live system snapshot returned by `/api/info`:** `LAPTOP-OL3D69CT` · Windows 11 · AMD64 ·
**12 logical / 10 physical cores** · 1300 MHz · 15.69 GB RAM · Python 3.14.5 · SciPy available.

### B.1 Defect found and fixed during verification — silent hung runs

**Symptom.** A benchmark submitted with a *relative* interpreter path
(`.venv/Scripts/python.exe`) stayed pinned at `status="running"` with
`completed_trials: 0` **forever**. No error was ever returned to the client and nothing
appeared in the server log.

**Root cause.** `perf_bench.py` is dual-purpose (CLI + library). On a fatal trial error —
including "executable not found", which is what a relative path produces when the server's
working directory differs — it calls `sys.exit()`. In the server this executes inside a
**worker thread**, where `sys.exit()` raises `SystemExit`. `SystemExit` derives from
`BaseException`, **not** from `Exception`, so the worker's `except Exception` handler did
not catch it: the thread died silently and the run record was never updated.

**Fix** (`perf_bench_server.py`, `_run_benchmark_thread`): catch `BaseException` instead of
`Exception`, so any terminated worker is always reported.

**Verified after fix:** the same deliberately-invalid command now fails cleanly in ~2 s with
`status: "error"` and the message
`SystemExit: [FATAL] Executable not found on PATH: 'this_command_does_not_exist_xyz.exe'`.

> **Operational note for the demo:** always give the dashboard an **absolute** path to the
> target interpreter/binary. With the fix, a wrong path now reports an error instead of
> hanging — but an absolute path avoids the situation entirely.

---

## Appendix C — Coefficient-invariance experiment (Loophole 2, proven not just argued)

The project's central defensive claim is that **the headline deliverables do not depend on
the energy coefficients**. Rather than argue it, we measured it.

Two discrepancies surfaced during verification and were turned into the experiment:
`/api/info` reported **10 physical cores** where the model defaulted to 8, and the CPU was
identified as an **i5-1235U whose base power is 15 W**, not the generic 28 W default. So we
recomputed energy from the **same measured timings and memory** under three different
coefficient sets:

| Pair @ N | Coefficient set | Energy slow | Energy fast | **% Reduction** |
|---|---|---:|---:|---:|
| Bubble→Quick @ N=10,000 | A: 8 cores / 28 W / 475 g | 19.70 J | 0.06314 J | **99.6795 %** |
| | B: 10 cores / 28 W / 475 g | 15.76 J | 0.05052 J | **99.6795 %** |
| | **C: 10 cores / 15 W / 500 g (FINAL)** | 8.445 J | 0.02706 J | **99.6795 %** |
| Linear→Binary @ N=10,000 | A: 8 cores / 28 W / 475 g | 0.001887 J | 1.092e-05 J | **99.4210 %** |
| | B: 10 cores / 28 W / 475 g | 0.001509 J | 8.739e-06 J | **99.4210 %** |
| | **C: 10 cores / 15 W / 500 g (FINAL)** | 0.0008087 J | 4.682e-06 J | **99.4210 %** |
| Naive→DP Fib @ N=35 | A: 8 cores / 28 W / 475 g | 4.529 J | 7.615e-06 J | **99.9998 %** |
| | B: 10 cores / 28 W / 475 g | 3.623 J | 6.092e-06 J | **99.9998 %** |
| | **C: 10 cores / 15 W / 500 g (FINAL)** | 1.941 J | 3.264e-06 J | **99.9998 %** |

**Result.** Absolute Joules moved by more than a factor of two across the three
configurations (19.70 J → 8.445 J for the same measured Bubble Sort run). The
**% Resource Reduction was identical to four decimal places in every case**, and **every
A–F grade was unchanged** — grades derive from the time/memory scaling exponent, which
never touches an energy coefficient at all.

**Why this matters for the defence.** This converts Loophole 2 from an argument into a
*measured result*. A reviewer who rejects our power constants and substitutes their own
changes only the absolute magnitude — never the ranking, the percentage, or the grade. The
two deliverables the project actually reports are provably coefficient-independent.

**Presentation rule:** absolute Joules are configuration-dependent, so **quote one
configuration throughout the report and state it explicitly**. All figures in this document
use configuration **C** — the one calibrated to the actual machine and the Thai grid:

```bash
python algo_lab.py --budget 8 --cores 10 --tdp 15 --grid 500
```

---

## Appendix D — Figures

Generated by `make_figures.py` from `results/case_studies_full.json` only — every point is
measured. Each figure ships as **PNG (300 dpi** for the report/slides**), PDF and SVG**
(vector — use these for the poster so they stay sharp at A0).

| File | What it shows | Best used in |
|---|---|---|
| `results/figures/fig1_scaling_loglog.*` | Measured time vs N with ±SD error bars, one panel per case study; `k` and grade in each legend. Panels 1–2 log–log (straight line = power law); panel 3 semi-log on linear N (straight line = exponential). | Report ch. 4, poster centre |
| `results/figures/fig2_bigO_validation.*` | Measured `k` vs theoretical ideal `k` for each algorithm, plotted over the A–F grade bands. **The calibration money-shot.** | Report ch. 4, slide 3 |
| `results/figures/fig3_headline_savings.*` | Speed-up factor and Joules saved per invocation at matched N (log scale). | Slide 4, poster headline |

Colour palette is CVD-validated (worst-pair ΔE 24.7 under protanopia simulation, against a
target of ≥ 8), so the figures stay readable for colour-blind reviewers and in greyscale
print. Series identity is additionally carried by the legend text, never by colour alone.

---

## Appendix E — External-Reference Calibration (Part 2a, EXECUTED)

**The problem.** Valgrind is Linux-only and no Linux machine was available. **The
solution:** do not calibrate against another *sampling profiler* — that only compares two
estimates. Calibrate against the **operating system's own peak-memory accounting**, the
ground truth the kernel maintains for every process it schedules. That reference exists on
every OS, so the experiment needs no Linux box and no Raspberry Pi:

| OS | Kernel-truth reference | How it is read |
|---|---|---|
| Windows | `PROCESS_MEMORY_COUNTERS.PeakWorkingSetSize` | `GetProcessMemoryInfo()` (Win32/psapi) via `ctypes` |
| macOS | *maximum resident set size* | `/usr/bin/time -l` (parses `rusage.ru_maxrss`, **bytes**) |
| Linux | *Maximum resident set size* | `/usr/bin/time -v` (`rusage.ru_maxrss`, **kB**) |

**Method.** Five workloads allocating 25 → 400 MB (every page touched so the memory is
genuinely *resident*, not merely reserved). Each is measured twice: once by our sampling
profiler (`perf_bench.py`, ~1 ms interval) and once by the kernel reference (median of 3
repeats). Our sampled peak is then regressed on the kernel's true peak.

**Result — Windows 11, Intel i5-1235U, Python 3.14.5:**

| Workload (MB) | Kernel peak (MB) | Our net peak (MB) | Ratio |
|---:|---:|---:|---:|
| 25 | 35.7 | 37.8 | 1.057 |
| 50 | 60.7 | 62.6 | 1.032 |
| 100 | 110.7 | 112.5 | 1.016 |
| 200 | 210.7 | 212.1 | 1.006 |
| 400 | 410.7 | 411.9 | 1.003 |

> **ours = 0.9978 × kernel + 2.04 MB  ·  R² = 1.0000  ·  Pearson r = 1.0000  → PASS**
> (acceptance criteria: R² ≥ 0.95 and slope within 0.8–1.2)

**Interpretation — three separate claims, all defensible:**
1. **Scale is correct.** Slope 0.9978 ≈ 1.0: our measurement is not systematically
   inflated or deflated. It is not merely *correlated* with the truth — it *equals* it.
2. **The offset is constant, small, and explained.** The +2.04 MB intercept is the fixed
   observer/interpreter overhead; because it is *constant* it cancels completely in every
   comparison and in the baseline-subtraction model (Part 2c).
3. **The ~1 ms sampling rate is sufficient.** A sampler that missed the high-water mark
   would under-report, most visibly on the fastest-allocating workloads. It does not.

**Why this beats a Valgrind correlation.** Valgrind is another *instrument*, so agreeing
with it proves only that two instruments agree. `PeakWorkingSetSize` and `ru_maxrss` are
the numbers the OS itself keeps — the ground truth. Validating against them is the
stronger experiment, and it is portable to every platform in this study.

**Reproduce:**

```bash
python calibrate_external.py
```

### E.1 Defect found *by* this experiment — process-tree blindness

The first calibration run failed spectacularly: a 400 MB workload was reported as **4.0 MB**
by our profiler *and* **4.4 MB** by the kernel reference. Investigation showed the cause was
not the measurement but *what was being measured*:

> On Windows, a virtualenv's `Scripts\python.exe` is a ~4 MB **redirector shim** that
> re-executes the real interpreter as a **child process**. Both instruments were faithfully
> measuring the shim. The 400 MB lived in a grandchild.

Measured directly: **direct child 4.0 MB vs whole process tree 414.8 MB** — a ~100× error.

**Why this matters far beyond the venv case.** Any target that does its work in a
subprocess is affected: shell/`npm`/`uv` wrappers, `multiprocessing` pools, ML dataloader
workers, and most training scripts. A profiler that measures only the process it spawned
silently under-reports all of them.

**Fix** (`perf_bench.py`, `_read_sample`): RSS, VMS and CPU% are now **summed over the whole
process tree** (`children(recursive=True)`), with descendants that exit mid-enumeration
skipped safely. This is the correct accounting for *"what did this command cost the
machine?"*. After the fix the calibration returns R² = 1.0000.

**Presenting this is a strength, not an embarrassment:** the calibration experiment did
exactly what a calibration experiment is *for* — it caught a real defect that ordinary
testing had missed. That is the argument for doing the experiment at all.

---

## Appendix F — Running the tool on each platform

### F.1 Windows (ASUS Vivobook X1605ZA — the development machine)

Hardware confirmed via `/api/info`: Intel **i5-1235U** (Alder Lake-P, 2 P-cores + 8
E-cores = **10 physical / 12 logical**), 15.69 GB RAM, Windows 11 Home SL, Python 3.14.5.

```powershell
cd "$HOME\OneDrive\เอกสาร\MyCoding"
.\.venv\Scripts\Activate.ps1
```

Then, in order — the four things worth demonstrating:

```powershell
python green_metrics.py
```

```powershell
python algo_lab.py --quick --cores 10 --tdp 15 --grid 500
```

```powershell
python calibrate_external.py
```

```powershell
python perf_bench_server.py --host 127.0.0.1 --port 5055
```

**Recommended coefficients for this machine:** `--cores 10` (measured) and **`--tdp 15`**.
The i5-1235U's Processor Base Power is **15 W** (Max Turbo Power 55 W); the 28 W default is
a generic U/P-series figure. Use 15 W and say so — a reviewer can verify it on Intel ARK.

> **Hybrid-architecture caveat, stated openly.** Alder Lake mixes 2 performance cores with
> 8 efficiency cores, so `TDP ÷ physical_cores` charges every core the same share when in
> reality a P-core draws several times an E-core. Our single-threaded workloads are
> scheduled on a P-core, so the *true* per-core power is higher than the 1.5 W this yields.
> This affects absolute Joules only — never the % reduction or the grade (Appendix C).
> Naming this limitation before a judge does is worth more than hoping it goes unnoticed.

### F.2 macOS (MacBook Air 13, macOS 15.4.1 — the cross-platform validation machine)

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install psutil flask matplotlib
```

```bash
python algo_lab.py --quick --cores 8 --tdp 20 --grid 500
```

```bash
python calibrate_external.py
```

Apple Silicon notes: `perf_bench.py` already detects it (`is_apple_silicon` in
`/api/info`). Set `--cores` to the machine's **performance + efficiency core count**
(M1/M2 Air = 8, M3 = 8) and `--tdp` to roughly **20 W** for the package. `/usr/bin/time -l`
is built in, so the Appendix E calibration runs unmodified — no Homebrew, no Valgrind.

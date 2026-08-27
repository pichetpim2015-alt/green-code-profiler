"""
Publication figures for the Green Software Diagnostics PoC.
Built ONLY from measured data in results/case_studies_full.json.
Outputs 300-dpi PNG (report/slides) + PDF & SVG (vector, for poster printing).
"""
import json, math, pathlib, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import LogLocator, NullFormatter

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "results" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

def _load_case_studies() -> dict:
    """
    Locate the dataset written by algo_lab.py.

    algo_lab.py defaults to results/case_studies.json; earlier runs used
    results/case_studies_full.json. Accept either, newest first, and allow an
    explicit path as argv[1].
    """
    if len(sys.argv) > 1:
        candidates = [pathlib.Path(sys.argv[1])]
    else:
        candidates = [ROOT / "results" / name for name in
                      ("case_studies_full.json", "case_studies.json")]

    for path in candidates:
        if path.is_file():
            print(f"  reading {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
            return json.loads(path.read_text(encoding="utf-8"))

    raise SystemExit(
        "No case-study dataset found. Looked for:\n"
        + "".join(f"    {p}\n" for p in candidates)
        + "\nRun the benchmark first:\n"
          "    python algo_lab.py --cores 10 --tdp 15 --grid 500\n"
          "(a --quick run also works, but covers fewer input sizes)"
    )


data = _load_case_studies()
cells = data["cells"]
scaling = data["scaling"]

# ---- validated palette (dataviz reference instance, light mode) -------------
INEFF   = "#eb6834"   # categorical slot 2 (orange)  - the wasteful algorithm
EFF     = "#2a78d6"   # categorical slot 1 (blue)    - the efficient algorithm
INK     = "#0b0b0b"
INK2    = "#52514e"
GRID    = "#d8d7d2"
SURFACE = "#fcfcfb"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans", "font.size": 9,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "axes.titlecolor": INK,
    "xtick.color": INK2, "ytick.color": INK2,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "grid.alpha": 0.9, "axes.axisbelow": True,
    "legend.frameon": False,
})

def series(label, nmax=None):
    cs = sorted([c for c in cells if c["label"] == label], key=lambda c: c["n"])
    if nmax is not None:
        cs = [c for c in cs if c["n"] <= nmax]
    return [c["n"] for c in cs], cs

def save(fig, name):
    for ext, kw in (("png", {"dpi": 300}), ("pdf", {}), ("svg", {})):
        fig.savefig(OUT / f"{name}.{ext}", bbox_inches="tight", **kw)
    plt.close(fig)
    print("  wrote", name, "(png/pdf/svg)")


# ===========================================================================
# FIGURE 1 - Scaling curves, log-log, small multiples (one panel per case)
# ===========================================================================
CASES = [
    ("Case Study 1: Sorting",   "Bubble Sort",               "Quick Sort",                    None),
    ("Case Study 2: Searching", "Linear Search",             "Binary Search",                 None),
    ("Case Study 3: Fibonacci", "Naive Recursive Fibonacci", "Dynamic-Programming Fibonacci", 35),
]

fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.3))
for pi, (ax, (title, slow, fast, nmax)) in enumerate(zip(axes, CASES)):
    is_exp_panel = pi == 2                      # Fibonacci: linear N, exponential growth
    for lbl, color, short in ((slow, INEFF, slow.replace(" Fibonacci", "")),
                              (fast, EFF,  fast.replace(" Fibonacci", ""))):
        ns, cs = series(lbl, nmax)
        ts = [c["time_mean_ms"] for c in cs]
        sd = [c["time_std_ms"] for c in cs]
        k = scaling[lbl]["time_grade"]["exponent_k"]
        g = scaling[lbl]["time_grade"]["grade"]
        # k and grade live in the legend -> no annotation collisions at panel edges
        ax.errorbar(ns, ts, yerr=sd, color=color, lw=2.0, marker="o", ms=6,
                    capsize=3, elinewidth=1.0, mec=SURFACE, mew=1.2, zorder=3,
                    label=f"{short}   k={k:.2f} · grade {g}")
    ax.set_yscale("log")
    if is_exp_panel:
        # Exponential data on LINEAR N: a straight line here means exponential growth.
        ax.set_xticks([20, 25, 30, 35])
        ax.set_xlabel("Input size N  (linear)")
    else:
        ax.set_xscale("log")
        ax.set_xticks([100, 1000, 10000])
        ax.set_xticklabels(["100", "1,000", "10,000"])
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xlabel("Input size N  (log)")
    ax.set_title(title, fontsize=10.5, fontweight="bold", pad=8)
    ax.legend(loc="upper left", fontsize=8.4)
axes[0].set_ylabel("Measured time (ms, log scale)   ±SD")
fig.suptitle("Measured scaling on real hardware.  Panels 1–2 are log–log: a straight line is a power law of slope k.  "
             "Panel 3 is semi-log on linear N: a straight line there is exponential growth.",
             fontsize=9.5, color=INK2, y=1.05)
save(fig, "fig1_scaling_loglog")


# ===========================================================================
# FIGURE 2 - Measured exponent k vs. theoretical ideal, over the grade bands
# ===========================================================================
IDEAL = {
    "Bubble Sort": 2.00, "Quick Sort": 1.15, "Linear Search": 1.00,
    "Binary Search": 0.15, "Dynamic-Programming Fibonacci": 1.00,
}
BANDS = [("A", -0.05, 0.30), ("B", 0.30, 1.10), ("C", 1.10, 1.60),
         ("D", 1.60, 2.40), ("F", 2.40, 2.75)]
BAND_FILL = ["#eef2f6", "#e6ebf1", "#eef2f6", "#e6ebf1", "#eef2f6"]

rows = [(lbl, IDEAL[lbl], scaling[lbl]["time_grade"]["exponent_k"],
         scaling[lbl]["time_grade"]["grade"])
        for lbl in ["Bubble Sort", "Quick Sort", "Linear Search",
                    "Dynamic-Programming Fibonacci", "Binary Search"]]

fig, ax = plt.subplots(figsize=(9.6, 4.3))
for (g, lo, hi), fillc in zip(BANDS, BAND_FILL):
    ax.axvspan(lo, hi, color=fillc, zorder=0)
    ax.text((lo + hi) / 2, len(rows) - 0.32, g, ha="center", va="bottom",
            fontsize=11, fontweight="bold", color="#8a8a85", zorder=1)

for i, (lbl, ideal, meas, grade) in enumerate(rows):
    y = len(rows) - 1 - i
    ax.plot([ideal, meas], [y, y], color=GRID, lw=2.0, zorder=2)
    ax.plot(ideal, y, "o", ms=9, mfc=SURFACE, mec=INK2, mew=1.8, zorder=3,
            label="Theoretical ideal k" if i == 0 else None)
    ax.plot(meas, y, "o", ms=10, color=EFF, mec=SURFACE, mew=1.4, zorder=4,
            label="Measured k (this tool)" if i == 0 else None)
    ax.annotate(f"{meas:.2f}", xy=(meas, y), xytext=(0, 11),
                textcoords="offset points", ha="center", fontsize=9,
                fontweight="bold", color=EFF)
    # Grade label sits OUTSIDE the right spine (axes-fraction x) so it never
    # lands inside a band it does not belong to.
    ax.annotate(f"grade {grade}", xy=(1.02, y), xycoords=("axes fraction", "data"),
                va="center", fontsize=9.5, fontweight="bold", color=INK,
                clip_on=False)

ax.set_yticks(range(len(rows)))
ax.set_yticklabels([r[0].replace("Dynamic-Programming", "DP") for r in rows][::-1],
                   fontsize=9.5, color=INK)
ax.set_xlim(-0.05, 2.75); ax.set_ylim(-0.6, len(rows) - 0.05)
ax.set_xlabel("Complexity-scaling exponent  k   (fitted from measured timings)")
ax.grid(axis="y", visible=False)
# Anchored in the empty mid-left region -> no collision with dots or grade labels.
ax.legend(loc="center left", bbox_to_anchor=(0.015, 0.60), fontsize=9)
ax.set_title("Calibration: the profiler recovers theoretical complexity from noisy live timings",
             fontsize=11, fontweight="bold", pad=26, loc="left")
_nf = scaling["Naive Recursive Fibonacci"]["time_grade"]      # read from data, never hard-code
ax.text(0, 1.045,
        f"Naive Recursive Fibonacci is off-scale (k = {_nf['exponent_k']:.2f}, "
        f"correctly flagged exponential -> grade {_nf['grade']})",
        transform=ax.transAxes, fontsize=8.5, color=INK2)
save(fig, "fig2_bigO_validation")


# ===========================================================================
# FIGURE 3 - Headline result: speed-up and energy saved at matched N
# ===========================================================================
PAIRS = [("Bubble → Quick", "Bubble Sort", "Quick Sort", [100, 1000, 10000]),
         ("Linear → Binary", "Linear Search", "Binary Search", [100, 1000, 10000]),
         ("Naive → DP Fib", "Naive Recursive Fibonacci",
          "Dynamic-Programming Fibonacci", [20, 25, 30, 35])]

def short_n(n):
    return {1000: "1k", 10000: "10k"}.get(n, f"{n:,}")

def cell_at(label, n):
    """The measured cell for (label, n), or None if this run did not cover it."""
    return next((c for c in cells if c["label"] == label and c["n"] == n), None)


fig, (axL, axR) = plt.subplots(1, 2, figsize=(13.4, 4.8))
xpos, xlab, speed, energy, groups = [], [], [], [], []
skipped = []
cur = 0
for gi, (title, slow, fast, sizes) in enumerate(PAIRS):
    start = cur
    for n in sizes:
        cs, cf = cell_at(slow, n), cell_at(fast, n)
        if cs is None or cf is None:        # e.g. a --quick run used other sizes
            skipped.append(f"{title} @ N={n:,}")
            continue
        xpos.append(cur); xlab.append(short_n(n))
        speed.append(cs["time_mean_ms"] / cf["time_mean_ms"])
        energy.append(cs["energy_j"] - cf["energy_j"])
        cur += 1
    if cur > start:                         # only label groups that produced bars
        groups.append((title, start, cur - 1))
        cur += 2.1                  # wide gutter so group labels never collide

if skipped:
    print(f"  note: {len(skipped)} pair(s) not in this dataset, omitted from fig3 "
          f"({skipped[0]}{', …' if len(skipped) > 1 else ''})")
if not xpos:
    plt.close(fig)
    raise SystemExit(
        "  fig3 needs matched baseline/optimised pairs at the same N, and this dataset\n"
        "  has none. It was probably produced by `algo_lab.py --quick`.\n"
        "  Re-run the full benchmark, then regenerate:\n"
        "      python algo_lab.py --cores 10 --tdp 15 --grid 500\n"
        "      python make_figures.py\n"
        "  (figures 1 and 2 above were written successfully)"
    )

for ax, vals, ylabel, titletxt, fmt in (
    (axL, speed,  "Speed-up factor  (×, log scale)",
     "How much faster the efficient algorithm is", lambda v: f"{v:,.0f}×"),
    (axR, energy, "Energy saved per invocation (J, log scale)",
     "Energy saved by the substitution", lambda v: f"{v:.3g}"),
):
    ax.bar(xpos, vals, width=0.72, color=EFF, zorder=3)
    ax.set_yscale("log")
    ax.set_xticks(xpos); ax.set_xticklabels(xlab, fontsize=8.5)
    ax.set_ylabel(ylabel)
    ax.set_title(titletxt, fontsize=10.5, fontweight="bold", pad=10, loc="left")
    ax.grid(axis="x", visible=False)
    for x, v in zip(xpos, vals):
        ax.annotate(fmt(v), xy=(x, v), xytext=(0, 4), textcoords="offset points",
                    ha="center", fontsize=8, color=INK)
    ax.set_ylim(top=max(vals) * 8)
    ax.set_xlim(-0.8, cur - 1.2)
    # Group labels BELOW the tick row (standard grouped-categorical axis)
    for title, a, b in groups:
        ax.annotate("", xy=(a - 0.42, -0.115), xytext=(b + 0.42, -0.115),
                    xycoords=("data", "axes fraction"),
                    textcoords=("data", "axes fraction"),
                    arrowprops=dict(arrowstyle="-", color=GRID, lw=1.4))
        ax.annotate(title, xy=((a + b) / 2, -0.20),
                    xycoords=("data", "axes fraction"), ha="center", va="top",
                    fontsize=8.8, color=INK2, fontweight="bold", clip_on=False)
    ax.annotate("Input size N", xy=(0.5, -0.30), xycoords="axes fraction",
                ha="center", va="top", fontsize=9, color=INK2)
save(fig, "fig3_headline_savings")

print("\nAll figures ->", OUT)

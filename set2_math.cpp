// ============================================================================
//  GREEN CODE PROFILER - BENCHMARK SET 2 : MATHEMATICAL CALCULATION
//
//  Two independent experiments, so each one isolates a single variable:
//
//    2a  AGGREGATE STATISTICS   sum / sum-of-squares / count over 1..N
//        A : counting loop, O(N)          B : closed-form formula, O(1)
//
//    2b  PER-SAMPLE SCALING     scale every ADC reading by a calibration factor
//        A : integer division, O(N)       B : bit shift, O(N)
//        Same complexity, same result - only the CPU instruction differs.
//
//  IMPORTANT (2b): the divisor is read through a volatile so the compiler
//  cannot see that it is a power of two and silently turn the division into a
//  shift for us. On a real device the calibration divisor comes from NVS or a
//  config packet at runtime, so this mirrors reality rather than gaming it.
//
//  Usage:
//    set2_math.exe      -> run both, verify identity, print comparison
//    set2_math.exe A    -> run ONLY variant A  (profile this process)
//    set2_math.exe B    -> run ONLY variant B  (profile this process)
//    <exe> A 4      -> variant A, workload scaled 4x (argv[2], default 1)
// ============================================================================

#include <cstdio>
#include <cstdint>
#include <vector>

#if defined(ESP32) || defined(ARDUINO)
static const uint32_t N_STATS       = 20000;
static const uint32_t STATS_REPEATS = 20;
static const uint32_t SAMPLE_N      = 2048;
static const uint32_t SCALE_REPEATS = 20;
#else
static const uint32_t N_STATS       = 1000000;  // 2a: range 1..N
static const uint32_t STATS_REPEATS = 200;
static const uint32_t SAMPLE_N      = 65536;    // 2b: sample buffer
static const uint32_t SCALE_REPEATS = 600;
#endif

// Repeat-count multiplier, set from argv[2] (default 1). Stretch the run until
// it dominates process-startup time in the profiler - see README section 3.
static uint32_t g_scale = 1u;

// The calibration factor, expressed two ways. Both are volatile so the compiler
// must load them at runtime.
//
// SUBTLETY, found by disassembling the binary: it is NOT enough to make only the
// shift volatile and then write "1u << g_cal_shift" in variant A. GCC sees that
// 1<<k is provably a power of two, strength-reduces the division into a shift by
// itself, and both variants end up as byte-identical machine code - the
// experiment then measures nothing at all. The divisor must arrive as an opaque
// runtime value, which is exactly what a calibration constant read from NVS or a
// config packet actually is.
static volatile uint32_t g_cal_divisor = 64;    // variant A divides by this
static volatile uint32_t g_cal_shift   = 6;     // variant B shifts by this (2^6 == 64)

static uint32_t g_lcg = 1u;

static uint32_t lcg_next(void)
{
    g_lcg = g_lcg * 1664525u + 1013904223u;
    return g_lcg;
}

static void fill_samples(uint16_t *dst, uint32_t n, uint32_t seed)
{
    g_lcg = seed;
    for (uint32_t i = 0; i < n; ++i) {
        dst[i] = (uint16_t)(lcg_next() >> 20);  // 0..4095, a 12-bit reading
    }
}

// ----------------------------------------------------------------------------
//  Result bundle. Both variants must fill this identically.
// ----------------------------------------------------------------------------
struct Stats {
    uint64_t sum;         // 1 + 2 + ... + N
    uint64_t sumSq;       // 1^2 + 2^2 + ... + N^2
    uint32_t multiples8;  // how many values in 1..N divide evenly by 8
};

static uint64_t mix_stats(uint64_t acc, const Stats &s)
{
    acc = acc * 1099511628211ULL + s.sum;
    acc = acc * 1099511628211ULL + s.sumSq;
    acc = acc * 1099511628211ULL + (uint64_t)s.multiples8;
    return acc;
}

// ============================================================================
//  2a - CODE A : COUNTING LOOP  (Grade F)
// ============================================================================
//  Touches every integer from 1 to N to compute values that mathematics can
//  give us instantly. At N = 1,000,000 this is a million iterations to produce
//  three numbers.
// ----------------------------------------------------------------------------
static Stats stats_naive(uint32_t N)
{
    Stats s;
    s.sum        = 0;
    s.sumSq      = 0;
    s.multiples8 = 0;

    for (uint32_t i = 1; i <= N; ++i) {
        s.sum   += i;
        s.sumSq += (uint64_t)i * (uint64_t)i;
        if (i % 8 == 0) {
            s.multiples8++;
        }
    }
    return s;
}

// ============================================================================
//  2a - CODE B : CLOSED-FORM FORMULA  (Grade A)
// ============================================================================
//  Gauss's formula and the standard sum-of-squares identity. Constant time, and
//  exact: n(n+1) is always even and n(n+1)(2n+1) is always divisible by 6, so
//  the integer divisions below never lose a remainder.
// ----------------------------------------------------------------------------
static Stats stats_formula(uint32_t N)
{
    const uint64_t n = (uint64_t)N;

    Stats s;
    s.sum        = n * (n + 1ULL) / 2ULL;
    s.sumSq      = n * (n + 1ULL) * (2ULL * n + 1ULL) / 6ULL;
    s.multiples8 = (uint32_t)(n >> 3);   // floor(n / 8)
    return s;
}

// ============================================================================
//  2b - CODE A : HARDWARE DIVISION  (Grade F)
// ============================================================================
//  Integer division is one of the slowest instructions a CPU has: roughly
//  20-40 cycles on x86 and NOT pipelined. Some MCU cores have no divide
//  instruction at all and call a software routine costing hundreds of cycles.
// ----------------------------------------------------------------------------
static uint64_t scale_with_division(const uint16_t *raw, uint32_t n, uint32_t divisor)
{
    uint64_t acc = 0;
    for (uint32_t i = 0; i < n; ++i) {
        acc += ((uint32_t)raw[i] * 1000u) / divisor;
    }
    return acc;
}

// ============================================================================
//  2b - CODE B : BIT SHIFT  (Grade A)
// ============================================================================
//  For UNSIGNED values, dividing by 2^k is exactly a right shift by k - one
//  cycle, fully pipelined.
//
//  CAVEAT worth stating in the report: this identity holds for unsigned and
//  non-negative values only. For a negative signed value, "/" truncates toward
//  zero while ">>" rounds toward negative infinity, so -1 / 2 == 0 but
//  -1 >> 1 == -1. That is why the sample buffer here is uint16_t.
// ----------------------------------------------------------------------------
static uint64_t scale_with_shift(const uint16_t *raw, uint32_t n, uint32_t shift)
{
    uint64_t acc = 0;
    for (uint32_t i = 0; i < n; ++i) {
        acc += ((uint32_t)raw[i] * 1000u) >> shift;
    }
    return acc;
}

// ============================================================================
//  WORKLOADS
// ============================================================================
//  N and the buffer offset are varied per repetition. Without that, the
//  compiler would notice it is calling a pure function with identical
//  arguments and hoist the whole thing out of the repeat loop, which would
//  quietly destroy the measurement.
// ----------------------------------------------------------------------------
uint64_t workload_A_math(void)
{
    uint64_t acc = 0;

    for (uint32_t r = 0; r < STATS_REPEATS * g_scale; ++r) {
        acc = mix_stats(acc, stats_naive(N_STATS - (r & 63u)));
    }

    std::vector<uint16_t> raw(SAMPLE_N);
    fill_samples(raw.data(), SAMPLE_N, 987654321u);

    // Opaque to the compiler: it cannot prove this is a power of two, so it has
    // to emit a genuine hardware division. See the note on g_cal_divisor above.
    const uint32_t divisor = g_cal_divisor;
    for (uint32_t r = 0; r < SCALE_REPEATS * g_scale; ++r) {
        const uint32_t off = r & 63u;
        acc = acc * 31u + scale_with_division(raw.data() + off, SAMPLE_N - 64u, divisor);
    }
    return acc;
}

uint64_t workload_B_math(void)
{
    uint64_t acc = 0;

    for (uint32_t r = 0; r < STATS_REPEATS * g_scale; ++r) {
        acc = mix_stats(acc, stats_formula(N_STATS - (r & 63u)));
    }

    std::vector<uint16_t> raw(SAMPLE_N);
    fill_samples(raw.data(), SAMPLE_N, 987654321u);

    const uint32_t shift = g_cal_shift;           // same value, used as a shift
    for (uint32_t r = 0; r < SCALE_REPEATS * g_scale; ++r) {
        const uint32_t off = r & 63u;
        acc = acc * 31u + scale_with_shift(raw.data() + off, SAMPLE_N - 64u, shift);
    }
    return acc;
}

// ============================================================================
//  HARNESS  (define GREEN_BENCH_NO_MAIN to reuse the workloads on an ESP32)
// ============================================================================
#if !defined(GREEN_BENCH_NO_MAIN) && !defined(ARDUINO) && !defined(ESP32)

#include <chrono>
#include <cstring>
#include <cstdlib>

static volatile uint64_t g_sink = 0;

static double run_timed(uint64_t (*fn)(void), uint64_t *out_checksum)
{
    const std::chrono::steady_clock::time_point t0 = std::chrono::steady_clock::now();
    const uint64_t r = fn();
    const std::chrono::steady_clock::time_point t1 = std::chrono::steady_clock::now();

    g_sink = r;
    *out_checksum = r;
    return std::chrono::duration<double, std::milli>(t1 - t0).count();
}

int main(int argc, char **argv)
{
    const char *mode = (argc > 1) ? argv[1] : "both";

    if (argc > 2) {
        const int s = atoi(argv[2]);
        if (s > 0) {
            g_scale = (uint32_t)s;
        }
    }

    printf("GREEN-BENCH set=2 name=math N=%u stats_reps=%u samples=%u scale_reps=%u\n",
           (unsigned)N_STATS, (unsigned)(STATS_REPEATS * g_scale),
           (unsigned)SAMPLE_N, (unsigned)(SCALE_REPEATS * g_scale));

    // The two calibration constants are what make A and B agree. If they ever
    // drift apart the outputs diverge, so fail loudly rather than silently
    // reporting a mismatch that looks like an algorithm bug.
    if (g_cal_divisor != (1u << g_cal_shift)) {
        printf("CONFIG ERROR: g_cal_divisor (%u) != 1 << g_cal_shift (%u)\n",
               (unsigned)g_cal_divisor, (unsigned)(1u << g_cal_shift));
        return 2;
    }

    if (strcmp(mode, "A") == 0 || strcmp(mode, "a") == 0) {
        uint64_t cs = 0;
        const double ms = run_timed(workload_A_math, &cs);
        printf("variant=A label=loop_plus_division\n");
        printf("checksum=0x%016llX\n", (unsigned long long)cs);
        printf("time_ms=%.3f\n", ms);
        return 0;
    }

    if (strcmp(mode, "B") == 0 || strcmp(mode, "b") == 0) {
        uint64_t cs = 0;
        const double ms = run_timed(workload_B_math, &cs);
        printf("variant=B label=formula_plus_shift\n");
        printf("checksum=0x%016llX\n", (unsigned long long)cs);
        printf("time_ms=%.3f\n", ms);
        return 0;
    }

    uint64_t csA = 0, csB = 0;
    const double msA = run_timed(workload_A_math, &csA);
    const double msB = run_timed(workload_B_math, &csB);

    printf("\n  %-28s %12s  %18s\n", "variant", "time_ms", "checksum");
    printf("  %-28s %12.3f  %018llX\n", "A  loop + division",
           msA, (unsigned long long)csA);
    printf("  %-28s %12.3f  %018llX\n", "B  formula + shift",
           msB, (unsigned long long)csB);

    const bool same = (csA == csB);
    printf("\n  OUTPUT IDENTICAL : %s\n", same ? "YES (PASS)" : "NO (FAIL)");
    if (msB > 0.0) {
        printf("  A COSTS          : %.1fx the CPU time of B\n", msA / msB);
    }
    return same ? 0 : 1;
}

#endif // GREEN_BENCH_NO_MAIN

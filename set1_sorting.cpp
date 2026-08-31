// ============================================================================
//  GREEN CODE PROFILER - BENCHMARK SET 1 : DATA SORTING
//  Scenario : sorting a buffer of raw 12-bit ADC readings (median filtering,
//             calibration, outlier rejection - all need a sorted buffer).
//
//  Code A (Grade F) : Bubble Sort   - O(n^2) comparisons
//  Code B (Grade A) : std::sort     - O(n log n) introsort
//
//  Both variants sort the EXACT SAME deterministic input and therefore produce
//  a bit-identical output buffer. Run with no arguments to prove it.
//
//  Usage:
//    set1_sorting.exe        -> run both, verify identity, print comparison
//    set1_sorting.exe A      -> run ONLY variant A  (profile this process)
//    set1_sorting.exe B      -> run ONLY variant B  (profile this process)
//    <exe> A 4      -> variant A, workload scaled 4x (argv[2], default 1)
//
//  Portable: standard C++ only. No WiFi.h, no esp_adc_cal.h, no platform APIs.
// ============================================================================

#include <cstdio>
#include <cstdint>
#include <algorithm>
#include <vector>

// ----------------------------------------------------------------------------
//  WORKLOAD SIZE
//  Tuned so variant A runs long enough (~0.5 s on a PC) for a sampling profiler
//  to collect meaningful power data. ESP32 gets much smaller numbers because it
//  has ~320 KB of RAM and runs at 240 MHz instead of several GHz.
// ----------------------------------------------------------------------------
#if defined(ESP32) || defined(ARDUINO)
static const uint32_t SAMPLE_COUNT = 256;
static const uint32_t REPEATS      = 10;
#else
static const uint32_t SAMPLE_COUNT = 4096;   // one ADC capture buffer
static const uint32_t REPEATS      = 40;     // repeat so the run is measurable
#endif

// Repeat-count multiplier, set from argv[2] (default 1). Stretch the run until
// it dominates process-startup time in the profiler - see README section 3.
static uint32_t g_scale = 1u;

// ----------------------------------------------------------------------------
//  Deterministic pseudo-random sensor data.
//  A fixed-seed LCG guarantees A and B receive byte-identical input, which is
//  what makes "identical output" a fair claim rather than a coincidence.
// ----------------------------------------------------------------------------
static uint32_t g_lcg = 1u;

static void lcg_seed(uint32_t s)
{
    g_lcg = s;
}

static uint32_t lcg_next(void)
{
    // Numerical Recipes constants. Unsigned overflow is well-defined in C++.
    g_lcg = g_lcg * 1664525u + 1013904223u;
    return g_lcg;
}

static void fill_sensor_data(int16_t *dst, uint32_t n, uint32_t seed)
{
    lcg_seed(seed);
    for (uint32_t i = 0; i < n; ++i) {
        dst[i] = (int16_t)(lcg_next() >> 20);   // top 12 bits -> 0..4095
    }
}

// FNV-1a over the buffer. Order-sensitive, so any difference at all in the
// sorted result changes the checksum.
static uint64_t fnv1a_buf(const int16_t *d, uint32_t n)
{
    uint64_t h = 1469598103934665603ULL;
    for (uint32_t i = 0; i < n; ++i) {
        h ^= (uint64_t)(uint16_t)d[i];
        h *= 1099511628211ULL;
    }
    return h;
}

// ============================================================================
//  CODE A - INEFFICIENT  (Grade D/F)
// ============================================================================
//  Textbook bubble sort. For every one of the n passes it re-scans the whole
//  remaining array, comparing and swapping neighbours. Nothing is remembered
//  between passes, so the same elements get compared over and over again.
//
//  The "swapped" early-exit flag is deliberately omitted because that is how
//  the algorithm is usually first written - and on random data it would not
//  help anyway: the complexity stays O(n^2).
// ----------------------------------------------------------------------------
static void bubble_sort(int16_t *a, uint32_t n)
{
    for (uint32_t i = 0; i + 1 < n; ++i) {
        for (uint32_t j = 0; j + 1 < n - i; ++j) {
            if (a[j] > a[j + 1]) {
                int16_t t = a[j];
                a[j]      = a[j + 1];
                a[j + 1]  = t;
            }
        }
    }
}

uint64_t workload_A_sort(void)
{
    std::vector<int16_t> work(SAMPLE_COUNT);
    uint64_t acc = 0;

    for (uint32_t r = 0; r < REPEATS * g_scale; ++r) {
        // Fresh unsorted data every repetition - otherwise repetitions 2..N
        // would sort an already-sorted array and the test would be rigged.
        fill_sensor_data(work.data(), SAMPLE_COUNT, 12345u + r);
        bubble_sort(work.data(), SAMPLE_COUNT);
        acc = acc * 31u + fnv1a_buf(work.data(), SAMPLE_COUNT);
    }
    return acc;
}

// ============================================================================
//  CODE B - OPTIMIZED  (Grade A)
// ============================================================================
//  std::sort is an introsort: quicksort, falling back to heapsort if the
//  recursion gets too deep, plus insertion sort on small partitions. Every
//  comparison it makes actually reduces the remaining disorder.
// ----------------------------------------------------------------------------
uint64_t workload_B_sort(void)
{
    std::vector<int16_t> work(SAMPLE_COUNT);
    uint64_t acc = 0;

    for (uint32_t r = 0; r < REPEATS * g_scale; ++r) {
        // Identical seed sequence as variant A -> identical input.
        fill_sensor_data(work.data(), SAMPLE_COUNT, 12345u + r);
        std::sort(work.begin(), work.end());
        acc = acc * 31u + fnv1a_buf(work.data(), SAMPLE_COUNT);
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

// Stops an aggressive optimizer from deleting a result that is never used.
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

    printf("GREEN-BENCH set=1 name=sorting samples=%u repeats=%u\n",
           (unsigned)SAMPLE_COUNT, (unsigned)(REPEATS * g_scale));

    if (strcmp(mode, "A") == 0 || strcmp(mode, "a") == 0) {
        uint64_t cs = 0;
        const double ms = run_timed(workload_A_sort, &cs);
        printf("variant=A label=Bubble_Sort_O_n2\n");
        printf("checksum=0x%016llX\n", (unsigned long long)cs);
        printf("time_ms=%.3f\n", ms);
        return 0;
    }

    if (strcmp(mode, "B") == 0 || strcmp(mode, "b") == 0) {
        uint64_t cs = 0;
        const double ms = run_timed(workload_B_sort, &cs);
        printf("variant=B label=std_sort_O_n_log_n\n");
        printf("checksum=0x%016llX\n", (unsigned long long)cs);
        printf("time_ms=%.3f\n", ms);
        return 0;
    }

    // ---- both: prove identical output, then report the cost difference ----
    uint64_t csA = 0, csB = 0;
    const double msA = run_timed(workload_A_sort, &csA);
    const double msB = run_timed(workload_B_sort, &csB);

    printf("\n  %-28s %12s  %18s\n", "variant", "time_ms", "checksum");
    printf("  %-28s %12.3f  %018llX\n", "A  Bubble Sort O(n^2)",
           msA, (unsigned long long)csA);
    printf("  %-28s %12.3f  %018llX\n", "B  std::sort O(n log n)",
           msB, (unsigned long long)csB);

    const bool same = (csA == csB);
    printf("\n  OUTPUT IDENTICAL : %s\n", same ? "YES (PASS)" : "NO (FAIL)");
    if (msB > 0.0) {
        printf("  A COSTS          : %.1fx the CPU time of B\n", msA / msB);
    }
    return same ? 0 : 1;
}

#endif // GREEN_BENCH_NO_MAIN

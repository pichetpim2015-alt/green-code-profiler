// ============================================================================
//  GREEN CODE PROFILER - BENCHMARK SET 3 : STRING BUILDING
//  Scenario : assembling a telemetry payload before pushing it over MQTT /
//             HTTP / LoRa. Record format is "S<id4>,<temp4>,<hum4>;"
//             e.g. "S0042,2731,6550;"  (16 characters per record)
//
//  Code A (Grade F) : out = out + rec        - reallocates and copies the whole
//                                              payload on every single record
//  Code B (Grade A) : reserve + append       - one allocation, then pure appends
//
//  Both variants emit a byte-for-byte identical payload string. The "both" mode
//  proves it with a real string comparison, not just a checksum.
//
//  Usage:
//    set3_string.exe      -> run both, verify identity, print comparison
//    set3_string.exe A    -> run ONLY variant A  (profile this process)
//    set3_string.exe B    -> run ONLY variant B  (profile this process)
//    <exe> A 4      -> variant A, workload scaled 4x (argv[2], default 1)
// ============================================================================

#include <cstdio>
#include <cstdint>
#include <string>

#if defined(ESP32) || defined(ARDUINO)
static const uint32_t RECORDS = 300;
static const uint32_t REPEATS = 5;
#else
static const uint32_t RECORDS = 4000;
static const uint32_t REPEATS = 30;
#endif

// Repeat-count multiplier, set from argv[2] (default 1). Stretch the run until
// it dominates process-startup time in the profiler - see README section 3.
static uint32_t g_scale = 1u;

static const uint32_t RECORD_LEN = 16;   // "S0042,2731,6550;"

// ----------------------------------------------------------------------------
//  Deterministic field values, shared by both variants.
//  Ranges are chosen so every field is always exactly 4 digits.
// ----------------------------------------------------------------------------
static uint32_t field_id(uint32_t i)   { return i % 10000u; }              // 0..9999
static uint32_t field_temp(uint32_t i) { return 2000u + (i * 7u)  % 1500u; } // 2000..3499
static uint32_t field_hum(uint32_t i)  { return 3000u + (i * 13u) % 4000u; } // 3000..6999

static uint64_t fnv1a_str(const std::string &s)
{
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < s.size(); ++i) {
        h ^= (uint64_t)(unsigned char)s[i];
        h *= 1099511628211ULL;
    }
    return h;
}

// ============================================================================
//  CODE A - INEFFICIENT  (Grade D/F)
// ============================================================================
//  Three separate wasteful habits, all extremely common in real firmware:
//
//   1. "out = out + rec" builds a brand-new temporary string holding the entire
//      payload so far, copies every byte into it, then throws the old one away.
//      Record 4000 copies ~64 KB just to add 16 bytes. Total work is O(n^2).
//   2. No reserve(), so the buffer is reallocated as it grows.
//   3. Building the id with "0" + idstr prepends, which shifts the whole string
//      each pass instead of writing the digits directly.
// ----------------------------------------------------------------------------
static std::string build_payload_A(uint32_t records)
{
    std::string out;   // no reserve - let it grow the hard way

    for (uint32_t i = 0; i < records; ++i) {
        const uint32_t id   = field_id(i);
        const uint32_t temp = field_temp(i);
        const uint32_t hum  = field_hum(i);

        // Zero-pad the id by repeatedly prepending. Every prepend shifts the
        // existing characters along.
        std::string idstr = std::to_string(id);
        while (idstr.size() < 4) {
            idstr = "0" + idstr;
        }

        // Each "+" produces a temporary string that is immediately discarded.
        std::string rec = "S";
        rec = rec + idstr;
        rec = rec + ",";
        rec = rec + std::to_string(temp);
        rec = rec + ",";
        rec = rec + std::to_string(hum);
        rec = rec + ";";

        // The expensive line: copies the ENTIRE accumulated payload every pass.
        out = out + rec;
    }
    return out;
}

// ============================================================================
//  CODE B - OPTIMIZED  (Grade A)
// ============================================================================
//  Same output, built the cheap way:
//
//   1. reserve() once, so the payload buffer is allocated a single time and
//      never moves again.
//   2. Append straight into that buffer - no temporary strings at all.
//   3. Convert numbers to digits by hand into the buffer, so there is no
//      std::to_string round trip.
//
//  Total work is O(n): every byte of the payload is written exactly once.
// ----------------------------------------------------------------------------

// Writes v as exactly 4 zero-padded digits. Valid for v < 10000.
static void append_u32_pad4(std::string &out, uint32_t v)
{
    out.push_back((char)('0' + (v / 1000u) % 10u));
    out.push_back((char)('0' + (v / 100u)  % 10u));
    out.push_back((char)('0' + (v / 10u)   % 10u));
    out.push_back((char)('0' + (v)         % 10u));
}

// Writes v with no padding, using a small stack buffer (never touches the heap).
static void append_u32(std::string &out, uint32_t v)
{
    char tmp[12];
    int k = 0;

    if (v == 0u) {
        tmp[k++] = '0';
    }
    while (v != 0u) {
        tmp[k++] = (char)('0' + (v % 10u));
        v /= 10u;
    }
    while (k > 0) {
        out.push_back(tmp[--k]);   // digits came out backwards
    }
}

static std::string build_payload_B(uint32_t records)
{
    std::string out;
    out.reserve((size_t)records * RECORD_LEN + RECORD_LEN);   // one allocation

    for (uint32_t i = 0; i < records; ++i) {
        out.push_back('S');
        append_u32_pad4(out, field_id(i));
        out.push_back(',');
        append_u32(out, field_temp(i));
        out.push_back(',');
        append_u32(out, field_hum(i));
        out.push_back(';');
    }
    return out;
}

// ============================================================================
//  WORKLOADS
// ============================================================================
//  The record count is varied per repetition so the compiler cannot cache one
//  result and reuse it for every pass.
// ----------------------------------------------------------------------------
uint64_t workload_A_string(void)
{
    uint64_t acc = 0;
    for (uint32_t r = 0; r < REPEATS * g_scale; ++r) {
        const std::string s = build_payload_A(RECORDS - (r & 63u));
        acc = acc * 31u + fnv1a_str(s);
    }
    return acc;
}

uint64_t workload_B_string(void)
{
    uint64_t acc = 0;
    for (uint32_t r = 0; r < REPEATS * g_scale; ++r) {
        const std::string s = build_payload_B(RECORDS - (r & 63u));
        acc = acc * 31u + fnv1a_str(s);
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

    printf("GREEN-BENCH set=3 name=string records=%u repeats=%u\n",
           (unsigned)RECORDS, (unsigned)(REPEATS * g_scale));

    if (strcmp(mode, "A") == 0 || strcmp(mode, "a") == 0) {
        uint64_t cs = 0;
        const double ms = run_timed(workload_A_string, &cs);
        printf("variant=A label=concat_no_reserve\n");
        printf("checksum=0x%016llX\n", (unsigned long long)cs);
        printf("time_ms=%.3f\n", ms);
        return 0;
    }

    if (strcmp(mode, "B") == 0 || strcmp(mode, "b") == 0) {
        uint64_t cs = 0;
        const double ms = run_timed(workload_B_string, &cs);
        printf("variant=B label=reserve_append\n");
        printf("checksum=0x%016llX\n", (unsigned long long)cs);
        printf("time_ms=%.3f\n", ms);
        return 0;
    }

    // ---- direct string comparison: the strongest possible identity proof ----
    const std::string sampleA = build_payload_A(RECORDS);
    const std::string sampleB = build_payload_B(RECORDS);
    const bool bytes_equal = (sampleA == sampleB);

    printf("\n  payload bytes    : A=%u  B=%u\n",
           (unsigned)sampleA.size(), (unsigned)sampleB.size());
    printf("  first 48 chars A : %s\n", sampleA.substr(0, 48).c_str());
    printf("  first 48 chars B : %s\n", sampleB.substr(0, 48).c_str());
    printf("  BYTE-FOR-BYTE EQUAL : %s\n", bytes_equal ? "YES" : "NO");

    uint64_t csA = 0, csB = 0;
    const double msA = run_timed(workload_A_string, &csA);
    const double msB = run_timed(workload_B_string, &csB);

    printf("\n  %-28s %12s  %18s\n", "variant", "time_ms", "checksum");
    printf("  %-28s %12.3f  %018llX\n", "A  concat, no reserve",
           msA, (unsigned long long)csA);
    printf("  %-28s %12.3f  %018llX\n", "B  reserve + append",
           msB, (unsigned long long)csB);

    const bool same = bytes_equal && (csA == csB);
    printf("\n  OUTPUT IDENTICAL : %s\n", same ? "YES (PASS)" : "NO (FAIL)");
    if (msB > 0.0) {
        printf("  A COSTS          : %.1fx the CPU time of B\n", msA / msB);
    }
    return same ? 0 : 1;
}

#endif // GREEN_BENCH_NO_MAIN

// ============================================================================
//  GREEN CODE PROFILER - ESP32 RUNNER
//
//  Runs the exact same benchmark sources that the PC profiler uses, on real
//  hardware. Copy set1_sorting.cpp, set2_math.cpp and set3_string.cpp into
//  THIS folder (next to esp32_runner.ino) and press Upload.
//
//  No edits to those files are needed: each one detects ESP32/ARDUINO and
//  (a) shrinks its workload constants to fit 320 KB of RAM, and
//  (b) compiles out its PC main() so it does not clash with the Arduino core.
//
//  Because the checksums are computed identically on both platforms, an ESP32
//  run and a PC run of the same variant print the SAME checksum. That is direct
//  evidence the PC is executing genuinely equivalent work - the strongest
//  answer to "but you measured on a PC, not on the microcontroller".
// ============================================================================

#include <Arduino.h>
#include <stdint.h>

// Defined in the three benchmark .cpp files in this folder.
extern uint64_t workload_A_sort(void);
extern uint64_t workload_B_sort(void);
extern uint64_t workload_A_math(void);
extern uint64_t workload_B_math(void);
extern uint64_t workload_A_string(void);
extern uint64_t workload_B_string(void);

static void run_pair(const char *set_name,
                     const char *label_a, uint64_t (*fn_a)(void),
                     const char *label_b, uint64_t (*fn_b)(void))
{
    // Free heap before/after is worth watching: variant A of set 3 churns the
    // allocator hard, which is what fragments a long-running device.
    const uint32_t heap_before = ESP.getFreeHeap();

    const uint32_t t0 = micros();
    const uint64_t cs_a = fn_a();
    const uint32_t t1 = micros();

    const uint64_t cs_b = fn_b();
    const uint32_t t2 = micros();

    const uint32_t us_a = t1 - t0;
    const uint32_t us_b = t2 - t1;

    Serial.println();
    Serial.printf("=== %s ===\n", set_name);
    Serial.printf("  A  %-24s %8lu us   checksum=%016llX\n",
                  label_a, (unsigned long)us_a, (unsigned long long)cs_a);
    Serial.printf("  B  %-24s %8lu us   checksum=%016llX\n",
                  label_b, (unsigned long)us_b, (unsigned long long)cs_b);
    Serial.printf("  OUTPUT IDENTICAL : %s\n", (cs_a == cs_b) ? "YES (PASS)" : "NO (FAIL)");
    if (us_b > 0) {
        Serial.printf("  A COSTS          : %.1fx the CPU time of B\n",
                      (double)us_a / (double)us_b);
    }
    Serial.printf("  heap free        : %lu -> %lu bytes\n",
                  (unsigned long)heap_before, (unsigned long)ESP.getFreeHeap());
}

void setup()
{
    Serial.begin(115200);
    delay(1500);   // let the USB serial monitor attach

    Serial.println();
    Serial.println("Green Code Profiler - ESP32 benchmark run");
    Serial.printf("CPU %u MHz, free heap %lu bytes\n",
                  (unsigned)getCpuFrequencyMhz(), (unsigned long)ESP.getFreeHeap());

    run_pair("SET 1  sorting",
             "Bubble Sort O(n^2)",  workload_A_sort,
             "std::sort O(n log n)", workload_B_sort);

    run_pair("SET 2  math",
             "loop + division",     workload_A_math,
             "formula + shift",     workload_B_math);

    run_pair("SET 3  string",
             "concat, no reserve",  workload_A_string,
             "reserve + append",    workload_B_string);

    Serial.println();
    Serial.println("Done.");
}

void loop()
{
    delay(1000);
}

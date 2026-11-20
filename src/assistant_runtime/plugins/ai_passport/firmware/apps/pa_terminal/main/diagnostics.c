/* Read-only board diagnostics on the native USB Serial/JTAG console. */
#include "diagnostics.h"
#include "object_cache.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_sleep.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "network.h"
#include "pictures.h"
#include "settings.h"
#include "terminal_audio.h"
#include "terminal_limits.h"
#include <stdio.h>
#include <string.h>

static void report_memory(void) {
    pp_memory_stats_t m = pp_memory_stats();
    pp_cache_info_t cache = pp_objects_info();
    pp_stream_stats_t stream = pp_stream_stats();
    /* Each entry is [held, reclaimable, peak, allocation failures], in bytes
     * except for the final count. Driver memory is included in heap pressure,
     * but is outside these application buffer counters. */
#define USE(use, reclaimable) m.live[use], (unsigned)(reclaimable), m.peak[use], m.failures[use]
    printf("{\"app\":\"PA-Terminal-memory\",\"heap\":%u,\"largest\":%u,\"minimum\":%u,"
           "\"connected\":%s,\"cache_in_flight\":%u,\"displayed_image\":%u,"
           "\"stream_stored\":%u,\"stream_peak\":%u,\"stream_writes\":%u,\"stream_reads\":%u,\"stream_errors\":%u,"
           "\"stream_workspace\":%u,\"stream_ram_only\":%u,\"stream_full\":%u,"
           "\"fields\":[\"held\",\"reclaimable\",\"peak\",\"failures\"],"
           "\"capture\":[%u,%u,%u,%u],\"playback\":[%u,%u,%u,%u],"
           "\"image\":[%u,%u,%u,%u],\"network_setup\":[%u,%u,%u,%u],"
           "\"tls_certificate\":[%u,%u,%u,%u],\"wire\":[%u,%u,%u,%u],"
           "\"audio_wire\":[%u,%u,%u,%u],\"cache\":[%u,%u,%u,%u]}\n",
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
           (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
           (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT),
           pp_net_connected() ? "true" : "false", cache.in_flight, pp_picture_displayed_bytes(),
           stream.stored_bytes, stream.peak_bytes, stream.writes, stream.reads, stream.errors,
           stream.workspace_bytes, stream.ram_only, stream.full,
           USE(PP_MEMORY_CAPTURE, 0), USE(PP_MEMORY_PLAYBACK, 0),
           USE(PP_MEMORY_IMAGE, 0), USE(PP_MEMORY_NETWORK_SETUP, 0),
           USE(PP_MEMORY_TLS_CERT, 0), USE(PP_MEMORY_WIRE, 0),
           USE(PP_MEMORY_AUDIO, 0), USE(PP_MEMORY_CACHE, cache.recoverable));
#undef USE
}

static void usb_task(void *arg) {
    (void)arg;
    char line[64];
    unsigned n = 0;
    bool overflow = false;
    for (;;) {
        int ch = getchar();
        if (ch == EOF) {
            clearerr(stdin);
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (ch == '\r' || ch == '\n') {
            line[n] = 0;
            if (!overflow && !strcmp(line, "pp.memory")) {
                report_memory();
            } else if (!overflow && !strcmp(line, "pp.audio")) {
                pp_playback_report_t r = pp_audio_playback_report();
                printf("{\"app\":\"PA-Terminal-audio\",\"generation\":%u,\"window_bytes\":%u,"
                       "\"received_bytes\":%u,\"received_crc32\":%u,\"i2s_bytes\":%u,\"i2s_crc32\":%u,"
                       "\"underruns\":%u}\n",
                       (unsigned)r.generation, (unsigned)r.window_bytes, (unsigned)r.received_bytes,
                       (unsigned)r.received_crc32, (unsigned)r.i2s_bytes, (unsigned)r.i2s_crc32,
                       (unsigned)r.underruns);
            } else if (!overflow && !strcmp(line, "pp.status")) {
                char device[33] = "";
                /* The SDK's log helper can truncate this digest to a configured prefix. */
                static const char hex[] = "0123456789abcdef";
                const esp_app_desc_t *app = esp_app_get_description();
                char elf_sha256[2 * sizeof(app->app_elf_sha256) + 1];
                for (size_t i = 0; i < sizeof(app->app_elf_sha256); i++) {
                    unsigned byte = app->app_elf_sha256[i];
                    elf_sha256[2 * i] = hex[byte >> 4];
                    elf_sha256[2 * i + 1] = hex[byte & 15];
                }
                elf_sha256[sizeof(elf_sha256) - 1] = 0;
                bool offline_cache = false;
                (void)pp_settings_device_status(device, &offline_cache);
                pp_cache_info_t cache = pp_objects_info();
                pp_memory_stats_t memory = pp_memory_stats();
                printf("{\"app\":\"PA-Terminal\",\"protocol\":%u,\"version\":\"%s\","
                       "\"elf_sha256\":\"%s\",\"connected\":%"
                       "s,\"wifi_connected\":%s,\"wifi_standby\":%s,\"device_id\":\"%s\","
                       "\"offline_cache\":%s,\"heap\":%u,\"largest\":%u,"
                       "\"minimum\":%u,\"wake_cause\":%u,\"cache_ready\":%s,\"cache_hits\":%u,"
                       "\"cache_misses\":%u,"
                       "\"cache_write_failures\":%u,\"cache_busy\":%u,\"cache_ram_hits\":%u,"
                       "\"cache_flash_hits\":%u,\"cache_recoverable\":%u,\"cache_in_flight\":%u,"
                       "\"cache_io_peak_ms\":%u,\"cache_stack_min_free\":%u,\"memory_waits\":%u,\"memory_wait_expired\":%u,"
                       "\"memory_peak\":{\"wire\":%u,\"audio\":%u,\"image\":%u,\"cache\":%u},"
                       "\"memory_failures\":{\"wire\":%u,\"audio\":%u,\"image\":%u,\"cache\":%u}}\n",
                       PP_PROTOCOL_VERSION, app->version, elf_sha256,
                       pp_net_connected() ? "true" : "false",
                       pp_net_wifi_connected() ? "true" : "false",
                       pp_net_in_standby() ? "true" : "false",
                       device, offline_cache ? "true" : "false",
                       (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
                       (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
                       (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT),
                       (unsigned)esp_sleep_get_wakeup_cause(),
                       cache.ready ? "true" : "false", cache.hits, cache.misses,
                       cache.write_failures, cache.busy, cache.ram_hits, cache.flash_hits,
                       cache.recoverable, cache.in_flight, cache.io_peak_ms, cache.stack_min_free, memory.waits, memory.expired,
                       memory.peak[PP_MEMORY_WIRE], memory.peak[PP_MEMORY_AUDIO],
                       memory.peak[PP_MEMORY_IMAGE], memory.peak[PP_MEMORY_CACHE],
                       memory.failures[PP_MEMORY_WIRE], memory.failures[PP_MEMORY_AUDIO],
                       memory.failures[PP_MEMORY_IMAGE], memory.failures[PP_MEMORY_CACHE]);
            }
            n = 0;
            overflow = false;
        } else if (n + 1 < sizeof(line) && !overflow)
            line[n++] = ch;
        else
            overflow = true;
    }
}
void pp_usb_start(void) { xTaskCreate(usb_task, "pp_usb", 3072, NULL, 2, NULL); }

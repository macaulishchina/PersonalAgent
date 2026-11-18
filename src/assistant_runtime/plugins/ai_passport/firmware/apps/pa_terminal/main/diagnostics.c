/* Read-only board diagnostics on the native USB Serial/JTAG console. */
#include "diagnostics.h"
#include "cache_disk.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_sleep.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "network.h"
#include "settings.h"
#include <stdio.h>
#include <string.h>

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
            if (!overflow && !strcmp(line, "pp.status")) {
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
                pp_disk_info_t cache = pp_disk_info();
                printf("{\"app\":\"PA-Terminal\",\"protocol\":2,\"version\":\"%s\","
                       "\"elf_sha256\":\"%s\",\"connected\":%"
                       "s,\"device_id\":\"%s\",\"offline_cache\":%s,\"heap\":%u,\"largest\":%u,"
                       "\"minimum\":%u,\"wake_cause\":%u,\"cache_ready\":%s,\"cache_hits\":%u,"
                       "\"cache_misses\":%u,"
                       "\"cache_write_failures\":%u}\n",
                       app->version, elf_sha256,
                       pp_net_connected() ? "true" : "false",
                       device, offline_cache ? "true" : "false",
                       (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
                       (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
                       (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT),
                       (unsigned)esp_sleep_get_wakeup_cause(),
                       cache.ready ? "true" : "false", cache.hits, cache.misses,
                       cache.write_failures);
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

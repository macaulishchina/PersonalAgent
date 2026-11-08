#include "settings.h"
#include "bootloader_random.h"
#include "esp_app_desc.h"
#include "esp_heap_caps.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "network.h"
#include "cache_disk.h"
#include "nvs.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *space = "pa_terminal_v2";
static pp_settings_t *current;
static SemaphoreHandle_t lock;
static void random_hex(char *out, size_t bytes) {
    static const char alphabet[] = "0123456789abcdef";
    unsigned char raw[32];
    esp_fill_random(raw, bytes);
    for (size_t i = 0; i < bytes; i++) {
        out[2 * i] = alphabet[raw[i] >> 4];
        out[2 * i + 1] = alphabet[raw[i] & 15];
    }
    out[2 * bytes] = 0;
    memset(raw, 0, sizeof(raw));
}
/* Caller holds lock; NVS blob replacement is committed before changing RAM. */
static esp_err_t save(const pp_settings_t *candidate) {
    if (!pp_settings_valid(candidate))
        return ESP_ERR_INVALID_ARG;
    nvs_handle_t n;
    esp_err_t e = nvs_open(space, NVS_READWRITE, &n);
    if (e != ESP_OK)
        return e;
    char *raw = pp_settings_encode(candidate);
    e = raw ? nvs_set_str(n, "settings", raw) : ESP_ERR_NO_MEM;
    if (e == ESP_OK)
        e = nvs_commit(n);
    if (raw) {
        memset(raw, 0, strlen(raw));
        free(raw);
    }
    nvs_close(n);
    if (e == ESP_OK && current != candidate)
        memcpy(current, candidate, sizeof(*current));
    return e;
}
esp_err_t pp_settings_load(pp_settings_t *s) {
    if (current)
        return ESP_ERR_INVALID_STATE;
    lock = xSemaphoreCreateMutex();
    if (!lock)
        return ESP_ERR_NO_MEM;
    current = s;
    nvs_handle_t n;
    esp_err_t e = nvs_open(space, NVS_READWRITE, &n);
    if (e != ESP_OK)
        return e;
    size_t size = 0;
    e = nvs_get_str(n, "settings", NULL, &size);
    if (e == ESP_OK) {
        if (!size || size > 8193)
            e = ESP_ERR_INVALID_SIZE;
        else {
            char *raw = malloc(size);
            if (!raw)
                e = ESP_ERR_NO_MEM;
            else {
                e = nvs_get_str(n, "settings", raw, &size);
                if (e == ESP_OK && !pp_settings_decode(raw, s))
                    e = ESP_ERR_INVALID_STATE;
                memset(raw, 0, size);
                free(raw);
            }
        }
    }
    nvs_close(n);
    if (e == ESP_ERR_NVS_NOT_FOUND) {
        memset(s, 0, sizeof(*s));
        s->magic = PP_SETTINGS_MAGIC;
        s->volume = 45;
        s->brightness = 65;
        s->text_size = 16;
        s->sleep_seconds = 120;
        s->offline_cache = s->show_images = true;
        bootloader_random_enable();
        random_hex(s->device, 16);
        random_hex(s->credential, 32);
        bootloader_random_disable();
        return save(s);
    }
    return e;
}
esp_err_t pp_settings_snapshot(pp_settings_t *s) {
    if (!current || !lock || !xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    memcpy(s, current, sizeof(*s));
    xSemaphoreGive(lock);
    return ESP_OK;
}
static pp_settings_t *candidate(void) {
    pp_settings_t *s = malloc(sizeof(*s));
    if (!s)
        return NULL;
    memcpy(s, current, sizeof(*s));
    return s;
}
static void dispose(pp_settings_t *s) {
    if (s) {
        memset(s, 0, sizeof(*s));
        free(s);
    }
}
esp_err_t pp_settings_bootstrap(const char *raw) {
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    pp_settings_t *s = candidate();
    esp_err_t e =
        s ? (pp_settings_prepare_bootstrap(current, raw, s) ? save(s) : ESP_ERR_INVALID_ARG)
          : ESP_ERR_NO_MEM;
    dispose(s);
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_settings_cloud(const cJSON *changes, cJSON **receipt, bool *reconnect) {
    *receipt = NULL;
    *reconnect = false;
    if (!cJSON_IsObject(changes) || cJSON_GetArraySize(changes) > PP_CONFIG_FIELDS)
        return ESP_ERR_INVALID_ARG;
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    pp_settings_t *s = candidate();
    cJSON *r = cJSON_CreateObject(), *versions = cJSON_CreateObject(),
          *actual = cJSON_CreateObject();
    esp_err_t e = ESP_ERR_NO_MEM;
    bool changed = false;
    if (s && r && versions && actual) {
        e = pp_settings_prepare_cloud(current, changes, s, versions, &changed, reconnect)
                ? ESP_OK
                : ESP_ERR_INVALID_ARG;
        if (e == ESP_OK && changed)
            e = save(s);
        if (e == ESP_OK &&
            (!cJSON_AddNumberToObject(actual, "volume", s->volume) ||
             !cJSON_AddStringToObject(actual, "input_mode", s->queue ? "queue" : "steer") ||
             !cJSON_AddBoolToObject(actual, "keep_online", s->keep_online)))
            e = ESP_ERR_NO_MEM;
        if (e == ESP_OK) {
            cJSON_AddStringToObject(r, "type", "configuration.applied");
            cJSON_AddItemToObject(r, "versions", versions);
            versions = NULL;
            cJSON_AddItemToObject(r, "actual", actual);
            actual = NULL;
            *receipt = r;
            r = NULL;
        }
    }
    dispose(s);
    cJSON_Delete(r);
    cJSON_Delete(versions);
    cJSON_Delete(actual);
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_settings_binding(const char *binding) {
    if (!binding || !binding[0] || strlen(binding) > 64)
        return ESP_ERR_INVALID_ARG;
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    esp_err_t e = ESP_OK;
    if (strcmp(current->binding, binding)) {
        pp_settings_t *s = candidate();
        if (!s)
            e = ESP_ERR_NO_MEM;
        else {
            strcpy(s->binding, binding);
            memset(s->versions, 0, sizeof(s->versions));
            e = save(s);
            dispose(s);
        }
    }
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_preference(const char *key, unsigned value) {
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    pp_settings_t *s = candidate();
    esp_err_t e = ESP_ERR_NO_MEM;
    if (s) {
        bool ok = true;
        if (!strcmp(key, "volume") && value <= 100)
            s->volume = value;
        else if (!strcmp(key, "mode") && value <= 1)
            s->queue = value;
        else if (!strcmp(key, "online") && value <= 1)
            s->keep_online = value;
        else if (!strcmp(key, "paired") && value <= 1)
            s->paired = value;
        else
            ok = false;
        e = ok ? save(s) : ESP_ERR_INVALID_ARG;
        dispose(s);
    }
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_settings_reset(void) {
    nvs_handle_t n;
    esp_err_t e = nvs_open(space, NVS_READWRITE, &n);
    if (e == ESP_OK) {
        e = nvs_erase_all(n);
        if (e == ESP_OK)
            e = nvs_commit(n);
        nvs_close(n);
    }
    return e;
}
esp_err_t pp_pending(const char *id) {
    if (!id || strlen(id) > 36)
        return ESP_ERR_INVALID_ARG;
    nvs_handle_t n;
    esp_err_t e = nvs_open(space, NVS_READWRITE, &n);
    if (e != ESP_OK)
        return e;
    e = nvs_set_str(n, "pending", id);
    if (e == ESP_OK)
        e = nvs_commit(n);
    nvs_close(n);
    return e;
}
esp_err_t pp_pending_read(char *id, unsigned size) {
    nvs_handle_t n;
    esp_err_t e = nvs_open(space, NVS_READONLY, &n);
    if (e != ESP_OK)
        return e;
    size_t count = size;
    e = nvs_get_str(n, "pending", id, &count);
    nvs_close(n);
    return e;
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
            if (!overflow && !strcmp(line, "pp.status")) {
                char device[33] = "";
                bool offline_cache = false;
                if (lock && xSemaphoreTake(lock, pdMS_TO_TICKS(100)) == pdTRUE) {
                    if (current) {
                        strcpy(device, current->device);
                        offline_cache = current->offline_cache;
                    }
                    xSemaphoreGive(lock);
                }
                pp_disk_info_t cache = pp_disk_info();
                printf("{\"app\":\"PA-Terminal\",\"protocol\":2,\"version\":\"%s\",\"connected\":%"
                       "s,\"device_id\":\"%s\",\"offline_cache\":%s,\"heap\":%u,\"largest\":%u,"
                       "\"minimum\":%u,\"cache_ready\":%s,\"cache_hits\":%u,\"cache_misses\":%u,"
                       "\"cache_write_failures\":%u}\n",
                       esp_app_get_description()->version, pp_net_connected() ? "true" : "false",
                       device, offline_cache ? "true" : "false",
                       (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
                       (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
                       (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT),
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

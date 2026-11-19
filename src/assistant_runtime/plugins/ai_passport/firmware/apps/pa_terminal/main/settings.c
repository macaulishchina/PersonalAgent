#include "settings.h"
#include "bootloader_random.h"
#include "esp_random.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *space = "pa_terminal_v2";
static pp_settings_t stored;
static atomic_uint revision;
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
    if (e == ESP_OK) {
        if (candidate != &stored)
            stored = *candidate;
        ++revision;
    }
    return e;
}
static esp_err_t load_record(pp_settings_t *s) {
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
esp_err_t pp_settings_load(pp_terminal_settings_t *view) {
    if (lock)
        return ESP_ERR_INVALID_STATE;
    lock = xSemaphoreCreateMutex();
    if (!lock)
        return ESP_ERR_NO_MEM;
    esp_err_t e = load_record(&stored);
    if (e == ESP_OK) {
        revision = 1;
        pp_settings_project(&stored, revision, view);
    } else {
        memset(&stored, 0, sizeof(stored));
    }
    return e;
}
bool pp_settings_refresh(pp_terminal_settings_t *view) {
    if (view->revision == atomic_load(&revision))
        return true;
    if (xSemaphoreTake(lock, 0) != pdTRUE)
        return false;
    pp_settings_project(&stored, revision, view);
    xSemaphoreGive(lock);
    return true;
}
esp_err_t pp_settings_connection(pp_connection_settings_t *s, pp_buffer_t *certificate) {
    if (!lock || !xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    esp_err_t error = ESP_OK;
    if (!strncmp(stored.url, "https://", 8) && stored.ca[0]) {
        size_t size = strlen(stored.ca) + 1;
        if (pp_buffer_reserve(certificate, size, PP_MEMORY_CRITICAL, PP_MEMORY_TLS_CERT)) {
            memcpy(certificate->data, stored.ca, size);
            certificate->size = size;
        } else
            error = ESP_ERR_NO_MEM;
    }
    if (error == ESP_OK) {
        memcpy(s->url, stored.url, sizeof(s->url));
        memcpy(s->device, stored.device, sizeof(s->device));
        memcpy(s->credential, stored.credential, sizeof(s->credential));
        memcpy(s->wifi, stored.wifi, sizeof(s->wifi));
        s->wifi_count = stored.wifi_count;
        s->paired = stored.paired;
    }
    xSemaphoreGive(lock);
    return error;
}
static pp_settings_t *candidate(void) {
    pp_settings_t *s = malloc(sizeof(*s));
    if (!s)
        return NULL;
    *s = stored;
    return s;
}
static void dispose(pp_settings_t *s) {
    if (s) {
        memset(s, 0, sizeof(*s));
        free(s);
    }
}
esp_err_t pp_settings_bootstrap(const char *raw, const char *release_url, const char *release_ca) {
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    pp_settings_t *s = malloc(sizeof(*s));
    esp_err_t e = s ? (pp_settings_prepare_bootstrap(&stored, raw, release_url, release_ca, s)
                           ? save(s)
                           : ESP_ERR_INVALID_ARG)
                    : ESP_ERR_NO_MEM;
    dispose(s);
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_settings_cloud(const cJSON *changes, cJSON **receipt, bool *reconnect,
                            pp_terminal_settings_t *view) {
    *receipt = NULL;
    *reconnect = false;
    if (!cJSON_IsObject(changes) || cJSON_GetArraySize(changes) > PP_CONFIG_FIELDS)
        return ESP_ERR_INVALID_ARG;
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    pp_settings_t *s = malloc(sizeof(*s));
    cJSON *r = cJSON_CreateObject();
    cJSON *versions = r ? cJSON_AddObjectToObject(r, "versions") : NULL;
    cJSON *actual = r ? cJSON_AddObjectToObject(r, "actual") : NULL;
    esp_err_t e = ESP_ERR_NO_MEM;
    bool changed = false;
    if (s && r && versions && actual) {
        e = pp_settings_prepare_cloud(&stored, changes, s, versions, &changed, reconnect)
                ? ESP_OK
                : ESP_ERR_INVALID_ARG;
        /* Construct the entire receipt before committing. Under memory pressure
         * a failed receipt must not leave new settings applied without notice. */
        if (e == ESP_OK &&
            (!cJSON_AddNumberToObject(actual, "volume", s->volume) ||
             !cJSON_AddStringToObject(actual, "input_mode", s->queue ? "queue" : "steer") ||
             !cJSON_AddBoolToObject(actual, "keep_online", s->keep_online) ||
             !cJSON_AddStringToObject(r, "type", "configuration.applied")))
            e = ESP_ERR_NO_MEM;
        if (e == ESP_OK && changed)
            e = save(s);
        if (e == ESP_OK) {
            *receipt = r;
            r = NULL;
            pp_settings_project(&stored, revision, view);
        }
    }
    dispose(s);
    cJSON_Delete(r);
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_settings_binding(const char *binding, pp_terminal_settings_t *view) {
    if (!binding || !binding[0] || strlen(binding) > 64)
        return ESP_ERR_INVALID_ARG;
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    esp_err_t e = ESP_OK;
    if (strcmp(stored.binding, binding)) {
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
    if (e == ESP_OK)
        pp_settings_project(&stored, revision, view);
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_preference(pp_preference_t key, unsigned value, pp_terminal_settings_t *view) {
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    unsigned previous;
    esp_err_t e = ESP_ERR_INVALID_ARG;
    if (pp_settings_edit_preference(&stored, key, value, &previous)) {
        e = previous == value ? ESP_OK : save(&stored);
        if (e != ESP_OK) {
            unsigned ignored;
            pp_settings_edit_preference(&stored, key, previous, &ignored);
        } else if (view) {
            pp_settings_project(&stored, revision, view);
        }
    }
    xSemaphoreGive(lock);
    return e;
}
static esp_err_t write_pending(const char *id);
static esp_err_t read_pending(char *id, unsigned size);
/* Called by the network worker with the radio running (hardware entropy).
 * Scope a legacy pending receipt to the old device before replacing its identity.
 * Power loss before the settings commit leaves the old receipt readable. */
esp_err_t pp_settings_new_identity(void) {
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    pp_settings_t *s = candidate();
    esp_err_t e = ESP_ERR_NO_MEM;
    if (s) {
        char pending_id[37] = "";
        e = read_pending(pending_id, sizeof(pending_id));
        if (e == ESP_ERR_NVS_NOT_FOUND)
            e = ESP_OK;
        if (e == ESP_OK && pending_id[0])
            e = write_pending(pending_id);
        if (e == ESP_OK) {
            pp_settings_forget_binding(s);
            random_hex(s->device, 16);
            random_hex(s->credential, 32);
            e = save(s);
        }
        dispose(s);
    }
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_settings_reset(void) {
    if (lock && !xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    nvs_handle_t n;
    esp_err_t e = nvs_open(space, NVS_READWRITE, &n);
    if (e == ESP_OK) {
        e = nvs_erase_all(n);
        if (e == ESP_OK)
            e = nvs_commit(n);
        nvs_close(n);
    }
    if (lock)
        xSemaphoreGive(lock);
    return e;
}
static esp_err_t write_pending(const char *id) {
    if (!id || strlen(id) > 36)
        return ESP_ERR_INVALID_ARG;
    nvs_handle_t n;
    esp_err_t e = nvs_open(space, NVS_READWRITE, &n);
    if (e != ESP_OK)
        return e;
    char scoped[72];
    snprintf(scoped, sizeof(scoped), "%s:%s", stored.device, id);
    e = nvs_set_str(n, "pending", scoped);
    if (e == ESP_OK)
        e = nvs_commit(n);
    nvs_close(n);
    return e;
}
static esp_err_t read_pending(char *id, unsigned size) {
    nvs_handle_t n;
    esp_err_t e = nvs_open(space, NVS_READONLY, &n);
    if (e != ESP_OK)
        return e;
    char raw[72];
    size_t count = sizeof(raw);
    e = nvs_get_str(n, "pending", raw, &count);
    if (e == ESP_OK) {
        char *value = strchr(raw, ':');
        if (value) {
            *value++ = 0;
            if (strcmp(raw, stored.device))
                value = "";
        } else
            value = raw;
        if (strlen(value) >= size)
            e = ESP_ERR_INVALID_SIZE;
        else
            strcpy(id, value);
    }
    nvs_close(n);
    return e;
}
esp_err_t pp_pending(const char *id) {
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    esp_err_t e = write_pending(id);
    xSemaphoreGive(lock);
    return e;
}
esp_err_t pp_pending_read(char *id, unsigned size) {
    if (!xSemaphoreTake(lock, pdMS_TO_TICKS(2000)))
        return ESP_ERR_TIMEOUT;
    esp_err_t e = read_pending(id, size);
    xSemaphoreGive(lock);
    return e;
}
bool pp_settings_device_status(char device[33], bool *offline_cache) {
    if (!revision || xSemaphoreTake(lock, pdMS_TO_TICKS(100)) != pdTRUE)
        return false;
    strcpy(device, stored.device);
    *offline_cache = stored.offline_cache;
    xSemaphoreGive(lock);
    return true;
}

#include "settings.h"
#include "cJSON.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static bool field(cJSON *obj, const char *name, char *out, size_t cap, bool optional) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!item && optional) {
        out[0] = 0;
        return true;
    }
    if (!cJSON_IsString(item) || strlen(item->valuestring) >= cap)
        return false;
    memcpy(out, item->valuestring, strlen(item->valuestring) + 1);
    return true;
}
static bool decode(const char *raw, pp_settings_t *s) {
    memset(s, 0, sizeof(*s));
    cJSON *j = cJSON_Parse(raw);
    if (!j)
        return false;
    bool ok = field(j, "ssid", s->ssid, sizeof(s->ssid), false) &&
              field(j, "password", s->password, sizeof(s->password), true) &&
              field(j, "url", s->url, sizeof(s->url), false) &&
              field(j, "ca", s->ca, sizeof(s->ca), false) &&
              field(j, "device_id", s->device, sizeof(s->device), false) &&
              field(j, "credential", s->credential, sizeof(s->credential), false) &&
              field(j, "pairing_token", s->pairing, sizeof(s->pairing), false);
    s->volume = 45;
    s->sleep_seconds = 120;
    cJSON *prefs = cJSON_GetObjectItemCaseSensitive(j, "settings"), *p;
    if (prefs) {
        s->keep_online = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(prefs, "keep_online"));
        field(prefs, "nickname", s->nickname, sizeof(s->nickname), true);
        p = cJSON_GetObjectItemCaseSensitive(prefs, "input_mode");
        s->queue = cJSON_IsString(p) && !strcmp(p->valuestring, "queue");
        p = cJSON_GetObjectItemCaseSensitive(prefs, "volume");
        if (cJSON_IsNumber(p) && p->valueint >= 0 && p->valueint <= 100)
            s->volume = p->valueint;
        p = cJSON_GetObjectItemCaseSensitive(prefs, "sleep_seconds");
        if (cJSON_IsNumber(p) && p->valueint >= 30 && p->valueint <= 3600)
            s->sleep_seconds = p->valueint;
    }
    if (!s->nickname[0])
        strcpy(s->nickname, "嘟嘟");
    cJSON *protocol = cJSON_GetObjectItemCaseSensitive(j, "protocol");
    ok = ok && cJSON_IsNumber(protocol) && protocol->valueint == 1 && s->ssid[0] &&
         strlen(s->credential) >= 32 && !strncmp(s->url, "wss://", 6) &&
         strstr(s->url, "/stream") && strstr(s->ca, "-----BEGIN CERTIFICATE-----") &&
         !strchr(s->url, '\r') && !strchr(s->url, '\n') && !strchr(s->credential, '\r') &&
         !strchr(s->credential, '\n') && !strchr(s->device, '\r') && !strchr(s->device, '\n');
    cJSON_Delete(j);
    return ok;
}
esp_err_t pp_settings_load(pp_settings_t *settings) {
    nvs_handle_t n;
    esp_err_t e = nvs_open("pa_terminal", NVS_READONLY, &n);
    if (e != ESP_OK)
        return e;
    size_t size = 0;
    e = nvs_get_str(n, "config", NULL, &size);
    if (e != ESP_OK || size > 8192) {
        nvs_close(n);
        return ESP_ERR_INVALID_SIZE;
    }
    char *raw = malloc(size);
    if (!raw) {
        nvs_close(n);
        return ESP_ERR_NO_MEM;
    }
    e = nvs_get_str(n, "config", raw, &size);
    if (e == ESP_OK && !decode(raw, settings))
        e = ESP_ERR_INVALID_ARG;
    memset(raw, 0, size);
    free(raw);
    uint32_t value;
    if (nvs_get_u32(n, "mode", &value) == ESP_OK)
        settings->queue = value == 1;
    if (nvs_get_u32(n, "volume", &value) == ESP_OK && value <= 100)
        settings->volume = value;
    if (nvs_get_u32(n, "online", &value) == ESP_OK)
        settings->keep_online = value == 1;
    if (nvs_get_u32(n, "paired", &value) == ESP_OK)
        settings->paired = value == 1;
    nvs_close(n);
    return e;
}
esp_err_t pp_settings_store(const char *raw) {
    if (strlen(raw) > 8191)
        return ESP_ERR_INVALID_SIZE;
    pp_settings_t *s = malloc(sizeof(*s));
    if (!s)
        return ESP_ERR_NO_MEM;
    bool ok = decode(raw, s);
    unsigned volume = s->volume, mode = s->queue, online = s->keep_online;
    memset(s, 0, sizeof(*s));
    free(s);
    if (!ok)
        return ESP_ERR_INVALID_ARG;
    nvs_handle_t n;
    esp_err_t e = nvs_open("pa_terminal", NVS_READWRITE, &n);
    if (e != ESP_OK)
        return e;
    e = nvs_set_str(n, "config", raw);
    if (e == ESP_OK)
        e = nvs_set_u32(n, "mode", mode);
    if (e == ESP_OK)
        e = nvs_set_u32(n, "volume", volume);
    if (e == ESP_OK)
        e = nvs_set_u32(n, "online", online);
    if (e == ESP_OK)
        e = nvs_set_u32(n, "paired", 0);
    if (e == ESP_OK)
        e = nvs_set_str(n, "pending", "");
    if (e == ESP_OK)
        e = nvs_commit(n);
    nvs_close(n);
    return e;
}
esp_err_t pp_preference(const char *key, unsigned value) {
    nvs_handle_t n;
    esp_err_t e = nvs_open("pa_terminal", NVS_READWRITE, &n);
    if (e != ESP_OK)
        return e;
    e = nvs_set_u32(n, key, value);
    if (e == ESP_OK)
        e = nvs_commit(n);
    nvs_close(n);
    return e;
}
esp_err_t pp_pending(const char *id) {
    nvs_handle_t n;
    esp_err_t e = nvs_open("pa_terminal", NVS_READWRITE, &n);
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
    esp_err_t e = nvs_open("pa_terminal", NVS_READONLY, &n);
    if (e != ESP_OK)
        return e;
    size_t count = size;
    e = nvs_get_str(n, "pending", id, &count);
    nvs_close(n);
    return e;
}
static void usb_task(void *arg) {
    (void)arg;
    char *line = calloc(1, 8192);
    if (!line) {
        vTaskDelete(NULL);
        return;
    }
    size_t used = 0;
    bool overflow = false;
    for (;;) {
        int ch = getchar();
        if (ch == EOF) {
            clearerr(stdin);
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (ch == '\r' || ch == '\n') {
            line[used] = 0;
            if (!strcmp(line, "pp.status"))
                puts("{\"app\":\"PA-Terminal\",\"protocol\":1}");
            else if (!overflow && !strcmp(line, "pp.reset CONFIRM")) {
                nvs_handle_t n;
                esp_err_t e = nvs_open("pa_terminal", NVS_READWRITE, &n);
                if (e == ESP_OK) {
                    e = nvs_erase_all(n);
                    if (e == ESP_OK)
                        e = nvs_commit(n);
                    nvs_close(n);
                }
                printf("{\"reset\":%s}\n", e == ESP_OK ? "true" : "false");
                fflush(stdout);
                if (e == ESP_OK) {
                    vTaskDelay(pdMS_TO_TICKS(200));
                    esp_restart();
                }
            } else if (!overflow && used > 10 && !strncmp(line, "pp.config ", 10)) {
                esp_err_t e = pp_settings_store(line + 10);
                printf("{\"configured\":%s}\n", e == ESP_OK ? "true" : "false");
                fflush(stdout);
                memset(line, 0, 8192);
                if (e == ESP_OK) {
                    vTaskDelay(pdMS_TO_TICKS(200));
                    esp_restart();
                }
            }
            memset(line, 0, used);
            used = 0;
            overflow = false;
        } else if (used < 8191)
            line[used++] = (char)ch;
        else
            overflow = true;
    }
}
void pp_usb_start(void) { xTaskCreate(usb_task, "pp_usb", 4096, NULL, 2, NULL); }

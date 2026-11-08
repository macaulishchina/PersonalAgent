#pragma once
#include "cJSON.h"
#include "network_model.h"
#include <stdbool.h>
#include <stdint.h>

#define PP_SETTINGS_MAGIC 0x50503201u
#define PP_CONFIG_FIELDS 12

typedef struct {
    char ssid[33], password[65];
} pp_wifi_t;
typedef struct {
    uint32_t magic;
    char device[33], credential[65], binding[65];
    char url[513], ca[4097];
    pp_wifi_t wifi[PP_WIFI_MAX];
    uint32_t versions[PP_CONFIG_FIELDS];
    unsigned wifi_count, volume, sleep_seconds, brightness, text_size;
    bool queue, keep_online, paired, offline_cache, show_images;
    unsigned speech_mode; /* 0 brief, 1 full, 2 off; synthesis remains on PA. */
} pp_settings_t;

bool pp_settings_valid(const pp_settings_t *settings);
bool pp_settings_prepare_bootstrap(const pp_settings_t *current, const char *json,
                                   pp_settings_t *candidate);
bool pp_settings_prepare_cloud(const pp_settings_t *current, const cJSON *fields,
                               pp_settings_t *candidate, cJSON *versions, bool *changed,
                               bool *reconnect);
/* Compact NVS representation omits unused Wi-Fi slots and an empty CA. */
char *pp_settings_encode(const pp_settings_t *settings);
bool pp_settings_decode(const char *json, pp_settings_t *settings);

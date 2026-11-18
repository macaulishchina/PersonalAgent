#pragma once
#include "cJSON.h"
#include "network_model.h"
#include <stdbool.h>
#include <stdint.h>

#define PP_SETTINGS_MAGIC 0x50503201u
#define PP_CONFIG_FIELDS 12
#define PP_WIFI_SETUP_JSON_MAX 1024

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

/* Main-loop snapshot. Network credentials, CA and cloud revisions stay with
 * the settings store; cache binding needs only these public identity fields. */
typedef struct {
    char url[sizeof(((pp_settings_t *)0)->url)];
    char device[sizeof(((pp_settings_t *)0)->device)];
    char binding[sizeof(((pp_settings_t *)0)->binding)];
    unsigned wifi_count, volume, sleep_seconds, brightness, text_size, revision;
    bool queue, keep_online, paired, offline_cache, show_images;
} pp_terminal_settings_t;
void pp_settings_project(const pp_settings_t *source, unsigned revision, pp_terminal_settings_t *view);

typedef enum { PP_PREF_VOLUME, PP_PREF_QUEUE, PP_PREF_KEEP_ONLINE, PP_PREF_PAIRED } pp_preference_t;
/* On success, previous is suitable for rollback while the caller holds its lock. */
bool pp_settings_edit_preference(pp_settings_t *settings, pp_preference_t key, unsigned value,
                                 unsigned *previous);
void pp_settings_forget_binding(pp_settings_t *candidate);
bool pp_settings_valid(const pp_settings_t *settings);
bool pp_settings_prepare_bootstrap(const pp_settings_t *current, const char *json,
                                   const char *release_url, const char *release_ca,
                                   pp_settings_t *candidate);
bool pp_settings_prepare_cloud(const pp_settings_t *current, const cJSON *fields,
                               pp_settings_t *candidate, cJSON *versions, bool *changed,
                               bool *reconnect);
/* Compact NVS representation omits unused Wi-Fi slots and an empty CA. */
char *pp_settings_encode(const pp_settings_t *settings);
bool pp_settings_decode(const char *json, pp_settings_t *settings);

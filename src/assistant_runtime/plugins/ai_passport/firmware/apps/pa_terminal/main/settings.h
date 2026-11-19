#pragma once
#include "byte_buffer.h"
#include "esp_err.h"
#include "settings_model.h"

/* One in-memory settings owner. Every mutation below commits one NVS blob. */
esp_err_t pp_settings_load(pp_terminal_settings_t *view);
/* Nonblocking, revision-checked UI refresh. No NVS reads in the frame loop. */
bool pp_settings_refresh(pp_terminal_settings_t *view);
typedef struct {
    char url[sizeof(((pp_settings_t *)0)->url)];
    char device[sizeof(((pp_settings_t *)0)->device)];
    char credential[sizeof(((pp_settings_t *)0)->credential)];
    pp_wifi_t wifi[PP_WIFI_MAX];
    unsigned wifi_count;
    bool paired;
} pp_connection_settings_t;
/* The setup snapshot and the optional exact-length PEM have separate owners.
 * The transport may keep referencing the PEM after it has copied URL and headers. */
esp_err_t pp_settings_connection(pp_connection_settings_t *settings, pp_buffer_t *certificate);
esp_err_t pp_settings_bootstrap(const char *json, const char *release_url, const char *release_ca);
esp_err_t pp_settings_cloud(const cJSON *fields, cJSON **receipt, bool *reconnect,
                            pp_terminal_settings_t *view);
esp_err_t pp_settings_binding(const char *binding, pp_terminal_settings_t *view);
esp_err_t pp_preference(pp_preference_t key, unsigned value, pp_terminal_settings_t *view);
esp_err_t pp_settings_new_identity(void);
esp_err_t pp_settings_reset(void);
esp_err_t pp_pending(const char *id);
esp_err_t pp_pending_read(char *id, unsigned size);
bool pp_settings_device_status(char device[33], bool *offline_cache);

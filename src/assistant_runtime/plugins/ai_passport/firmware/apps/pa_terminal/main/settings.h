#pragma once
#include "esp_err.h"
#include "settings_model.h"

/* One in-memory settings owner. Every mutation below commits one NVS blob. */
esp_err_t pp_settings_load(pp_settings_t *settings);
esp_err_t pp_settings_snapshot(pp_settings_t *settings);
esp_err_t pp_settings_bootstrap(const char *json);
esp_err_t pp_settings_cloud(const cJSON *fields, cJSON **receipt, bool *reconnect);
esp_err_t pp_settings_binding(const char *binding);
esp_err_t pp_preference(const char *key, unsigned value);
esp_err_t pp_settings_reset(void);
esp_err_t pp_pending(const char *id);
esp_err_t pp_pending_read(char *id, unsigned size);
void pp_usb_start(void);

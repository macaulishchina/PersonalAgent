#pragma once
#include "esp_err.h"
#include <stdbool.h>
typedef struct {
    char ssid[33], password[65], url[513], ca[4097];
    char device[65], credential[129], pairing[129], nickname[97];
    unsigned volume, sleep_seconds;
    bool queue, keep_online, paired;
} pp_settings_t;
esp_err_t pp_settings_load(pp_settings_t *settings);
esp_err_t pp_settings_store(const char *json);
esp_err_t pp_preference(const char *key, unsigned value);
esp_err_t pp_pending(const char *id);
esp_err_t pp_pending_read(char *id, unsigned size);
void pp_usb_start(void);

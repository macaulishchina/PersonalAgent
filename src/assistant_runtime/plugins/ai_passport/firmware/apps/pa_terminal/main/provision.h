#pragma once
#include "esp_err.h"
#include <stdbool.h>
esp_err_t pp_provision_start(void);
esp_err_t pp_provision_stop(void);
bool pp_provision_complete(void);
bool pp_provision_active(void);
typedef struct {
    char ssid[24], password[16];
} pp_hotspot_t;
/* The HTTP server must be ready before credentials are visible. */
bool pp_provision_credentials(pp_hotspot_t *out);

#pragma once
#include "esp_err.h"
#include <stdbool.h>

bool lab_cancelled(void);
void lab_require_restart(void);
void lab_notice(const char *format, ...) __attribute__((format(printf, 1, 2)));
esp_err_t lab_wifi_test(void);
esp_err_t lab_ble_test(void);

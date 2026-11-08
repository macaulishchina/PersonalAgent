#pragma once
#include "esp_err.h"
#include <stdbool.h>
esp_err_t pp_provision_start(void);
esp_err_t pp_provision_stop(void);
bool pp_provision_complete(void);
bool pp_provision_active(void);

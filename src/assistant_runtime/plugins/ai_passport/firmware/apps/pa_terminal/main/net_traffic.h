#pragma once
#include "esp_netif.h"
#include "status_meter.h"
void pp_traffic_attach(esp_netif_t *sta);
void pp_traffic_new_window(void);
pp_traffic_t pp_traffic_read(void);

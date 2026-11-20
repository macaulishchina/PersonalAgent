#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    uint32_t tx, rx, epoch;
} pp_traffic_t;
typedef struct {
    uint32_t at;
    pp_traffic_t previous;
    bool initialized, linked;
} pp_rate_sample_t;
typedef struct {
    uint16_t up_k, down_k;
    uint8_t flash, sram;
    bool rates_valid, flash_valid;
} pp_meter_t;

/* Use actual elapsed time, including timer/counter rollover. A new association
 * or wake starts a fresh observation window. */
bool pp_meter_sample(pp_rate_sample_t *sample, pp_meter_t *meter, uint32_t now,
                     bool linked, pp_traffic_t traffic);
uint8_t pp_meter_percent(uint32_t used, uint32_t total);

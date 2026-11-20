#include "status_meter.h"
#include <assert.h>
#include <limits.h>

int main(void) {
    pp_rate_sample_t window = {0};
    pp_meter_t meter = {0};
    pp_traffic_t bytes = {.tx = UINT32_MAX - 100, .epoch = 1};
    uint32_t start = UINT32_MAX - 300;
    assert(pp_meter_sample(&window, &meter, start, false, bytes));
    assert(!meter.rates_valid);
    assert(pp_meter_sample(&window, &meter, start, true, bytes));
    assert(!meter.rates_valid);
    bytes.tx += 3 * 1024 * 2;
    bytes.rx += 18 * 1024 * 2;
    assert(!pp_meter_sample(&window, &meter, start + 999, true, bytes));
    assert(pp_meter_sample(&window, &meter, start + 2000, true, bytes));
    assert(meter.rates_valid && meter.up_k == 3 && meter.down_k == 18);
    assert(pp_meter_sample(&window, &meter, start + 3000, true, bytes));
    assert(meter.rates_valid && !meter.up_k && !meter.down_k);
    /* A disconnect/reconnect or standby entirely between frames. */
    bytes.epoch++;
    bytes.rx += 1400000;
    assert(pp_meter_sample(&window, &meter, start + 3100, true, bytes));
    assert(!meter.rates_valid && !meter.down_k);
    assert(pp_meter_sample(&window, &meter, start + 3120, false, bytes));
    assert(!meter.rates_valid);
    assert(pp_meter_percent(0, 0) == 0 && pp_meter_percent(0, 1000) == 0);
    assert(pp_meter_percent(2, 3) == 67 && pp_meter_percent(2000, 1000) == 100);
}

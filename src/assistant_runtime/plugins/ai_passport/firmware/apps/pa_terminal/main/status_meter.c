#include "status_meter.h"
#include <limits.h>

uint8_t pp_meter_percent(uint32_t used, uint32_t total) {
    if (!total)
        return 0;
    return used >= total ? 100 : ((uint64_t)used * 100 + total / 2) / total;
}

static uint16_t kib_per_second(uint32_t bytes, uint32_t elapsed) {
    uint64_t divisor = (uint64_t)elapsed * 1024;
    uint64_t rate = ((uint64_t)bytes * 1000 + divisor / 2) / divisor;
    return rate > UINT16_MAX ? UINT16_MAX : (uint16_t)rate;
}

bool pp_meter_sample(pp_rate_sample_t *sample, pp_meter_t *meter, uint32_t now,
                     bool linked, pp_traffic_t traffic) {
    uint32_t elapsed = now - sample->at;
    bool same = sample->initialized && linked == sample->linked &&
                traffic.epoch == sample->previous.epoch;
    if (same && elapsed < 1000)
        return false;
    meter->rates_valid = linked && same;
    meter->up_k = meter->rates_valid ? kib_per_second(traffic.tx - sample->previous.tx, elapsed) : 0;
    meter->down_k = meter->rates_valid ? kib_per_second(traffic.rx - sample->previous.rx, elapsed) : 0;
    *sample = (pp_rate_sample_t){.initialized = true, .linked = linked, .at = now,
                                 .previous = traffic};
    return true;
}

#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "cJSON.h"

/* One bounded read, independent of accepted task/execution state. */
typedef struct {
    char id[65];
    unsigned revision, offset;
    uint32_t started;
    bool pending;
} pp_notice_read_t;
bool pp_notice_begin(pp_notice_read_t *read, const char *id, unsigned revision, unsigned offset,
                     uint32_t now);
/* 1: current valid page; 0: stale/unrelated; -1: current page is malformed.
 * Both terminal results release the wait. No result triggers a retry. */
int pp_notice_page(pp_notice_read_t *read, const cJSON *page);
bool pp_notice_expire(pp_notice_read_t *read, uint32_t now);

#pragma once
#include "cJSON.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PP_HISTORY_IMAGES 4
#define PP_MESSAGE_ID 160
#define PP_HISTORY_BLOCK_CHARS 1600u
#define PP_LEGACY_HISTORY_BLOCK_CHARS 80u

typedef struct {
    char id[PP_MESSAGE_ID], images[PP_HISTORY_IMAGES][33];
    const char *text; /* Borrowed from the JSON block for the duration of the callback. */
    char group[192];
    double revision;
    bool legacy;
    unsigned offset, next, total, image_count;
    bool user, complete, image_only, local_excerpt;
} pp_history_page_t;
pp_history_page_t *pp_timeline_window(pp_history_page_t *page, const cJSON *response, unsigned offset);
pp_history_page_t *pp_timeline_decode(pp_history_page_t *page, const cJSON *response);

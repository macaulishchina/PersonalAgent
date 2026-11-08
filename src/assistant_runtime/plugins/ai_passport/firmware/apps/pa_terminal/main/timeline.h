#pragma once
#include "cJSON.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PP_TIMELINE_ENTRIES 3
#define PP_MESSAGE_ID 160
#define PP_HISTORY_CURSOR 512

typedef struct {
    char id[PP_MESSAGE_ID], cursor[PP_HISTORY_CURSOR];
    bool user;
} pp_history_entry_t;
typedef struct {
    char id[PP_MESSAGE_ID], text[641], images[4][33];
    double revision;
    unsigned offset, next, total, image_count;
    bool user, complete, image_only;
} pp_history_page_t;
typedef struct {
    pp_history_entry_t entries[PP_TIMELINE_ENTRIES];
    unsigned count;
} pp_timeline_t;

/* The directory is the only persistent RAM window. Payloads go through the
 * common Flash cache; no second LRU competes with the recent offline window. */
bool pp_timeline_index(pp_timeline_t *timeline, const cJSON *response);
const pp_history_entry_t *pp_timeline_entry(const pp_timeline_t *timeline, const char *id);
const pp_history_entry_t *pp_timeline_neighbor(const pp_timeline_t *timeline, const char *id,
                                               int direction);
pp_history_page_t *pp_timeline_decode(pp_history_page_t *page, const cJSON *response);

#pragma once
#include "cJSON.h"
#include "cache_disk.h"
#include <stdbool.h>
#include <stdint.h>
/* All calls belong to the application controller, never the network callback. */
void pp_picture_init(void (*status)(const char *));
bool pp_picture_open(const char *message, const char *image);
bool pp_picture_close(void);
bool pp_picture_receive(const cJSON *response);
bool pp_picture_active(void);
bool pp_picture_loading(void);
bool pp_picture_poll(uint32_t now); /* True only when a transfer expired. */
void pp_picture_cached(pp_disk_result_t *result);
bool pp_picture_prefetch(const char *message, const char *image);

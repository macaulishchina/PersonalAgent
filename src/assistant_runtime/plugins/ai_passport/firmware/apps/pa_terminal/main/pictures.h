#pragma once
#include "cJSON.h"
#include "object_cache.h"
#include <stdbool.h>
#include <stdint.h>
/* All calls belong to the application controller, never the network callback. */
void pp_picture_init(void (*status)(const char *));
void pp_picture_stream_supported(bool supported);
bool pp_picture_open(const char *message, const char *image, double revision, const char *group);
bool pp_picture_close(void);
bool pp_picture_receive(const cJSON *response);
void pp_picture_receive_strip(pp_buffer_t *frame);
bool pp_picture_active(void);
bool pp_picture_disconnected(void);
unsigned pp_picture_displayed_bytes(void);
bool pp_picture_poll(uint32_t now); /* True when a foreground transfer failed. */
void pp_picture_cached(pp_cache_result_t *result);

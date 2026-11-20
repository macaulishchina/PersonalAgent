#pragma once
#include "timeline.h"
#include "object_cache.h"
#include "image_model.h"

typedef void (*pp_history_page_fn)(pp_cache_status_t status, const pp_history_page_t *page);
void pp_history_cache_init(pp_history_page_fn page, void (*warning)(const char *));
void pp_history_cache_bind(const char *url, const char *device, const char *binding, bool enabled,
                           bool images);
void pp_history_cache_record(const pp_history_page_t *page, pp_buffer_t *frame);
bool pp_history_cache_preview(const char *key, const char *group, pp_buffer_t *pixels, const pp_image_transfer_t *meta);
bool pp_history_cache_read(const char *id, double revision, bool legacy, unsigned offset,
                           pp_cache_seek_t seek);
void pp_history_cache_poll(bool writable);
bool pp_history_cache_image_key(const char *message, double revision, const char *image, char key[256]);

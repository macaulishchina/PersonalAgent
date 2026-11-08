#pragma once
#include "cJSON.h"
#include "cache_disk.h"
#include <stdbool.h>

/* Data access, shared by live chat and historical browsing. Callback only queues
 * a bounded application event, identical to a network message.page. */
void pp_history_cache_init(void (*receive)(const char *, size_t));
void pp_history_cache_bind(const char *url, const char *device, const char *binding, bool enabled,
                           bool images);
bool pp_history_cache_receive(cJSON *message);
bool pp_history_cache_page(const char *id, unsigned offset);
bool pp_history_cache_recent(void);
void pp_history_cache_poll(bool idle);
bool pp_history_cache_revision(const char *id, double *revision);
/* Image access shares the same revision/namespace and storage budget. */
bool pp_history_cache_image_key(const char *message, const char *image, char key[256]);
bool pp_history_cache_owner(const char *message, char owner[192]);

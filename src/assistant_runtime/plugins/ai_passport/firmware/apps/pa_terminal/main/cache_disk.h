#pragma once
#include "cache_store.h"
#include <stdbool.h>
#include <stddef.h>

typedef struct {
    unsigned ticket, generation;
    bool hit, written;
    pp_cache_value_t value;
} pp_disk_result_t;
typedef struct {
    bool ready;
    unsigned hits, misses, write_failures;
} pp_disk_info_t;
/* Nonblocking application-side queue. The low-priority I/O owner alone mounts,
 * reads, commits and evicts files. A cache failure never aborts conversation. */
bool pp_disk_start(void);
bool pp_disk_identity(const char *url, const char *device, const char *binding, char identity[65]);
void pp_disk_configure(const char *scope, const char *const owners[], unsigned count, bool enabled);
bool pp_disk_read(const char *key, unsigned ticket);
bool pp_disk_write(const char *key, const char *owner, bool index, const void *data, size_t bytes);
bool pp_disk_write_confirm(const char *key, const char *owner, bool index, const void *data,
                           size_t bytes, unsigned ticket);
/* Consumes data on every path; lets a closed thumbnail transfer ownership without
 * allocating a second pixel buffer. aux is copied into the bounded queue item. */
bool pp_disk_write_take(const char *key, const char *owner, bool index, void *data, size_t bytes,
                        const void *aux, size_t aux_size);
bool pp_disk_result(pp_disk_result_t *result);
bool pp_disk_enabled(void);
void pp_disk_allow_writes(bool quiet);
pp_disk_info_t pp_disk_info(void);

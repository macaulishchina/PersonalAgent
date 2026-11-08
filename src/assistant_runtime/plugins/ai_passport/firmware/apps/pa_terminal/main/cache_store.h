#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PP_CACHE_BUDGET (256u * 1024u)
#define PP_CACHE_OBJECTS 128u
#define PP_CACHE_VALUE_MAX 32768u
#define PP_CACHE_AUX_MAX 96u
#define PP_CACHE_RECENT 3u

/* One I/O owner. POSIX files on LittleFS on target; ordinary private directory
 * in host tests. Cached data is disposable, never execution state. */
typedef struct {
    char root[96];
    unsigned char scope[16], pins[PP_CACHE_RECENT][16];
    unsigned pin_count;
    uint64_t sequence;
    size_t budget;
} pp_cache_store_t;
typedef struct {
    void *data;
    size_t size, aux_size;
    unsigned char aux[PP_CACHE_AUX_MAX];
} pp_cache_value_t;
bool pp_cache_store_open(pp_cache_store_t *store, const char *root, size_t budget);
void pp_cache_store_scope(pp_cache_store_t *store, const unsigned char scope[16],
                          const unsigned char pins[][16], unsigned count);
bool pp_cache_store_get(pp_cache_store_t *store, const char key[65], pp_cache_value_t *value);
bool pp_cache_store_get_limit(pp_cache_store_t *store, const char key[65], size_t limit,
                              pp_cache_value_t *value);
bool pp_cache_store_put(pp_cache_store_t *store, const char key[65], const unsigned char owner[16],
                        bool index, const void *data, size_t size, const void *aux,
                        size_t aux_size);
bool pp_cache_store_clear(pp_cache_store_t *store);
void pp_cache_value_free(pp_cache_value_t *value);

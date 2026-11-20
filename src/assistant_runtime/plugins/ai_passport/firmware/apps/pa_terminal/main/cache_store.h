#pragma once
#include "object_cache.h"

#define PP_CACHE_BUDGET (512u * 1024u)
#define PP_CACHE_OBJECTS 128u

/* One I/O owner; the payload format and layout remain compatible with older firmware. */
typedef struct {
    char root[96];
    unsigned char scope[16], pins[PP_CACHE_PIN_LIMIT][16];
    unsigned pin_count;
    uint64_t sequence;
    size_t budget, used; /* Committed objects, charged in the same units as admission. */
    bool (*allowed)(void *context);
    void *context;
} pp_cache_store_t;
bool pp_cache_store_open(pp_cache_store_t *store, const char *root, size_t budget);
bool pp_cache_store_recover(pp_cache_store_t *store);
void pp_cache_store_scope(pp_cache_store_t *store, const unsigned char scope[16],
                          const unsigned char pins[][16], unsigned count);
pp_cache_status_t pp_cache_store_read(pp_cache_store_t *store, const char key[65],
                                     pp_cache_value_t *value,
                                     bool (*reserve)(pp_buffer_t *, size_t, void *), void *context);
pp_cache_status_t pp_cache_store_write(pp_cache_store_t *store, const char key[65],
                                       const unsigned char owner[16], bool index,
                                       const void *data, size_t size, const void *aux,
                                       size_t aux_size, size_t reserve);
bool pp_cache_store_put(pp_cache_store_t *store, const char key[65], const unsigned char owner[16],
                        bool index, const void *data, size_t size, const void *aux,
                        size_t aux_size);
bool pp_cache_store_clear(pp_cache_store_t *store);

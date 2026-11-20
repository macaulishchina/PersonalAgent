#pragma once
#include "object_cache.h"

/* Reserve sixteen erase blocks for metadata, the staged value and the prune transaction. */
#define PP_CACHE_BUDGET ((1024u - 64u) * 1024u)
#define PP_CACHE_OBJECTS (PP_CACHE_BUDGET / 4096u)

/* One I/O owner; the payload format and layout remain compatible with older firmware. */
typedef struct {
    char root[96];
    unsigned char scope[16];
    /* Derived, compact directory of current-binding heads. Keep binary hashes
     * truncated here only: the chosen file's full header and sequence must match. */
    uint32_t head_order[PP_CACHE_OBJECTS];
    uint32_t head_tag[PP_CACHE_OBJECTS];
    unsigned heads;
    bool indexed, collision;
    uint64_t sequence;
    size_t budget, used; /* Committed objects, charged in the same units as admission. */
    bool (*available)(size_t *free_bytes); /* Physical filesystem space including metadata. */
    bool (*allowed)(void *context);
    void *context;
} pp_cache_store_t;
bool pp_cache_store_open(pp_cache_store_t *store, const char *root, size_t budget);
bool pp_cache_store_recover(pp_cache_store_t *store);
void pp_cache_store_scope(pp_cache_store_t *store, const unsigned char scope[16]);
pp_cache_status_t pp_cache_store_seek(pp_cache_store_t *store, char key[65], pp_cache_seek_t seek, uint64_t *cursor);
pp_cache_status_t pp_cache_store_read(pp_cache_store_t *store, const char key[65],
                                     pp_cache_value_t *value,
                                     bool (*reserve)(pp_buffer_t *, size_t, void *), void *context);
pp_cache_status_t pp_cache_store_write(pp_cache_store_t *store, const char key[65],
                                       const unsigned char owner[16], pp_object_kind_t kind,
                                       const void *data, size_t size, const void *aux, size_t aux_size,
                                       size_t reserve);
pp_cache_status_t pp_cache_store_write_ordered(pp_cache_store_t *store, const char key[65],
                                               const unsigned char owner[16], pp_object_kind_t kind,
                                               const void *data, size_t size, const void *aux,
                                               size_t aux_size, size_t reserve, uint64_t import_order);
bool pp_cache_store_put(pp_cache_store_t *store, const char key[65], const unsigned char owner[16],
                        bool index, const void *data, size_t size, const void *aux,
                        size_t aux_size);
bool pp_cache_store_clear(pp_cache_store_t *store);

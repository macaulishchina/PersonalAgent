#pragma once
#include "object_cache.h"

/* Caller serializes access. Slots are bounded and keep pixel/aux bytes immutable
 * until all independent lease tickets have been released. */
typedef struct {
    char key[65];
    pp_cache_value_t value;
    unsigned last_used, loans[4];
    pp_memory_priority_t priority;
} pp_cache_slot_t;
typedef struct {
    pp_cache_slot_t slots[2];
    unsigned clock;
} pp_cache_resident_t;

bool pp_resident_borrow(pp_cache_resident_t *cache, const char *key, pp_cache_result_t *result);
bool pp_resident_retain(pp_cache_resident_t *cache, unsigned lease, pp_cache_result_t *result);
void pp_resident_release(pp_cache_resident_t *cache, unsigned lease);
void pp_resident_invalidate(pp_cache_resident_t *cache, const char *key);
pp_buffer_t pp_resident_remove(pp_cache_resident_t *cache, pp_memory_priority_t priority,
                                 size_t exact_capacity);
bool pp_resident_insert(pp_cache_resident_t *cache, const char *key, pp_cache_value_t *value,
                         pp_memory_priority_t priority);
size_t pp_resident_recoverable(const pp_cache_resident_t *cache);

#include "cache_resident.h"
#include <string.h>

static bool borrowed(const pp_cache_slot_t *slot) {
    return slot->loans[0] || slot->loans[1] || slot->loans[2] || slot->loans[3];
}
static bool borrow(pp_cache_resident_t *cache, pp_cache_slot_t *slot, pp_cache_result_t *result) {
    for (unsigned i = 0; i < sizeof(slot->loans) / sizeof(*slot->loans); i++) {
        if (slot->loans[i])
            continue;
        if (!++cache->clock)
            ++cache->clock;
        result->lease = slot->loans[i] = slot->last_used = cache->clock;
        result->value = slot->value;
        return true;
    }
    return false;
}
bool pp_resident_borrow(pp_cache_resident_t *cache, const char *key, pp_cache_result_t *result) {
    for (unsigned i = 0; i < sizeof(cache->slots) / sizeof(*cache->slots); i++)
        if (cache->slots[i].key[0] && !strcmp(cache->slots[i].key, key))
            return borrow(cache, &cache->slots[i], result);
    return false;
}
bool pp_resident_retain(pp_cache_resident_t *cache, unsigned lease, pp_cache_result_t *result) {
    if (lease)
        for (unsigned i = 0; i < sizeof(cache->slots) / sizeof(*cache->slots); i++)
            for (unsigned j = 0; j < sizeof(cache->slots[i].loans) / sizeof(unsigned); j++)
                if (cache->slots[i].loans[j] == lease)
                    return borrow(cache, &cache->slots[i], result);
    return false;
}
void pp_resident_release(pp_cache_resident_t *cache, unsigned lease) {
    if (!lease)
        return;
    for (unsigned i = 0; i < sizeof(cache->slots) / sizeof(*cache->slots); i++)
        for (unsigned j = 0; j < sizeof(cache->slots[i].loans) / sizeof(unsigned); j++)
            if (cache->slots[i].loans[j] == lease) {
                cache->slots[i].loans[j] = 0;
                return;
            }
}
void pp_resident_invalidate(pp_cache_resident_t *cache, const char *key) {
    for (unsigned i = 0; i < sizeof(cache->slots) / sizeof(*cache->slots); i++)
        if (!key || !strcmp(cache->slots[i].key, key))
            cache->slots[i].key[0] = 0;
}
pp_buffer_t pp_resident_remove(pp_cache_resident_t *cache, pp_memory_priority_t priority,
                                 size_t exact_capacity) {
    pp_cache_slot_t *victim = NULL;
    for (unsigned i = 0; i < sizeof(cache->slots) / sizeof(*cache->slots); i++) {
        pp_cache_slot_t *slot = &cache->slots[i];
        if (!slot->value.bytes.data || borrowed(slot) || (slot->key[0] && slot->priority > priority) ||
            (exact_capacity && slot->value.bytes.capacity != exact_capacity))
            continue;
        if (!victim || !slot->key[0] || (victim->key[0] &&
            (slot->priority < victim->priority || (slot->priority == victim->priority &&
             (int32_t)(slot->last_used - victim->last_used) < 0))))
            victim = slot;
    }
    if (!victim)
        return (pp_buffer_t){0};
    pp_buffer_t released = pp_buffer_take(&victim->value.bytes);
    victim->key[0] = 0;
    return released;
}
bool pp_resident_insert(pp_cache_resident_t *cache, const char *key, pp_cache_value_t *value,
                         pp_memory_priority_t priority) {
    pp_cache_slot_t *free_slot = NULL;
    size_t bytes = value->bytes.capacity;
    for (unsigned i = 0; i < sizeof(cache->slots) / sizeof(*cache->slots); i++) {
        pp_cache_slot_t *slot = &cache->slots[i];
        if (!slot->value.bytes.data)
            free_slot = slot;
        if (slot->key[0] && !strcmp(slot->key, key))
            return false;
        bytes += slot->value.bytes.capacity;
    }
    if (!free_slot || bytes > PP_CACHE_RESIDENT_BYTES)
        return false;
    strcpy(free_slot->key, key);
    free_slot->value = *value;
    free_slot->priority = priority;
    free_slot->last_used = ++cache->clock;
    *value = (pp_cache_value_t){0};
    return true;
}
size_t pp_resident_recoverable(const pp_cache_resident_t *cache) {
    size_t bytes = 0;
    for (unsigned i = 0; i < sizeof(cache->slots) / sizeof(*cache->slots); i++)
        if (!borrowed(&cache->slots[i]))
            bytes += cache->slots[i].value.bytes.capacity;
    return bytes;
}

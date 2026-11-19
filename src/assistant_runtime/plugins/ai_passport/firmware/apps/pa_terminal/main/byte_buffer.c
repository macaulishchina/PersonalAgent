#include "byte_buffer.h"
#include <stdint.h>
#include <stdatomic.h>
#ifdef ESP_PLATFORM
#include "esp_heap_caps.h"
#endif
#include <stdlib.h>
#include <string.h>

static atomic_uint live[PP_MEMORY_USES], peak[PP_MEMORY_USES], failed[PP_MEMORY_USES];
static atomic_uint waited, expired;
static pp_buffer_t (*reuse_memory)(size_t, pp_memory_priority_t);
static void (*reclaim_memory)(size_t, pp_memory_priority_t);

static void charge(pp_memory_use_t use, size_t capacity) {
    if (!use)
        return;
    unsigned current = atomic_fetch_add(&live[use], capacity) + capacity;
    unsigned prior = atomic_load(&peak[use]);
    while (prior < current && !atomic_compare_exchange_weak(&peak[use], &prior, current)) {}
}
void pp_memory_on_pressure(pp_buffer_t (*take)(size_t, pp_memory_priority_t),
                           void (*reclaim)(size_t, pp_memory_priority_t)) {
    reuse_memory = take;
    reclaim_memory = reclaim;
}
void pp_memory_reclaim(size_t capacity, pp_memory_priority_t priority) {
    if (capacity && reclaim_memory && priority != PP_MEMORY_BACKGROUND)
        reclaim_memory(capacity, priority);
}
bool pp_buffer_reserve(pp_buffer_t *b, size_t capacity, pp_memory_priority_t priority,
                       pp_memory_use_t use) {
    if (!capacity || capacity < b->size)
        return false;
    if (b->capacity == capacity)
        return true;
    char *data = realloc(b->data, capacity);
    if (!data && !b->data && reuse_memory && priority != PP_MEMORY_BACKGROUND) {
        pp_buffer_t reused = reuse_memory(capacity, priority);
        if (reused.data) {
            data = reused.data;
            memset(data, 0, capacity);
            if (reused.use)
                atomic_fetch_sub(&live[reused.use], reused.capacity);
        }
    }
    if (!data && reclaim_memory && priority != PP_MEMORY_BACKGROUND) {
        pp_memory_reclaim(capacity, priority);
        data = realloc(b->data, capacity);
    }
    if (!data) {
        atomic_fetch_add(&failed[use], 1);
        return false;
    }
    if (b->use)
        atomic_fetch_sub(&live[b->use], b->capacity);
    b->data = data;
    b->capacity = capacity;
    b->use = use;
    charge(use, capacity);
    return true;
}
pp_buffer_t pp_buffer_adopt(void *data, size_t size, size_t capacity, pp_memory_use_t use) {
    if (!data)
        return (pp_buffer_t){0};
#ifdef ESP_PLATFORM
    /* cJSON may keep its larger allocation when shrinking it fails. */
    capacity = heap_caps_get_allocated_size(data);
#endif
    charge(use, capacity);
    return (pp_buffer_t){.data = data, .size = size, .capacity = capacity, .use = use};
}
pp_buffer_t pp_buffer_take(pp_buffer_t *b) {
    pp_buffer_t owned = *b;
    *b = (pp_buffer_t){0};
    return owned;
}
void pp_buffer_set_use(pp_buffer_t *b, pp_memory_use_t use) {
    if (!b->data || b->use == use)
        return;
    if (b->use)
        atomic_fetch_sub(&live[b->use], b->capacity);
    b->use = use;
    charge(use, b->capacity);
}
bool pp_buffers_acquire(pp_buffer_t *blocks, size_t count, size_t bytes,
                        pp_memory_priority_t priority, pp_memory_use_t use) {
    for (size_t i = 0; i < count; i++) {
        if (!pp_buffer_reserve(&blocks[i], bytes, priority, use)) {
            pp_buffers_release(blocks, i);
            return false;
        }
    }
    return true;
}
void pp_buffers_release(pp_buffer_t *blocks, size_t count) {
    for (size_t i = 0; i < count; i++)
        pp_buffer_free(&blocks[i]);
}
pp_memory_stats_t pp_memory_stats(void) {
    pp_memory_stats_t s = {.waits = atomic_load(&waited), .expired = atomic_load(&expired)};
    for (unsigned i = 0; i < PP_MEMORY_USES; i++) {
        s.live[i] = atomic_load(&live[i]);
        s.peak[i] = atomic_load(&peak[i]);
        s.failures[i] = atomic_load(&failed[i]);
    }
    return s;
}

bool pp_buffer_append(pp_buffer_t *b, const void *data, size_t bytes, size_t limit) {
    if (!data || !bytes || limit == SIZE_MAX || b->size > limit || bytes > limit - b->size)
        return false;
    size_t needed = b->size + bytes + 1;
    if (needed > b->capacity) {
        size_t capacity = b->capacity ? b->capacity : 128;
        while (capacity < needed) {
            if (capacity > (limit + 1) / 2) {
                capacity = limit + 1;
                break;
            }
            capacity *= 2;
        }
        /* Include the terminator when growth reaches the payload limit. */
        if (capacity >= limit)
            capacity = limit + 1;
        if (!pp_buffer_reserve(b, capacity, PP_MEMORY_CRITICAL, PP_MEMORY_WIRE))
            return false;
    }
    memcpy(b->data + b->size, data, bytes);
    b->size += bytes;
    b->data[b->size] = 0;
    return true;
}

void pp_buffer_free(pp_buffer_t *b) {
    if (b->data) {
        volatile unsigned char *p = (volatile unsigned char *)b->data;
        for (size_t i = 0; i < b->capacity; i++)
            p[i] = 0;
        free(b->data);
        if (b->use)
            atomic_fetch_sub(&live[b->use], b->capacity);
    }
    *b = (pp_buffer_t){0};
}

typedef struct {
    pp_memory_done_t done;
    size_t capacity;
    uint32_t deadline;
    unsigned ticket;
    pp_memory_priority_t priority;
    pp_memory_use_t use;
} wait_t;
static wait_t waiting[3];
static unsigned sequence;
static uint32_t retry_at;
unsigned pp_memory_wait(size_t capacity, pp_memory_priority_t priority, pp_memory_use_t use,
                        uint32_t deadline, pp_memory_done_t done) {
    if (!capacity || !done)
        return 0;
    for (unsigned i = 0; i < sizeof(waiting) / sizeof(*waiting); i++) {
        if (waiting[i].done)
            continue;
        if (!++sequence)
            ++sequence;
        waiting[i] = (wait_t){done, capacity, deadline, sequence, priority, use};
        atomic_fetch_add(&waited, 1);
        return sequence;
    }
    return 0;
}
static void complete(wait_t *wait, pp_buffer_t buffer) {
    wait_t item = *wait;
    wait->done = NULL;
    item.done(item.ticket, buffer);
}
void pp_memory_cancel(unsigned ticket) {
    for (unsigned i = 0; i < sizeof(waiting) / sizeof(*waiting); i++)
        if (waiting[i].done && waiting[i].ticket == ticket) {
            complete(&waiting[i], (pp_buffer_t){0});
            return;
        }
}
void pp_memory_poll(uint32_t now) {
    for (unsigned i = 0; i < sizeof(waiting) / sizeof(*waiting); i++) {
        wait_t *item = &waiting[i];
        if (!item->done)
            continue;
        if ((int32_t)(now - item->deadline) >= 0) {
            atomic_fetch_add(&expired, 1);
            complete(item, (pp_buffer_t){0});
        }
    }
    wait_t *next = NULL;
    for (unsigned i = 0; i < sizeof(waiting) / sizeof(*waiting); i++) {
        wait_t *item = &waiting[i];
        if (item->done && (int32_t)(now - item->deadline) < 0 &&
            (!next || item->priority > next->priority ||
             (item->priority == next->priority && (int32_t)(item->ticket - next->ticket) < 0)))
            next = item;
    }
    if (!next || (retry_at && (int32_t)(now - retry_at) < 0))
        return;
    pp_buffer_t buffer = {0};
    if (pp_buffer_reserve(&buffer, next->capacity, next->priority, next->use)) {
        retry_at = 0;
        complete(next, buffer);
    } else
        retry_at = now + 50;
}

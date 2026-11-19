#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum { PP_MEMORY_BACKGROUND, PP_MEMORY_NORMAL, PP_MEMORY_CRITICAL } pp_memory_priority_t;
typedef enum {
    PP_MEMORY_UNTRACKED, PP_MEMORY_WIRE, PP_MEMORY_AUDIO, PP_MEMORY_IMAGE, PP_MEMORY_CACHE,
    PP_MEMORY_CAPTURE, PP_MEMORY_PLAYBACK, PP_MEMORY_NETWORK_SETUP, PP_MEMORY_TLS_CERT,
    PP_MEMORY_USES
} pp_memory_use_t;

typedef struct {
    char *data;
    size_t size, capacity;
    pp_memory_use_t use;
} pp_buffer_t;

/* Allocates only when data arrives; the limit excludes the terminating zero. */
bool pp_buffer_append(pp_buffer_t *buffer, const void *data, size_t bytes, size_t limit);
void pp_buffer_free(pp_buffer_t *buffer);

/* On failure the current owner still holds its original buffer. */
bool pp_buffer_reserve(pp_buffer_t *buffer, size_t capacity, pp_memory_priority_t priority,
                       pp_memory_use_t use);
pp_buffer_t pp_buffer_adopt(void *data, size_t size, size_t capacity, pp_memory_use_t use);
pp_buffer_t pp_buffer_take(pp_buffer_t *buffer);
void pp_buffer_set_use(pp_buffer_t *buffer, pp_memory_use_t use);
/* Borrow a full set of independently allocated blocks, rolling back on failure.
 * The caller owns an empty array until the complete set can be published. */
bool pp_buffers_acquire(pp_buffer_t *blocks, size_t count, size_t bytes,
                        pp_memory_priority_t priority, pp_memory_use_t use);
void pp_buffers_release(pp_buffer_t *blocks, size_t count);

/* Register once before starting worker tasks. Reclaim only unborrowed memory. */
void pp_memory_on_pressure(pp_buffer_t (*take)(size_t, pp_memory_priority_t),
                           void (*reclaim)(size_t, pp_memory_priority_t));
void pp_memory_reclaim(size_t capacity, pp_memory_priority_t priority);
typedef struct {
    unsigned live[PP_MEMORY_USES], peak[PP_MEMORY_USES], failures[PP_MEMORY_USES], waits, expired;
} pp_memory_stats_t;
pp_memory_stats_t pp_memory_stats(void);

/* The controller owns this bounded queue. Completion callbacks consume their
 * buffer on success. A cancelled or timed-out request receives an empty buffer. */
typedef void (*pp_memory_done_t)(unsigned ticket, pp_buffer_t buffer);
unsigned pp_memory_wait(size_t capacity, pp_memory_priority_t priority, pp_memory_use_t use,
                        uint32_t deadline, pp_memory_done_t done);
void pp_memory_cancel(unsigned ticket);
void pp_memory_poll(uint32_t now);

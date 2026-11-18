#pragma once
#include "terminal_limits.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Protocol credit counts PCM bytes. One I2S write may be outstanding.
 * The adapter serializes these operations; codec I/O runs outside the lock. */
typedef struct {
    uint8_t pcm[PP_PLAYBACK_WINDOW];
    size_t head, queued, writing, credit;
    uint32_t generation, epoch;
    bool active;
} pp_playback_buffer_t;

void pp_playback_reset(pp_playback_buffer_t *b, uint32_t generation, bool active);
bool pp_playback_append(pp_playback_buffer_t *b, uint32_t generation, const void *pcm, size_t bytes);
size_t pp_playback_take(pp_playback_buffer_t *b, void *pcm, size_t capacity, uint32_t *epoch);
void pp_playback_written(pp_playback_buffer_t *b, uint32_t epoch, bool success);

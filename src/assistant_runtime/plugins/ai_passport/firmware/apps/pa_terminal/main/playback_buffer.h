#pragma once
#include "byte_buffer.h"
#include "terminal_limits.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PP_PLAYBACK_BLOCK_BYTES 2048u
#define PP_PLAYBACK_BLOCKS (PP_PLAYBACK_MAX_WINDOW / PP_PLAYBACK_BLOCK_BYTES)
#define PP_PLAYBACK_PREFILL_BYTES (PP_PLAYBACK_RATE * 2u / 10u)

typedef struct {
    uint32_t generation, window_bytes;
    uint32_t received_bytes, received_crc32, i2s_bytes, i2s_crc32, underruns;
} pp_playback_report_t;

/* Protocol credit counts PCM bytes. One I2S write may be outstanding.
 * The adapter serializes these operations; codec I/O runs outside the lock. */
typedef struct {
    pp_buffer_t blocks[PP_PLAYBACK_BLOCKS];
    size_t head, queued, writing, credit;
    pp_playback_report_t report;
    uint32_t epoch;
    bool active, priming, ended;
} pp_playback_buffer_t;

void pp_playback_reset(pp_playback_buffer_t *b, uint32_t generation, size_t capacity);
bool pp_playback_append(pp_playback_buffer_t *b, uint32_t generation, const void *pcm, size_t bytes);
void pp_playback_finish(pp_playback_buffer_t *b, uint32_t generation);
size_t pp_playback_take(pp_playback_buffer_t *b, void *pcm, size_t capacity,
                        bool dma_empty, uint32_t *epoch);
void pp_playback_written(pp_playback_buffer_t *b, uint32_t epoch, const void *pcm,
                         size_t written, bool success);

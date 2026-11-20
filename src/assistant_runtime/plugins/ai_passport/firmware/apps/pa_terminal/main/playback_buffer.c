#include "playback_buffer.h"
#include <string.h>

_Static_assert(PP_PLAYBACK_WINDOW % PP_PLAYBACK_BLOCK_BYTES == 0, "Whole playback blocks");
_Static_assert(PP_PLAYBACK_PREFILL_BYTES <= PP_PLAYBACK_WINDOW, "Prefill fits the credit window");

static size_t block_span(size_t position, size_t remaining) {
    size_t span = PP_PLAYBACK_BLOCK_BYTES - position % PP_PLAYBACK_BLOCK_BYTES;
    return span < remaining ? span : remaining;
}
static char *block_at(pp_playback_buffer_t *b, size_t position) {
    return b->blocks[position / PP_PLAYBACK_BLOCK_BYTES].data + position % PP_PLAYBACK_BLOCK_BYTES;
}

void pp_playback_reset(pp_playback_buffer_t *b, uint32_t generation, bool active) {
    b->epoch++;
    b->generation = generation;
    b->active = active;
    b->priming = active;
    b->ended = false;
    b->head = b->queued = b->writing = b->credit = 0;
}
bool pp_playback_append(pp_playback_buffer_t *b, uint32_t generation, const void *pcm, size_t bytes) {
    if (!b->active || b->ended || generation != b->generation)
        return true;
    if (!pcm || !bytes || bytes % 2 || bytes > PP_PLAYBACK_WINDOW - b->queued - b->writing)
        return false;
    size_t tail = (b->head + b->queued) % PP_PLAYBACK_WINDOW;
    const unsigned char *source = pcm;
    for (size_t copied = 0; copied < bytes;) {
        size_t n = block_span(tail, bytes - copied);
        memcpy(block_at(b, tail), source + copied, n);
        copied += n;
        tail = (tail + n) % PP_PLAYBACK_WINDOW;
    }
    b->queued += bytes;
    return true;
}
void pp_playback_finish(pp_playback_buffer_t *b, uint32_t generation) {
    if (b->active && b->generation == generation)
        b->ended = true;
}
size_t pp_playback_take(pp_playback_buffer_t *b, void *pcm, size_t capacity,
                        bool dma_empty, uint32_t *epoch) {
    if (!b->active || b->writing)
        return 0;
    if (dma_empty)
        b->priming = true;
    if (b->priming && !b->ended && b->queued < PP_PLAYBACK_PREFILL_BYTES)
        return 0;
    size_t n = capacity < b->queued ? capacity : b->queued;
    if (n > PP_PLAYBACK_WINDOW - b->head)
        n = PP_PLAYBACK_WINDOW - b->head;
    n &= ~(size_t)1;
    if (!n)
        return 0;
    b->priming = false;
    unsigned char *target = pcm;
    for (size_t copied = 0; copied < n;) {
        size_t pos = b->head + copied;
        size_t span = block_span(pos, n - copied);
        memcpy(target + copied, block_at(b, pos), span);
        copied += span;
    }
    b->head = (b->head + n) % PP_PLAYBACK_WINDOW;
    b->queued -= n;
    b->writing = n;
    *epoch = b->epoch;
    return n;
}
void pp_playback_written(pp_playback_buffer_t *b, uint32_t epoch, bool success) {
    if (epoch != b->epoch)
        return;
    if (success)
        b->credit += b->writing;
    else
        b->active = false;
    b->writing = 0;
}

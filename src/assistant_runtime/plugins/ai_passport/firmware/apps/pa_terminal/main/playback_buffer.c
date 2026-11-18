#include "playback_buffer.h"
#include <string.h>

void pp_playback_reset(pp_playback_buffer_t *b, uint32_t generation, bool active) {
    b->epoch++;
    b->generation = generation;
    b->active = active;
    b->head = b->queued = b->writing = b->credit = 0;
}
bool pp_playback_append(pp_playback_buffer_t *b, uint32_t generation, const void *pcm, size_t bytes) {
    if (!b->active || generation != b->generation)
        return true;
    if (!pcm || !bytes || bytes % 2 || bytes > PP_PLAYBACK_WINDOW - b->queued - b->writing)
        return false;
    size_t tail = (b->head + b->queued) % PP_PLAYBACK_WINDOW;
    size_t first = PP_PLAYBACK_WINDOW - tail;
    if (first > bytes)
        first = bytes;
    memcpy(b->pcm + tail, pcm, first);
    memcpy(b->pcm, (const uint8_t *)pcm + first, bytes - first);
    b->queued += bytes;
    return true;
}
size_t pp_playback_take(pp_playback_buffer_t *b, void *pcm, size_t capacity, uint32_t *epoch) {
    if (!b->active || b->writing)
        return 0;
    size_t n = capacity < b->queued ? capacity : b->queued;
    if (n > PP_PLAYBACK_WINDOW - b->head)
        n = PP_PLAYBACK_WINDOW - b->head;
    n &= ~(size_t)1;
    memcpy(pcm, b->pcm + b->head, n);
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

#include "playback_buffer.h"
#include <string.h>
#ifdef ESP_PLATFORM
#include "esp_rom_crc.h"
#endif

_Static_assert(PP_PLAYBACK_WINDOW % PP_PLAYBACK_BLOCK_BYTES == 0, "Whole playback blocks");
_Static_assert(PP_PLAYBACK_MAX_WINDOW % PP_PLAYBACK_BLOCK_BYTES == 0, "Whole playback blocks");
_Static_assert(PP_PLAYBACK_PREFILL_BYTES <= PP_PLAYBACK_WINDOW, "Prefill fits the credit window");

static uint32_t pcm_crc32(uint32_t crc, const uint8_t *pcm, size_t bytes) {
#ifdef ESP_PLATFORM
    return esp_rom_crc32_le(crc, pcm, bytes);
#else
    crc = ~crc;
    for (size_t i = 0; i < bytes; i++) {
        crc ^= pcm[i];
        for (unsigned bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ ((crc & 1u) ? 0xedb88320u : 0);
    }
    return ~crc;
#endif
}

static size_t block_span(size_t position, size_t remaining) {
    size_t span = PP_PLAYBACK_BLOCK_BYTES - position % PP_PLAYBACK_BLOCK_BYTES;
    return span < remaining ? span : remaining;
}
static char *block_at(pp_playback_buffer_t *b, size_t position) {
    return b->blocks[position / PP_PLAYBACK_BLOCK_BYTES].data + position % PP_PLAYBACK_BLOCK_BYTES;
}

void pp_playback_reset(pp_playback_buffer_t *b, uint32_t generation, size_t capacity) {
    b->epoch++;
    b->report = (pp_playback_report_t){.generation = generation, .window_bytes = capacity};
    b->active = b->priming = capacity != 0;
    b->ended = false;
    b->head = b->queued = b->writing = b->credit = 0;
}
bool pp_playback_append(pp_playback_buffer_t *b, uint32_t generation, const void *pcm, size_t bytes) {
    if (!b->active || b->ended || generation != b->report.generation)
        return true;
    size_t window = b->report.window_bytes;
    if (!pcm || !bytes || bytes % 2 || bytes > window - b->queued - b->writing)
        return false;
    size_t tail = (b->head + b->queued) % window;
    const unsigned char *source = pcm;
    for (size_t copied = 0; copied < bytes;) {
        size_t n = block_span(tail, bytes - copied);
        memcpy(block_at(b, tail), source + copied, n);
        copied += n;
        tail = (tail + n) % window;
    }
    b->queued += bytes;
    b->report.received_bytes += bytes;
    b->report.received_crc32 = pcm_crc32(b->report.received_crc32, pcm, bytes);
    return true;
}
void pp_playback_finish(pp_playback_buffer_t *b, uint32_t generation) {
    if (b->active && b->report.generation == generation)
        b->ended = true;
}
size_t pp_playback_take(pp_playback_buffer_t *b, void *pcm, size_t capacity,
                        bool dma_empty, uint32_t *epoch) {
    if (!b->active || b->writing)
        return 0;
    if (dma_empty && !b->priming && !b->ended) {
        b->report.underruns++;
        b->priming = true;
    }
    size_t window = b->report.window_bytes;
    size_t prefill = window > PP_PLAYBACK_WINDOW ? window - PP_PLAYBACK_BLOCK_BYTES
                                                : PP_PLAYBACK_PREFILL_BYTES;
    if (b->priming && !b->ended && b->queued < prefill)
        return 0;
    size_t n = capacity < b->queued ? capacity : b->queued;
    if (n > window - b->head)
        n = window - b->head;
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
    b->head = (b->head + n) % window;
    b->queued -= n;
    b->writing = n;
    *epoch = b->epoch;
    return n;
}
void pp_playback_written(pp_playback_buffer_t *b, uint32_t epoch, const void *pcm,
                         size_t written, bool success) {
    if (epoch != b->epoch)
        return;
    if (written > b->writing)
        written = b->writing;
    if (written) {
        b->report.i2s_bytes += written;
        b->report.i2s_crc32 = pcm_crc32(b->report.i2s_crc32, pcm, written);
    }
    if (success && written == b->writing)
        b->credit += b->writing;
    else
        b->active = false;
    b->writing = 0;
}

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

void pp_playback_reset(pp_playback_buffer_t *b, uint32_t generation, size_t capacity) {
    b->epoch++;
    b->report = (pp_playback_report_t){.generation = generation, .window_bytes = capacity};
    b->active = b->priming = capacity != 0;
    b->ended = b->repeated_gap = false;
    b->last_underrun_i2s = 0;
    b->prefill_first_bytes = 0;
    b->queued = b->writing_ram = b->writing = b->credit = 0;
    pp_stream_reset(&b->queue, b->blocks, PP_PLAYBACK_BLOCK_BYTES, capacity);
}
bool pp_playback_append(pp_playback_buffer_t *b, uint32_t generation, const void *pcm, size_t bytes) {
    if (!b->active || b->ended || generation != b->report.generation)
        return true;
    if (!pcm || !bytes || bytes % 2 || bytes + b->writing_ram > pp_stream_room(&b->queue) ||
        !pp_stream_append(&b->queue, pcm, bytes))
        return false;
    b->queued += bytes;
    b->report.received_bytes += bytes;
    b->report.received_crc32 = pcm_crc32(b->report.received_crc32, pcm, bytes);
    return true;
}
void pp_playback_finish(pp_playback_buffer_t *b, uint32_t generation) {
    if (b->active && b->report.generation == generation)
        b->ended = true;
}
/* Sample only while accumulating PCM: synthesis startup and time between
 * playback requests should not influence the next utterance. */
static bool prefilled(pp_playback_buffer_t *b, uint32_t now_ms) {
    if (!b->queued)
        return false;
    if (!b->prefill_first_bytes) {
        b->prefill_started_ms = now_ms;
        b->prefill_first_bytes = b->queued;
    }
    uint32_t elapsed = now_ms - b->prefill_started_ms;
    size_t window = b->report.window_bytes;
    size_t normal = window > PP_PLAYBACK_WINDOW ? window - PP_PLAYBACK_BLOCK_BYTES
                                                : PP_PLAYBACK_PREFILL_BYTES;
    if (b->queued < normal)
        return false;
    const unsigned bytes_per_second = PP_PLAYBACK_RATE * 2u;
    /* Ignore sub-packet timing jitter. A sustained rate below 80% of PCM
     * consumption warrants a longer contiguous run. A second underrun also
     * catches bursty sources that briefly refill at full line speed. */
    size_t arrived = b->queued - b->prefill_first_bytes;
    uint64_t delivered = (uint64_t)arrived * 1000;
    uint64_t realtime = (uint64_t)elapsed * bytes_per_second;
    bool slow = elapsed >= 120 && delivered < realtime * 4 / 5;
    if (!slow && !b->repeated_gap)
        return true;
    /* Stop the additional wait on EOF (caller), a time bound, or when the
     * FIFO cannot accept any more. RAM-only peers must always make progress. */
    size_t target = bytes_per_second * (b->repeated_gap ? 3u : 2u);
    size_t limit = pp_stream_capacity(&b->queue);
    if (target > limit)
        target = limit;
    unsigned wait_ms = b->repeated_gap || delivered < realtime / 2 ? 8000u : 6000u;
    return b->queued >= target || elapsed >= wait_ms;
}

size_t pp_playback_take(pp_playback_buffer_t *b, void *pcm, size_t capacity,
                        bool dma_empty, uint32_t now_ms, uint32_t *epoch) {
    if (!b->active || b->writing)
        return 0;
    if (dma_empty && !b->priming && (!b->ended || b->queued)) {
        /* Forget old stalls after two seconds of uninterrupted output. */
        b->repeated_gap = b->report.underruns &&
            b->report.i2s_bytes - b->last_underrun_i2s < PP_PLAYBACK_RATE * 2u * 2u;
        b->last_underrun_i2s = b->report.i2s_bytes;
        b->report.underruns++;
        b->priming = true;
        b->prefill_first_bytes = 0;
    }
    if (b->priming && !b->ended && !prefilled(b, now_ms))
        return 0;
    size_t n = capacity < b->queued ? capacity : b->queued;
    n = pp_stream_read(&b->queue, pcm, n & ~(size_t)1, &b->writing_ram);
    if (!n)
        return 0;
    b->priming = false;
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
        b->credit += b->writing_ram;
    else
        b->active = false;
    b->writing_ram = b->writing = 0;
}

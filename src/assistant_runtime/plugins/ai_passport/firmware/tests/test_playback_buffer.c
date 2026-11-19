#include "playback_buffer.h"

#include <assert.h>
#include <string.h>

int main(void) {
    pp_playback_buffer_t buffer = {0};
    assert(pp_buffers_acquire(buffer.blocks, PP_PLAYBACK_BLOCKS, PP_PLAYBACK_BLOCK_BYTES,
                              PP_MEMORY_CRITICAL, PP_MEMORY_PLAYBACK));
    uint8_t source[PP_PLAYBACK_WINDOW + 1024], out[1024];
    for (size_t i = 0; i < sizeof(source); i++)
        source[i] = (uint8_t)(i ^ (i >> 8));

    uint32_t epoch = 0;
    pp_playback_reset(&buffer, 3, true);
    /* One short WebSocket frame per sample must fill exactly the advertised window. */
    for (size_t i = 0; i < PP_PLAYBACK_WINDOW; i += 2)
        assert(pp_playback_append(&buffer, 3, source + i, 2));
    assert(!pp_playback_append(&buffer, 3, source, 2));
    assert(pp_playback_take(&buffer, out, 1023, &epoch) == 1022);
    assert(!memcmp(out, source, 1022));
    assert(!buffer.credit && !pp_playback_take(&buffer, out, sizeof(out), &epoch));
    assert(!pp_playback_append(&buffer, 3, source, 2)); /* Still in the codec write. */
    pp_playback_written(&buffer, epoch, true);
    assert(buffer.credit == 1022);
    assert(pp_playback_append(&buffer, 3, source + PP_PLAYBACK_WINDOW, 1022));

    size_t offset = 1022;
    while (buffer.queued) {
        size_t n = pp_playback_take(&buffer, out, sizeof(out), &epoch);
        assert(n && !memcmp(out, source + offset, n));
        pp_playback_written(&buffer, epoch, true);
        offset += n;
    }
    assert(offset == PP_PLAYBACK_WINDOW + 1022 && buffer.credit == offset);

    /* A new segment can reuse the wire generation. Late I/O from the old
     * segment must neither grant credit nor fail the new one. */
    assert(pp_playback_append(&buffer, 3, source, 2));
    assert(pp_playback_take(&buffer, out, sizeof(out), &epoch) == 2);
    uint32_t old_epoch = epoch;
    pp_playback_reset(&buffer, 3, true);
    assert(pp_playback_append(&buffer, 2, source, 2) && !buffer.queued);
    assert(!pp_playback_append(&buffer, 3, source, 3));
    assert(!pp_playback_append(&buffer, 3, NULL, 2));
    assert(pp_playback_append(&buffer, 3, source, 4));
    assert(pp_playback_take(&buffer, out, sizeof(out), &epoch) == 4);
    pp_playback_written(&buffer, old_epoch, true);
    pp_playback_written(&buffer, old_epoch, false);
    assert(buffer.active && buffer.writing == 4 && !buffer.credit);
    pp_playback_written(&buffer, epoch, false);
    assert(!buffer.active && !buffer.writing && !buffer.credit);
    assert(!pp_playback_take(&buffer, out, sizeof(out), &epoch));
    assert(pp_playback_append(&buffer, 3, source, 2) && !buffer.queued);
    pp_buffers_release(buffer.blocks, PP_PLAYBACK_BLOCKS);
    assert(!pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    return 0;
}

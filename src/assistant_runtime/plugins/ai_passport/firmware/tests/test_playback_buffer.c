#include "playback_buffer.h"

#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <string.h>

static void forget_pages(void) {}

int main(void) {
    pp_playback_buffer_t buffer = {0};
    assert(pp_buffers_acquire(buffer.blocks, PP_PLAYBACK_BLOCKS, PP_PLAYBACK_BLOCK_BYTES,
                              PP_MEMORY_CRITICAL, PP_MEMORY_PLAYBACK));
    uint8_t source[PP_PLAYBACK_MAX_WINDOW + 1024], out[1024];
    for (size_t i = 0; i < sizeof(source); i++)
        source[i] = (uint8_t)(i ^ (i >> 8));

    uint32_t epoch = 0;
    pp_playback_reset(&buffer, 3, PP_PLAYBACK_WINDOW);
    /* One short WebSocket frame per sample must fill exactly the advertised window. */
    for (size_t i = 0; i < PP_PLAYBACK_WINDOW; i += 2)
        assert(pp_playback_append(&buffer, 3, source + i, 2));
    assert(!pp_playback_append(&buffer, 3, source, 2));
    assert(pp_playback_take(&buffer, out, 1023, false, 0, &epoch) == 1022);
    assert(!memcmp(out, source, 1022));
    assert(!buffer.credit && !pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch));
    assert(!pp_playback_append(&buffer, 3, source, 2)); /* Still in the codec write. */
    pp_playback_written(&buffer, epoch, out, buffer.writing, true);
    assert(buffer.credit == 1022);
    assert(pp_playback_append(&buffer, 3, source + PP_PLAYBACK_WINDOW, 1022));

    size_t offset = 1022;
    while (buffer.queued) {
        size_t n = pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch);
        assert(n && !memcmp(out, source + offset, n));
        pp_playback_written(&buffer, epoch, out, buffer.writing, true);
        offset += n;
    }
    assert(offset == PP_PLAYBACK_WINDOW + 1022 && buffer.credit == offset);

    assert(buffer.report.received_bytes == offset && buffer.report.received_crc32 == 0x7c933a02u);
    assert(buffer.report.i2s_bytes == offset && buffer.report.i2s_crc32 == buffer.report.received_crc32);

    /* A new segment can reuse the wire generation. Late I/O from the old
     * segment must neither grant credit nor fail the new one. */
    assert(pp_playback_append(&buffer, 3, source, 2));
    assert(pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch) == 2);
    uint32_t old_epoch = epoch;
    pp_playback_reset(&buffer, 3, PP_PLAYBACK_WINDOW);
    assert(pp_playback_append(&buffer, 2, source, 2) && !buffer.queued);
    assert(!pp_playback_append(&buffer, 3, source, 3));
    assert(!pp_playback_append(&buffer, 3, NULL, 2));
    assert(pp_playback_append(&buffer, 3, source, 4));
    pp_playback_finish(&buffer, 2); /* An old playback.end cannot release the prefill. */
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch));
    pp_playback_finish(&buffer, 3);
    assert(pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch) == 4);
    pp_playback_written(&buffer, old_epoch, out, 2, true);
    pp_playback_written(&buffer, old_epoch, out, 0, false);
    assert(buffer.active && buffer.writing == 4 && !buffer.credit);
    pp_playback_written(&buffer, epoch, out, 2, false);
    assert(!buffer.active && !buffer.writing && !buffer.credit);
    assert(buffer.report.received_bytes == 4 && buffer.report.i2s_bytes == 2);
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch));
    assert(pp_playback_append(&buffer, 3, source, 2) && !buffer.queued);

    /* Wait for the initial prefill and for a refill after the real DMA ring
     * has emptied. Keep a short fragment while waiting, then play it in order. */
    pp_playback_reset(&buffer, 4, PP_PLAYBACK_WINDOW);
    assert(pp_playback_append(&buffer, 4, source, PP_PLAYBACK_PREFILL_BYTES - 2));
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch));
    assert(pp_playback_append(&buffer, 4, source + PP_PLAYBACK_PREFILL_BYTES - 2, 2));
    offset = 0;
    while (buffer.queued) {
        size_t n = pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch);
        assert(n && !memcmp(out, source + offset, n));
        pp_playback_written(&buffer, epoch, out, buffer.writing, true);
        offset += n;
    }
    assert(pp_playback_append(&buffer, 4, source, 512));
    assert(!pp_playback_take(&buffer, out, sizeof(out), true, 0, &epoch));
    assert(buffer.report.underruns == 1);
    assert(!pp_playback_take(&buffer, out, sizeof(out), true, 0, &epoch) && buffer.report.underruns == 1);
    assert(buffer.queued == 512 && buffer.credit == PP_PLAYBACK_PREFILL_BYTES);
    assert(pp_playback_append(&buffer, 4, source + 512, PP_PLAYBACK_PREFILL_BYTES - 512));
    assert(pp_playback_take(&buffer, out, sizeof(out), true, 0, &epoch) == sizeof(out));
    assert(!memcmp(out, source, sizeof(out)));
    pp_playback_written(&buffer, epoch, out, buffer.writing, true);
    assert(pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch) == sizeof(out));

    /* Stop discards old audio. An ended short segment still drains in full. */
    old_epoch = epoch;
    pp_playback_reset(&buffer, 5, PP_PLAYBACK_WINDOW);
    pp_playback_written(&buffer, old_epoch, out, 2, true);
    assert(!buffer.credit);
    assert(pp_playback_append(&buffer, 4, source, 512) && !buffer.queued);
    assert(pp_playback_append(&buffer, 5, source, 512));
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch));
    pp_playback_finish(&buffer, 5);
    assert(pp_playback_take(&buffer, out, sizeof(out), true, 0, &epoch) == 512);
    assert(!memcmp(out, source, 512));
    pp_playback_written(&buffer, epoch, out, buffer.writing, true);
    assert(!pp_playback_take(&buffer, out, sizeof(out), true, 0, &epoch) && buffer.credit == 512);
    assert(buffer.report.underruns == 0);
    pp_playback_reset(&buffer, 6, PP_PLAYBACK_MAX_WINDOW);
    const size_t prefill = PP_PLAYBACK_MAX_WINDOW - PP_PLAYBACK_BLOCK_BYTES;
    assert(pp_playback_append(&buffer, 6, source, prefill - 2));
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch));
    assert(pp_playback_append(&buffer, 6, source + prefill - 2, 2));
    assert(pp_playback_take(&buffer, out, sizeof(out), false, 0, &epoch) == sizeof(out));
    assert(!memcmp(out, source, sizeof(out)));

    /* Real-time arrival timing must survive the 32-bit millisecond wrap. A
     * RAM-only link can always start once its reachable window is full. */
    pp_playback_reset(&buffer, 7, PP_PLAYBACK_WINDOW);
    assert(pp_playback_append(&buffer, 7, source, 2048));
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, UINT32_MAX-99, &epoch));
    assert(pp_playback_append(&buffer, 7, source+2048, 4096));
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, 200, &epoch));
    assert(pp_playback_append(&buffer, 7, source+6144, PP_PLAYBACK_WINDOW-6144));
    assert(pp_playback_take(&buffer, out, sizeof(out), false, 400, &epoch) == sizeof(out));
    pp_playback_written(&buffer, epoch, out, sizeof(out), true);
    pp_playback_finish(&buffer, 7);
    assert(pp_playback_take(&buffer, out, sizeof(out), true, 800, &epoch) == sizeof(out));
    assert(buffer.report.underruns == 1); /* A real gap while draining the tail. */

    /* Use the real lease controller. No disk I/O is needed to test a pause
     * after enough bytes for normal playback but too few for slow prefill. */
    pp_stream_attach((pp_stream_disk_t){.pages=64, .forget=forget_pages, .single_pass=true});
    assert(pp_stream_prepare(&buffer.queue, 0));
    pp_playback_reset(&buffer, 8, PP_PLAYBACK_WINDOW);
    assert(pp_playback_append(&buffer, 8, source, 2048));
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, 100, &epoch));
    assert(pp_playback_append(&buffer, 8, source+2048, 4096));
    assert(!pp_playback_take(&buffer, out, sizeof(out), false, 8099, &epoch));
    assert(pp_playback_take(&buffer, out, sizeof(out), false, 8100, &epoch) == sizeof(out));
    pp_playback_written(&buffer, epoch, out, sizeof(out), true);
    assert(pp_playback_take(&buffer, out, sizeof(out), true, 8200, &epoch) == sizeof(out));
    pp_playback_written(&buffer, epoch, out, sizeof(out), true);
    assert(pp_playback_append(&buffer, 8, source, 2048));
    assert(!pp_playback_take(&buffer, out, sizeof(out), true, 8300, &epoch)); /* Two close gaps. */
    assert(buffer.report.underruns == 2);
    pp_playback_finish(&buffer, 8);
    assert(pp_playback_take(&buffer, out, sizeof(out), true, 8301, &epoch) == sizeof(out));
    pp_playback_reset(&buffer, 9, PP_PLAYBACK_WINDOW);
    pp_playback_finish(&buffer, 8);
    assert(!buffer.ended && !buffer.queued && !buffer.repeated_gap);
    assert(pp_playback_append(&buffer, 9, source, PP_PLAYBACK_PREFILL_BYTES));
    assert(pp_playback_take(&buffer, out, sizeof(out), false, 8400, &epoch) == sizeof(out));
    pp_playback_reset(&buffer, 0, 0);
    while (pp_stream_service()) {}
    assert(!pp_stream_stats().workspace_bytes);
    pp_buffers_release(buffer.blocks, PP_PLAYBACK_BLOCKS);
    assert(!pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    return 0;
}

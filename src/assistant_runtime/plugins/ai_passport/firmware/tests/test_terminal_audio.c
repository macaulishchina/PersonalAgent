#include "terminal_audio.h"
#include "terminal_audio_fixture.h"
#include "byte_buffer.h"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <setjmp.h>
#undef realloc
#include <stdlib.h>
#include <string.h>

static void (*worker_entry)(void *);
static jmp_buf finished;
static bool test_mute, test_drain, small_heap, overflowing = true;
static bool test_cues, early_voice;
static uint32_t tx_position;
static unsigned reads, writes, failed_mutes, polls, alloc_limit, capture_frames;
static unsigned cue_reads;
static int16_t first_cue[512];
static char task;

void *pp_test_realloc(void *pointer, size_t size) {
    if (!alloc_limit)
        return NULL;
    if (alloc_limit != UINT32_MAX)
        alloc_limit--;
    assert(size <= 2560); /* Model a fragmented heap with no large block. */
    return realloc(pointer, size);
}
int xTaskCreate(void (*entry)(void *), const char *name, unsigned stack, void *arg,
                unsigned priority, TaskHandle_t *worker) {
    assert(!strcmp(name, "pp_audio") && stack && !arg && priority);
    *worker = &task;
    worker_entry = entry;
    return pdPASS;
}
void xTaskNotifyGive(TaskHandle_t worker) { assert(worker == &task); }
void vTaskDelay(unsigned wait) {
    (void)wait;
    abort(); /* An already parked codec must acknowledge quiesce immediately. */
}

int bsp_audio_init(void) { return ESP_OK; }
int bsp_audio_set_volume(unsigned volume) {
    if (test_mute && volume == 0) {
        failed_mutes++;
        return -1;
    }
    return ESP_OK;
}
int bsp_audio_set_format(unsigned rate, unsigned bits, unsigned channels) {
    assert(rate == (test_mute || test_drain ? PP_PLAYBACK_RATE : PP_CAPTURE_RATE));
    assert(bits == 16 && channels == 1);
    return ESP_OK;
}
int bsp_audio_read(void *pcm, size_t size) {
    reads++;
    if (test_cues) {
        if (reads == 1) {
            memset(pcm, early_voice ? 0x10 : 0, size);
            pp_audio_capture_confirm();
            assert(!pp_audio_capture_ready());
        } else if (cue_reads) {
            cue_reads--;
            tx_position += 7;
            memset(pcm, 0x7f, size); /* Simulate the speaker entering the mic. */
        } else {
            assert(pp_audio_capture_ready());
            memset(pcm, 2, size);
            pp_audio_capture_release();
            pp_audio_recording_released();
        }
        return ESP_OK;
    }
    memset(pcm, reads, size);
    if (!overflowing)
        pp_audio_capture_release(); /* Key released during the hardware read. */
    return ESP_OK;
}
int bsp_audio_write(const void *pcm, size_t size, size_t *written) {
    if (test_cues) {
        assert(pcm && size == sizeof(first_cue) && !cue_reads);
        const int16_t *samples = pcm;
        assert(samples[0] == 0 && samples[511] == 0);
        assert(memcmp(samples, (const int16_t[512]){0}, size));
        const bool opening = !early_voice && !writes;
        if (opening) {
            assert(pp_audio_capturing() && !pp_audio_capture_ready());
            memcpy(first_cue, pcm, size);
        } else {
            pp_recording_t frame;
            assert(!pp_audio_capturing());
            assert(pp_audio_recorded(&frame) && frame.pcm[0] == (early_voice ? 0x10 : 0));
            assert(pp_audio_recorded(&frame) && frame.pcm[0] == 2);
            assert(pp_audio_capture_drained());
            if (!early_voice)
                assert(memcmp(first_cue, pcm, size));
        }
        cue_reads = 3;
        *written = size;
        writes++;
        return ESP_OK;
    }
    assert((test_mute || test_drain) && pcm && size == 2);
    *written = size;
    writes++;
    if (test_mute) {
        pp_audio_play_stop(); /* UI stops while this I2S write is outstanding. */
        assert(!pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    }
    return ESP_OK;
}
uint32_t bsp_audio_tx_position(void) { return tx_position; }
bool bsp_audio_tx_drained(uint32_t position) { return tx_position - position >= 7u; }

void ulTaskNotifyTake(int clear, unsigned wait) {
    assert(clear && wait == portMAX_DELAY && ++polls <= 5);
    if (test_cues) {
        assert(writes == (early_voice ? 1u : 2u) && !cue_reads);
        assert(pp_audio_capture_drained() && !pp_audio_playback_report().i2s_bytes);
    } else if (test_drain) {
        assert(writes == 1 && pp_audio_take_credit() == 2);
        assert(!pp_audio_play_drained());
        tx_position = 6;
        assert(!pp_audio_play_drained());
        tx_position = 7;
        assert(pp_audio_play_drained());
        pp_playback_report_t report = pp_audio_playback_report();
        assert(report.generation == 1 && report.received_bytes == 2 && report.i2s_bytes == 2);
        assert(report.received_crc32 == report.i2s_crc32 && !report.underruns);
        pp_audio_play_stop();
        assert(pp_audio_playback_report().i2s_bytes == 2);
        assert(!pp_audio_play_drained() && !pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    } else if (test_mute) {
        /* The failed device stays parked when woken, sends one status and
         * continues to acknowledge a later sleep request. */
        if (polls == 1) {
            assert(pp_audio_take_error() == PP_AUDIO_CODEC_FAILURE);
            assert(!pp_audio_capture_begin());
            assert(!pp_audio_capturing());
            return;
        }
        assert(pp_audio_take_error() == PP_AUDIO_OK);
        assert(pp_audio_quiesce());
        assert(writes == 1 && failed_mutes == 1 && reads == 0);
    } else {
        pp_recording_t frame;
        if (overflowing) {
            assert(pp_audio_take_error() == PP_AUDIO_CAPTURE_OVERFLOW);
            assert(!pp_audio_capturing());
            assert(reads == capture_frames + 1 && pp_audio_recorded_count() == capture_frames);
            for (unsigned i = 1; i <= capture_frames; i++) {
                assert(pp_audio_recorded(&frame));
                assert(frame.pcm[0] == i && frame.pcm[sizeof(frame.pcm) - 1] == i);
            }
            pp_audio_capture_discard();
            assert(!pp_memory_stats().live[PP_MEMORY_CAPTURE]);
            assert(!pp_audio_recording_capacity());
            overflowing = false;
            alloc_limit = UINT32_MAX;
            assert(pp_audio_capture_begin());
            return;
        }
        assert(pp_audio_take_error() == PP_AUDIO_OK);
        assert(reads == capture_frames + 2 && pp_audio_recorded(&frame) && pp_audio_capture_drained());
        assert(frame.pcm[0] == reads);
        pp_audio_capture_discard();
        assert(!pp_memory_stats().live[PP_MEMORY_CAPTURE]);
    }
    longjmp(finished, 1);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    test_mute = !strcmp(argv[1], "mute");
    test_drain = !strcmp(argv[1], "drain");
    small_heap = !strcmp(argv[1], "small-heap");
    early_voice = !strcmp(argv[1], "early-voice");
    test_cues = early_voice || !strcmp(argv[1], "cues");
    assert(test_mute || test_drain || test_cues || small_heap || !strcmp(argv[1], "overflow"));
    assert(pp_audio_start(45));
    assert(!pp_memory_stats().live[PP_MEMORY_CAPTURE] && !pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    alloc_limit = 2;
    assert(!pp_audio_capture_begin() && !pp_memory_stats().live[PP_MEMORY_CAPTURE]);
    alloc_limit = 2;
    assert(!pp_audio_play_begin(1, PP_PLAYBACK_MAX_WINDOW) && !pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    alloc_limit = PP_PLAYBACK_WINDOW / PP_PLAYBACK_BLOCK_BYTES;
    assert(pp_audio_play_begin(1, PP_PLAYBACK_MAX_WINDOW) == PP_PLAYBACK_WINDOW);
    pp_audio_play_stop();
    assert(!pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    alloc_limit = UINT32_MAX;
    if (test_mute || test_drain) {
        const int16_t sample = 0;
        assert(pp_audio_play_begin(1, PP_PLAYBACK_WINDOW) == PP_PLAYBACK_WINDOW);
        assert(pp_memory_stats().live[PP_MEMORY_PLAYBACK] == PP_PLAYBACK_WINDOW);
        assert(pp_play_pcm(1, &sample, sizeof(sample)));
        assert(!pp_audio_play_drained());
        pp_audio_play_end(1);
    } else {
        capture_frames = small_heap ? PP_CAPTURE_FRAMES : PP_CAPTURE_MAX_FRAMES;
        if (small_heap)
            alloc_limit = PP_CAPTURE_FRAMES / 4; /* Only the base capture blocks fit. */
        assert(pp_audio_capture_begin());
        assert(pp_audio_recording_capacity() == capture_frames);
        assert(pp_memory_stats().live[PP_MEMORY_CAPTURE] == capture_frames * PP_CAPTURE_BYTES);
    }
    if (!setjmp(finished))
        worker_entry(NULL);
    return 0;
}

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
static bool test_mute, overflowing = true;
static unsigned reads, writes, failed_mutes, polls, alloc_limit;
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
    assert(rate == (test_mute ? PP_PLAYBACK_RATE : PP_CAPTURE_RATE));
    assert(bits == 16 && channels == 1);
    return ESP_OK;
}
int bsp_audio_read(void *pcm, size_t size) {
    reads++;
    memset(pcm, reads, size);
    if (!overflowing)
        pp_audio_capture_release(); /* Key released during the hardware read. */
    return ESP_OK;
}
int bsp_audio_write(const void *pcm, size_t size) {
    assert(test_mute && pcm && size == 2);
    writes++;
    pp_audio_play_stop(); /* UI stops while this I2S write is outstanding. */
    assert(!pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    return ESP_OK;
}

void ulTaskNotifyTake(int clear, unsigned wait) {
    assert(clear && wait == portMAX_DELAY && ++polls <= 5);
    if (test_mute) {
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
            assert(reads == PP_CAPTURE_FRAMES + 1 && pp_audio_recorded_count() == PP_CAPTURE_FRAMES);
            for (unsigned i = 1; i <= PP_CAPTURE_FRAMES; i++) {
                assert(pp_audio_recorded(&frame));
                assert(frame.pcm[0] == i && frame.pcm[sizeof(frame.pcm) - 1] == i);
            }
            pp_audio_capture_discard();
            assert(!pp_memory_stats().live[PP_MEMORY_CAPTURE]);
            overflowing = false;
            assert(pp_audio_capture_begin());
            return;
        }
        assert(pp_audio_take_error() == PP_AUDIO_OK);
        assert(reads == PP_CAPTURE_FRAMES + 2 && pp_audio_recorded(&frame) && pp_audio_capture_drained());
        assert(frame.pcm[0] == reads);
        pp_audio_capture_discard();
        assert(!pp_memory_stats().live[PP_MEMORY_CAPTURE]);
    }
    longjmp(finished, 1);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    test_mute = !strcmp(argv[1], "mute");
    assert(test_mute || !strcmp(argv[1], "overflow"));
    assert(pp_audio_start(45));
    assert(!pp_memory_stats().live[PP_MEMORY_CAPTURE] && !pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    alloc_limit = 2;
    assert(!pp_audio_capture_begin() && !pp_memory_stats().live[PP_MEMORY_CAPTURE]);
    alloc_limit = 2;
    assert(!pp_audio_play_begin(1) && !pp_memory_stats().live[PP_MEMORY_PLAYBACK]);
    alloc_limit = UINT32_MAX;
    if (test_mute) {
        const int16_t sample = 0;
        assert(pp_audio_play_begin(1));
        assert(pp_memory_stats().live[PP_MEMORY_PLAYBACK] == PP_PLAYBACK_WINDOW);
        assert(pp_play_pcm(1, &sample, sizeof(sample)));
    } else {
        assert(pp_audio_capture_begin());
        assert(pp_memory_stats().live[PP_MEMORY_CAPTURE] == PP_CAPTURE_FRAMES * PP_CAPTURE_BYTES);
    }
    if (!setjmp(finished))
        worker_entry(NULL);
    return 0;
}

#include "terminal_audio.h"
#include "terminal_audio_fixture.h"
#include "network.h"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <setjmp.h>
#include <stdlib.h>
#include <string.h>

static void (*worker_entry)(void *);
static jmp_buf finished;
static bool test_mute, queue_full = true;
static unsigned reads, writes, failed_mutes, polls, queued;
static pp_recording_t queued_block;
static char task, queue;

QueueHandle_t xQueueCreate(unsigned length, size_t size) {
    assert(length == 32 && size == sizeof(pp_recording_t));
    return &queue;
}
int xQueueSend(QueueHandle_t handle, const void *data, unsigned wait) {
    assert(handle == &queue && !wait);
    if (queue_full)
        return 0;
    memcpy(&queued_block, data, sizeof(queued_block));
    queued++;
    pp_audio_capture_release(); /* Key released during this 20 ms hardware read. */
    return pdTRUE;
}
int xQueueReceive(QueueHandle_t handle, void *data, unsigned wait) {
    assert(handle == &queue && !wait);
    if (!queued)
        return 0;
    queued--;
    memcpy(data, &queued_block, sizeof(queued_block));
    return pdTRUE;
}
unsigned uxQueueMessagesWaiting(QueueHandle_t handle) { assert(handle == &queue); return queued; }
void xQueueReset(QueueHandle_t handle) { assert(handle == &queue); queued = 0; }
void vQueueDelete(QueueHandle_t handle) { assert(handle == &queue); }
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
    memset(pcm, 0, size);
    return ESP_OK;
}
int bsp_audio_write(const void *pcm, size_t size) {
    assert(test_mute && pcm && size == 2);
    writes++;
    pp_audio_play_stop(); /* UI stops while this I2S write is outstanding. */
    return ESP_OK;
}

void ulTaskNotifyTake(int clear, unsigned wait) {
    assert(clear && wait == portMAX_DELAY && ++polls <= 5);
    if (test_mute) {
        /* The failed device stays parked when woken, sends one status and
         * continues to acknowledge a later sleep request. */
        if (polls == 1) {
            assert(pp_audio_take_error() == PP_AUDIO_CODEC_FAILURE);
            pp_audio_capture_begin();
            assert(!pp_audio_capturing());
            return;
        }
        assert(pp_audio_take_error() == PP_AUDIO_OK);
        assert(pp_audio_quiesce());
        assert(writes == 1 && failed_mutes == 1 && reads == 0);
    } else {
        if (queue_full) {
            assert(pp_audio_take_error() == PP_AUDIO_CAPTURE_OVERFLOW);
            assert(!pp_audio_capturing());
            queue_full = false;
            pp_audio_capture_begin();
            return;
        }
        pp_recording_t frame;
        assert(pp_audio_take_error() == PP_AUDIO_OK);
        assert(reads == 2 && pp_audio_recorded(&frame) && pp_audio_capture_drained());
    }
    longjmp(finished, 1);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    test_mute = !strcmp(argv[1], "mute");
    assert(test_mute || !strcmp(argv[1], "overflow"));
    assert(pp_audio_start(45));
    if (test_mute) {
        const int16_t sample = 0;
        pp_audio_play_begin(1);
        assert(pp_play_pcm(1, &sample, sizeof(sample)));
    } else {
        pp_audio_capture_begin();
    }
    if (!setjmp(finished))
        worker_entry(NULL);
    return 0;
}

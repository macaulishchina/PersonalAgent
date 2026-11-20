#include "terminal_audio.h"
#include "playback_buffer.h"
#include "bsp_audio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#ifdef ESP_PLATFORM
#include "esp_heap_caps.h"
#endif
#include <stdatomic.h>
#include <string.h>

#define CAPTURE_FRAMES_PER_BLOCK 4u
#define CAPTURE_BLOCKS (PP_CAPTURE_FRAMES / CAPTURE_FRAMES_PER_BLOCK)
#define CAPTURE_MAX_BLOCKS (PP_CAPTURE_MAX_FRAMES / CAPTURE_FRAMES_PER_BLOCK)
#define CAPTURE_BLOCK_BYTES (CAPTURE_FRAMES_PER_BLOCK * PP_CAPTURE_BYTES)
/* Optional capture capacity must leave room for TLS and control messages. */
#define CAPTURE_HEAP_RESERVE (32u * 1024u)
_Static_assert(PP_CAPTURE_FRAMES % CAPTURE_FRAMES_PER_BLOCK == 0, "Whole capture blocks");
_Static_assert(PP_CAPTURE_MAX_FRAMES % CAPTURE_FRAMES_PER_BLOCK == 0, "Whole capture blocks");

static pp_buffer_t recorded[CAPTURE_MAX_BLOCKS];
static unsigned record_head, record_count, record_capacity;
static portMUX_TYPE record_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t worker;
static pp_playback_buffer_t output;
static uint32_t output_tx_position;
static bool output_has_written;
static portMUX_TYPE output_lock = portMUX_INITIALIZER_UNLOCKED;
static atomic_bool capturing, busy, codec_failed, pause_requested, parked;
static atomic_int fault;
static atomic_uint capture_generation, target_volume, input_level;
static atomic_uint capture_releases;

bool pp_audio_capturing(void) { return capturing; }
bool pp_audio_busy(void) { return busy; }
unsigned pp_audio_level(void) { return input_level; }
unsigned pp_audio_volume(void) { return target_volume; }
void pp_audio_set_volume(unsigned volume) { target_volume = volume; }
pp_audio_error_t pp_audio_take_error(void) { return atomic_exchange(&fault, PP_AUDIO_OK); }

void pp_audio_capture_release(void) {
    capture_releases++;
    capturing = false;
}
void pp_audio_capture_discard(void) {
    pp_buffer_t returned[CAPTURE_MAX_BLOCKS];
    portENTER_CRITICAL(&record_lock);
    capturing = false;
    capture_generation++;
    int overflow = PP_AUDIO_CAPTURE_OVERFLOW;
    atomic_compare_exchange_strong(&fault, &overflow, PP_AUDIO_OK);
    unsigned blocks = record_capacity / CAPTURE_FRAMES_PER_BLOCK;
    record_head = record_count = record_capacity = 0;
    for (unsigned i = 0; i < blocks; i++)
        returned[i] = pp_buffer_take(&recorded[i]);
    portEXIT_CRITICAL(&record_lock);
    pp_buffers_release(returned, blocks);
}
bool pp_audio_capture_begin(void) {
    pp_audio_capture_discard();
    if (!worker || codec_failed)
        return false;
    unsigned releases = capture_releases;
    pp_buffer_t buffers[CAPTURE_MAX_BLOCKS] = {0};
    if (!pp_buffers_acquire(buffers, CAPTURE_BLOCKS, CAPTURE_BLOCK_BYTES,
                            PP_MEMORY_CRITICAL, PP_MEMORY_CAPTURE))
        return false;
    unsigned blocks = CAPTURE_BLOCKS;
    while (blocks < CAPTURE_MAX_BLOCKS) {
#ifdef ESP_PLATFORM
        if (heap_caps_get_free_size(MALLOC_CAP_8BIT) < CAPTURE_HEAP_RESERVE + CAPTURE_BLOCK_BYTES)
            break;
#endif
        if (!pp_buffer_reserve(&buffers[blocks], CAPTURE_BLOCK_BYTES, PP_MEMORY_BACKGROUND,
                               PP_MEMORY_CAPTURE))
            break;
        blocks++;
    }
    portENTER_CRITICAL(&record_lock);
    memcpy(recorded, buffers, blocks * sizeof(*buffers));
    record_capacity = blocks * CAPTURE_FRAMES_PER_BLOCK;
    capturing = capture_releases == releases && !codec_failed;
    portEXIT_CRITICAL(&record_lock);
    xTaskNotifyGive(worker);
    return true;
}
/* Called only with the capture lock and while the current session owns its blocks. */
static char *capture_slot(unsigned frame) {
    return recorded[frame / CAPTURE_FRAMES_PER_BLOCK].data +
           (frame % CAPTURE_FRAMES_PER_BLOCK) * PP_CAPTURE_BYTES;
}
bool pp_audio_recorded(pp_recording_t *block) {
    portENTER_CRITICAL(&record_lock);
    bool available = record_count > 0;
    if (available) {
        block->generation = capture_generation;
        memcpy(block->pcm, capture_slot(record_head), sizeof(block->pcm));
        record_head = (record_head + 1) % record_capacity;
        record_count--;
    }
    portEXIT_CRITICAL(&record_lock);
    return available;
}
unsigned pp_audio_recorded_count(void) {
    portENTER_CRITICAL(&record_lock);
    unsigned count = record_count;
    portEXIT_CRITICAL(&record_lock);
    return count;
}
unsigned pp_audio_recording_capacity(void) {
    portENTER_CRITICAL(&record_lock);
    unsigned capacity = record_capacity;
    portEXIT_CRITICAL(&record_lock);
    return capacity;
}
bool pp_audio_capture_drained(void) {
    return !capturing && !busy && !codec_failed && fault == PP_AUDIO_OK && !pp_audio_recorded_count();
}

bool pp_audio_play_begin(uint32_t generation) {
    if (!worker || codec_failed)
        return false;
    /* Only the controller changes backing storage. The codec worker copies
     * each write into its own scratch block under the lock. */
    if (output.blocks[0].data) {
        portENTER_CRITICAL(&output_lock);
        pp_playback_reset(&output, generation, true);
        output_has_written = false;
        portEXIT_CRITICAL(&output_lock);
    } else {
        pp_buffer_t buffers[PP_PLAYBACK_BLOCKS] = {0};
        if (!pp_buffers_acquire(buffers, PP_PLAYBACK_BLOCKS, PP_PLAYBACK_BLOCK_BYTES,
                                PP_MEMORY_CRITICAL, PP_MEMORY_PLAYBACK))
            return false;
        portENTER_CRITICAL(&output_lock);
        memcpy(output.blocks, buffers, sizeof(output.blocks));
        pp_playback_reset(&output, generation, true);
        output_has_written = false;
        portEXIT_CRITICAL(&output_lock);
    }
    xTaskNotifyGive(worker);
    return true;
}
void pp_audio_play_stop(void) {
    pp_buffer_t returned[PP_PLAYBACK_BLOCKS];
    portENTER_CRITICAL(&output_lock);
    pp_playback_reset(&output, 0, false);
    output_has_written = false;
    for (unsigned i = 0; i < PP_PLAYBACK_BLOCKS; i++)
        returned[i] = pp_buffer_take(&output.blocks[i]);
    portEXIT_CRITICAL(&output_lock);
    pp_buffers_release(returned, PP_PLAYBACK_BLOCKS);
    if (worker)
        xTaskNotifyGive(worker); /* Apply stop/mute even when no more frames arrive. */
}
bool pp_play_pcm(uint32_t generation, const void *pcm, size_t bytes) {
    if (!worker || codec_failed)
        return true; /* Controller declines playback on a terminal without audio. */
    portENTER_CRITICAL(&output_lock);
    bool accepted = pp_playback_append(&output, generation, pcm, bytes);
    portEXIT_CRITICAL(&output_lock);
    if (accepted)
        xTaskNotifyGive(worker);
    return accepted;
}
void pp_audio_play_end(uint32_t generation) {
    portENTER_CRITICAL(&output_lock);
    pp_playback_finish(&output, generation);
    portEXIT_CRITICAL(&output_lock);
    if (worker)
        xTaskNotifyGive(worker);
}
bool pp_audio_play_drained(void) {
    portENTER_CRITICAL(&output_lock);
    bool drained = output.active && output.ended && !output.writing && !output.queued &&
                   (!output_has_written || bsp_audio_tx_drained(output_tx_position));
    portEXIT_CRITICAL(&output_lock);
    return drained;
}
unsigned pp_audio_take_credit(void) {
    portENTER_CRITICAL(&output_lock);
    unsigned bytes = output.credit;
    output.credit = 0;
    portEXIT_CRITICAL(&output_lock);
    return bytes;
}

typedef struct {
    unsigned rate, volume;
} codec_state_t;
static bool set_volume(codec_state_t *codec, unsigned volume) {
    if (volume != codec->volume && bsp_audio_set_volume(volume) != ESP_OK)
        return false;
    codec->volume = volume;
    return true;
}
static bool set_format(codec_state_t *codec, unsigned rate, unsigned volume) {
    if (codec->rate != rate) {
        if (bsp_audio_set_format(rate, 16, 1) != ESP_OK)
            return false;
        codec->rate = rate;
        codec->volume = UINT32_MAX;
    }
    return set_volume(codec, volume);
}
static pp_audio_error_t read_block(codec_state_t *codec, unsigned generation) {
    pp_recording_t block = {.generation = generation};
    if (!set_format(codec, PP_CAPTURE_RATE, 0) ||
        bsp_audio_read(block.pcm, sizeof(block.pcm)) != ESP_OK)
        return PP_AUDIO_CODEC_FAILURE;
    portENTER_CRITICAL(&record_lock);
    if (capture_generation != generation) {
        portEXIT_CRITICAL(&record_lock);
        return PP_AUDIO_OK;
    }
    if (record_count == record_capacity) {
        /* Publish the fault with the stopped capture, before the controller
         * can drain it or start a different generation. */
        fault = PP_AUDIO_CAPTURE_OVERFLOW;
        capturing = false;
        portEXIT_CRITICAL(&record_lock);
        return PP_AUDIO_CAPTURE_OVERFLOW;
    }
    unsigned tail = (record_head + record_count) % record_capacity;
    memcpy(capture_slot(tail), block.pcm, sizeof(block.pcm));
    record_count++;
    portEXIT_CRITICAL(&record_lock);
    unsigned amplitude = 0;
    for (unsigned i = 0; i < sizeof(block.pcm); i += 16) {
        int sample = (int16_t)(block.pcm[i] | ((unsigned)block.pcm[i + 1] << 8));
        amplitude += sample < 0 ? -sample : sample;
    }
    input_level = amplitude / (sizeof(block.pcm) / 16) / 12;
    return PP_AUDIO_OK;
}

static void disable_codec(void) {
    /* Only the I2S worker transitions the codec to this state. Keep the
     * UI, transport and deep-sleep handshake available until reboot. */
    codec_failed = true;
    capturing = false;
    fault = PP_AUDIO_CODEC_FAILURE;
}

static void audio_worker(void *arg) {
    (void)arg;
    codec_state_t codec = {.volume = UINT32_MAX};
    for (;;) {
        if (pause_requested || codec_failed) {
            parked = true;
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }
        parked = false;
        busy = true;
        unsigned generation = capture_generation;
        if (capturing) {
            pp_audio_error_t error = read_block(&codec, generation);
            if (error == PP_AUDIO_CODEC_FAILURE)
                disable_codec();
            busy = false;
            continue;
        }
        uint8_t pcm[1024];
        uint32_t epoch = 0;
        portENTER_CRITICAL(&output_lock);
        bool dma_empty = output_has_written && bsp_audio_tx_drained(output_tx_position);
        size_t n = pp_playback_take(&output, pcm, sizeof(pcm), dma_empty, &epoch);
        bool active = output.active;
        portEXIT_CRITICAL(&output_lock);
        if (n) {
            bool ok = set_format(&codec, PP_PLAYBACK_RATE, target_volume) &&
                      bsp_audio_write(pcm, n) == ESP_OK;
            uint32_t position = bsp_audio_tx_position();
            portENTER_CRITICAL(&output_lock);
            if (ok && epoch == output.epoch) {
                output_tx_position = position;
                output_has_written = true;
            }
            pp_playback_written(&output, epoch, ok);
            portEXIT_CRITICAL(&output_lock);
            if (!ok)
                disable_codec();
        } else if (!active && codec.rate && !set_volume(&codec, 0)) {
            disable_codec();
        }
        busy = false;
        if (!n && !codec_failed)
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    }
}
bool pp_audio_start(unsigned volume) {
    target_volume = volume;
    return bsp_audio_init() == ESP_OK &&
           xTaskCreate(audio_worker, "pp_audio", 4096, NULL, 4, &worker) == pdPASS;
}
bool pp_audio_quiesce(void) {
    if (!worker)
        return true;
    pause_requested = true;
    xTaskNotifyGive(worker);
    for (unsigned i = 0; i < 100; i++) {
        if (parked)
            return true;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    pause_requested = false;
    xTaskNotifyGive(worker);
    return false;
}
void pp_audio_resume(void) {
    pause_requested = false;
    if (worker)
        xTaskNotifyGive(worker);
}

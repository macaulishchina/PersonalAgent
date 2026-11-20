#include "terminal_audio.h"
#include "playback_buffer.h"
#include "bsp_audio.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdatomic.h>
#include <string.h>

#define CAPTURE_FRAMES_PER_BLOCK 4u
#define CAPTURE_BLOCKS (PP_CAPTURE_FRAMES / CAPTURE_FRAMES_PER_BLOCK)
#define CAPTURE_MAX_BLOCKS (PP_CAPTURE_MAX_FRAMES / CAPTURE_FRAMES_PER_BLOCK)
#define CAPTURE_BLOCK_BYTES (CAPTURE_FRAMES_PER_BLOCK * PP_CAPTURE_BYTES)
/* Optional audio capacity must leave room for TLS and control messages. */
#define AUDIO_HEAP_RESERVE (32u * 1024u)
#define EARLY_VOICE_LEVEL 32u
#define EARLY_VOICE_STRONG_LEVEL (3u * EARLY_VOICE_LEVEL)
#define AUDIO_WORK_SAMPLES 512u
_Static_assert(PP_CAPTURE_FRAMES % CAPTURE_FRAMES_PER_BLOCK == 0, "Whole capture blocks");
_Static_assert(PP_CAPTURE_MAX_FRAMES % CAPTURE_FRAMES_PER_BLOCK == 0, "Whole capture blocks");

static pp_buffer_t recorded[CAPTURE_MAX_BLOCKS];
static pp_stream_t capture;
static unsigned record_capacity;
static portMUX_TYPE record_lock = portMUX_INITIALIZER_UNLOCKED;
static TaskHandle_t worker;
static pp_playback_buffer_t output;
static pp_playback_report_t last_output;
static uint32_t output_tx_position;
static bool output_has_written;
static portMUX_TYPE output_lock = portMUX_INITIALIZER_UNLOCKED;
static atomic_bool capturing, busy, codec_failed, pause_requested, parked;
static atomic_bool capture_reading, end_cue_pending;
static atomic_int fault;
static atomic_uint capture_generation, target_volume, input_level;
static atomic_uint capture_releases;
static atomic_uint start_cue_generation;

bool pp_audio_capturing(void) { return capturing; }
bool pp_audio_capture_ready(void) { return capturing && !start_cue_generation; }
bool pp_audio_busy(void) { return busy; }
unsigned pp_audio_level(void) { return input_level; }
unsigned pp_audio_volume(void) { return target_volume; }
void pp_audio_set_volume(unsigned volume) { target_volume = volume; }
pp_audio_error_t pp_audio_take_error(void) {
    pp_audio_error_t error = atomic_exchange(&fault, PP_AUDIO_OK);
    if (error != PP_AUDIO_CODEC_FAILURE && (pp_stream_failed(&capture) || pp_stream_failed(&output.queue)))
        return PP_AUDIO_STORAGE_FAILURE;
    return error;
}

void pp_audio_capture_release(void) {
    capture_releases++;
    capturing = false;
}
void pp_audio_capture_confirm(void) {
    if (!worker || !capturing || !target_volume)
        return;
    start_cue_generation = capture_generation;
    xTaskNotifyGive(worker);
}
void pp_audio_recording_released(void) {
    if (!worker || codec_failed || !target_volume)
        return;
    end_cue_pending = true;
    xTaskNotifyGive(worker);
}
void pp_audio_capture_discard(void) {
    pp_buffer_t returned[CAPTURE_MAX_BLOCKS];
    portENTER_CRITICAL(&record_lock);
    capturing = false;
    capture_generation++;
    start_cue_generation = 0;
    int overflow = PP_AUDIO_CAPTURE_OVERFLOW;
    atomic_compare_exchange_strong(&fault, &overflow, PP_AUDIO_OK);
    unsigned blocks = record_capacity / CAPTURE_FRAMES_PER_BLOCK;
    pp_stream_reset(&capture, NULL, 0, 0);
    record_capacity = 0;
    for (unsigned i = 0; i < blocks; i++)
        returned[i] = pp_buffer_take(&recorded[i]);
    portEXIT_CRITICAL(&record_lock);
    pp_buffers_release(returned, blocks);
}
bool pp_audio_capture_begin(void) {
    pp_audio_capture_discard();
    end_cue_pending = false;
    if (!worker || codec_failed)
        return false;
    unsigned releases = capture_releases;
    pp_buffer_t buffers[CAPTURE_MAX_BLOCKS] = {0};
    if (!pp_buffers_acquire(buffers, CAPTURE_BLOCKS, CAPTURE_BLOCK_BYTES,
                            PP_MEMORY_CRITICAL, PP_MEMORY_CAPTURE))
        return false;
    pp_stream_prepare(&capture, AUDIO_HEAP_RESERVE);
    unsigned blocks = CAPTURE_BLOCKS;
    while (blocks < CAPTURE_MAX_BLOCKS &&
           pp_buffers_try_acquire(&buffers[blocks], 1, CAPTURE_BLOCK_BYTES,
                                  AUDIO_HEAP_RESERVE, PP_MEMORY_CAPTURE))
        blocks++;
    portENTER_CRITICAL(&record_lock);
    memcpy(recorded, buffers, blocks * sizeof(*buffers));
    record_capacity = blocks * CAPTURE_FRAMES_PER_BLOCK;
    pp_stream_reset(&capture, recorded, CAPTURE_BLOCK_BYTES, blocks * CAPTURE_BLOCK_BYTES);
    capturing = capture_releases == releases && !codec_failed;
    portEXIT_CRITICAL(&record_lock);
    xTaskNotifyGive(worker);
    return true;
}
bool pp_audio_recorded(pp_recording_t *block) {
    portENTER_CRITICAL(&record_lock);
    block->generation = capture_generation;
    bool available = pp_stream_read(&capture, block->pcm, sizeof(block->pcm), NULL) == sizeof(block->pcm);
    portEXIT_CRITICAL(&record_lock);
    return available;
}
unsigned pp_audio_recorded_count(void) {
    return pp_stream_size(&capture) / PP_CAPTURE_BYTES;
}
unsigned pp_audio_recording_capacity(void) {
    portENTER_CRITICAL(&record_lock);
    unsigned capacity = record_capacity;
    portEXIT_CRITICAL(&record_lock);
    return capacity;
}
bool pp_audio_capture_drained(void) {
    return !capturing && !capture_reading && !codec_failed && fault == PP_AUDIO_OK &&
           !pp_audio_recorded_count();
}

unsigned pp_audio_play_begin(uint32_t generation, unsigned max_window, bool flash_allowed) {
    if (!worker || codec_failed)
        return 0;
    unsigned limit = max_window / PP_PLAYBACK_BLOCK_BYTES;
    if (limit > PP_PLAYBACK_BLOCKS)
        limit = PP_PLAYBACK_BLOCKS;
    unsigned blocks = PP_PLAYBACK_WINDOW / PP_PLAYBACK_BLOCK_BYTES;
    /* Only the controller changes backing storage. The codec worker copies
     * each write into its own scratch block under the lock. */
    if (output.blocks[0].data) {
        portENTER_CRITICAL(&output_lock);
        while (blocks < limit && output.blocks[blocks].data)
            blocks++;
        last_output = output.report;
        pp_playback_reset(&output, generation, blocks * PP_PLAYBACK_BLOCK_BYTES);
        output_has_written = false;
        portEXIT_CRITICAL(&output_lock);
    } else {
        pp_buffer_t buffers[PP_PLAYBACK_BLOCKS] = {0};
        if (!pp_buffers_acquire(buffers, blocks, PP_PLAYBACK_BLOCK_BYTES,
                                PP_MEMORY_CRITICAL, PP_MEMORY_PLAYBACK))
            return 0;
        if (flash_allowed)
            pp_stream_prepare(&output.queue, AUDIO_HEAP_RESERVE);
        while (blocks < limit &&
               pp_buffers_try_acquire(&buffers[blocks], 1, PP_PLAYBACK_BLOCK_BYTES,
                                      AUDIO_HEAP_RESERVE, PP_MEMORY_PLAYBACK))
            blocks++;
        portENTER_CRITICAL(&output_lock);
        memcpy(output.blocks, buffers, sizeof(output.blocks));
        pp_playback_reset(&output, generation, blocks * PP_PLAYBACK_BLOCK_BYTES);
        output_has_written = false;
        portEXIT_CRITICAL(&output_lock);
    }
    xTaskNotifyGive(worker);
    return blocks * PP_PLAYBACK_BLOCK_BYTES;
}
void pp_audio_play_stop(void) {
    pp_buffer_t returned[PP_PLAYBACK_BLOCKS];
    portENTER_CRITICAL(&output_lock);
    if (output.report.window_bytes)
        last_output = output.report;
    pp_playback_reset(&output, 0, 0);
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
    unsigned bytes = output.credit + pp_stream_take_released(&output.queue);
    output.credit = 0;
    portEXIT_CRITICAL(&output_lock);
    return bytes;
}
pp_playback_report_t pp_audio_playback_report(void) {
    portENTER_CRITICAL(&output_lock);
    pp_playback_report_t report = output.report.window_bytes ? output.report : last_output;
    portEXIT_CRITICAL(&output_lock);
    return report;
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

static int cue_sine(unsigned phase) {
    static const int16_t wave[] = {
        0, 995, 1838, 2402, 2600, 2402, 1838, 995,
        0, -995, -1838, -2402, -2600, -2402, -1838, -995,
    };
    const unsigned index = (phase >> 12) & 15u;
    const int fraction = (int)(phase & 4095u);
    return wave[index] + (wave[(index + 1) & 15u] - wave[index]) * fraction / 4096;
}

static bool play_record_cue(codec_state_t *codec, int16_t *pcm, bool opening) {
    const unsigned volume = target_volume;
    if (!volume)
        return true;
    if (!set_format(codec, PP_CAPTURE_RATE, volume))
        return false;
    /* The press is a damped tap. Release is a soft puff with a late crest.
     * Keep press latency at 32 ms and reuse the same buffer for release. */
    const unsigned samples = opening ? AUDIO_WORK_SAMPLES : 2 * AUDIO_WORK_SAMPLES;
    const unsigned attack = (opening ? 5u : 46u) * (PP_CAPTURE_RATE / 1000);
    const unsigned tap_step = (1060u << 16) / PP_CAPTURE_RATE;
    const unsigned overtone_step = (1520u << 16) / PP_CAPTURE_RATE;
    const size_t block_bytes = AUDIO_WORK_SAMPLES * sizeof(*pcm);
    uint32_t noise = 1;
    int low = 0, band = 0;
    bool queued = false;
    for (unsigned offset = 0; offset < samples; offset += AUDIO_WORK_SAMPLES) {
        for (unsigned i = 0; i < AUDIO_WORK_SAMPLES; i++) {
            const unsigned at = offset + i;
            const int ramp = (int)(at < attack ? at * 256 / attack
                                  : (samples - 1 - at) * 256 / (samples - 1 - attack));
            const int envelope = ramp * ramp * (768 - 2 * ramp) / (256 * 256);
            if (opening) {
                /* Two short resonances keep the tap audible on the small speaker. */
                const int body = 5 * cue_sine(at * tap_step) / 4 + cue_sine(at * overtone_step) / 6;
                pcm[i] = body * envelope / 256;
            } else {
                /* Damped band-pass around 2 kHz gives the puff its airy texture. */
                noise = noise * 1664525u + 1013904223u;
                low += band * 3 / 4;
                const int high = (int)(noise >> 20) - 2048 - low - band * 7 / 8;
                band += high * 3 / 4;
                pcm[i] = band * envelope * 3 / (256 * 4);
            }
        }
        size_t written = 0;
        const int result = bsp_audio_write(pcm, block_bytes, &written);
        queued |= written > 0;
        if (result != ESP_OK || written != block_bytes)
            break;
    }
    const uint32_t position = bsp_audio_tx_position();
    /* Drain RX while the cue leaves the speaker so queued mic blocks cannot
     * carry the cue into this recording or the next one. */
    bool ok = true;
    for (unsigned i = 0; queued && i < 10 && !bsp_audio_tx_drained(position); i++) {
        if (bsp_audio_read(pcm, PP_CAPTURE_BYTES) != ESP_OK) {
            ok = false;
            break;
        }
    }
    if (!set_volume(codec, 0))
        return false;
    /* Discard the in-flight frame and give the acoustic tail a frame to settle. */
    for (unsigned i = 0; queued && ok && i < 2; i++)
        ok = bsp_audio_read(pcm, PP_CAPTURE_BYTES) == ESP_OK;
    return ok;
}

static int pcm_sample(const unsigned char *bytes) {
    return (int16_t)(bytes[0] | ((unsigned)bytes[1] << 8));
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
    if (!pp_stream_append(&capture, block.pcm, sizeof(block.pcm))) {
        /* Publish the fault with the stopped capture, before the controller
         * can drain it or start a different generation. */
        fault = PP_AUDIO_CAPTURE_OVERFLOW;
        capturing = false;
        portEXIT_CRITICAL(&record_lock);
        return PP_AUDIO_CAPTURE_OVERFLOW;
    }
    portEXIT_CRITICAL(&record_lock);
    /* A DC offset is not acoustic activity. Remove it from the meter and
     * cue decision while keeping the transmitted PCM unchanged. */
    enum { STRIDE = 16, POINTS = PP_CAPTURE_BYTES / STRIDE };
    int mean = 0;
    for (unsigned i = 0; i < sizeof(block.pcm); i += STRIDE)
        mean += pcm_sample(&block.pcm[i]);
    mean /= POINTS;
    unsigned amplitude = 0;
    for (unsigned i = 0; i < sizeof(block.pcm); i += STRIDE) {
        const int sample = pcm_sample(&block.pcm[i]) - mean;
        amplitude += sample < 0 ? -sample : sample;
    }
    input_level = amplitude / POINTS / 12;
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
    unsigned preroll_generation = 0;
    bool previous_loud = false, early_voice = false;
    for (;;) {
        if (pause_requested || codec_failed) {
            parked = true;
            ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
            continue;
        }
        parked = false;
        busy = true;
        int16_t pcm[AUDIO_WORK_SAMPLES];
        capture_reading = true;
        if (capturing) {
            unsigned generation = capture_generation;
            if (preroll_generation != generation) {
                preroll_generation = generation;
                previous_loud = early_voice = false;
            }
            unsigned cue_generation = start_cue_generation;
            if (cue_generation && cue_generation == generation) {
                capture_reading = false;
                /* Protect words that already arrived in pre-roll. Otherwise
                 * play the start cue before accepting the next mic frame. */
                if (!early_voice && capturing && !play_record_cue(&codec, pcm, true))
                    disable_codec();
                atomic_compare_exchange_strong(&start_cue_generation, &generation, 0);
                busy = false;
                continue;
            }
            pp_audio_error_t error = read_block(&codec, generation);
            if (error == PP_AUDIO_CODEC_FAILURE)
                disable_codec();
            else if (error == PP_AUDIO_OK && generation == capture_generation) {
                /* A bump at the beginning of the hold must not suppress the
                 * cue later. Keep protecting speech near confirmation. */
                const unsigned level = input_level;
                const bool loud = level >= EARLY_VOICE_LEVEL;
                early_voice = level >= EARLY_VOICE_STRONG_LEVEL || (loud && previous_loud);
                previous_loud = loud;
            }
            capture_reading = busy = false;
            continue;
        }
        capture_reading = false;
        if (atomic_exchange(&end_cue_pending, false)) {
            if (!play_record_cue(&codec, pcm, false))
                disable_codec();
            busy = false;
            continue;
        }
        uint32_t epoch = 0;
        uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
        portENTER_CRITICAL(&output_lock);
        bool dma_empty = output_has_written && bsp_audio_tx_drained(output_tx_position);
        size_t n = pp_playback_take(&output, pcm, sizeof(pcm), dma_empty, now_ms, &epoch);
        bool active = output.active;
        /* Poll DMA drain and prefill deadlines even between WebSocket messages. */
        bool monitor_drain = active && ((!output.ended && !output.priming) || output.queued);
        portEXIT_CRITICAL(&output_lock);
        if (n) {
            size_t written = 0;
            bool ok = set_format(&codec, PP_PLAYBACK_RATE, target_volume) &&
                      bsp_audio_write(pcm, n, &written) == ESP_OK;
            uint32_t position = bsp_audio_tx_position();
            portENTER_CRITICAL(&output_lock);
            if (ok && epoch == output.epoch) {
                output_tx_position = position;
                output_has_written = true;
            }
            pp_playback_written(&output, epoch, pcm, written, ok);
            portEXIT_CRITICAL(&output_lock);
            if (!ok)
                disable_codec();
        } else if (!active && codec.rate && !set_volume(&codec, 0)) {
            disable_codec();
        }
        busy = false;
        if (!n && !codec_failed)
            ulTaskNotifyTake(pdTRUE, monitor_drain ? pdMS_TO_TICKS(10) : portMAX_DELAY);
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

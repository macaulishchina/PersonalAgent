#pragma once
#include "playback_buffer.h"
#include "terminal_limits.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    unsigned generation;
    unsigned char pcm[PP_CAPTURE_BYTES];
} pp_recording_t;

#define PP_CAPTURE_FRAMES 32u
#define PP_CAPTURE_MAX_FRAMES 64u
bool pp_audio_start(unsigned volume);
bool pp_audio_quiesce(void);
void pp_audio_resume(void);
bool pp_audio_capture_begin(void);
void pp_audio_capture_confirm(void);
void pp_audio_recording_released(void);
void pp_audio_capture_release(void); /* Nonblocking, also called by the key callback. */
void pp_audio_capture_discard(void);
bool pp_audio_capturing(void);
bool pp_audio_capture_ready(void);
bool pp_audio_recorded(pp_recording_t *block);
unsigned pp_audio_recorded_count(void);
unsigned pp_audio_recording_capacity(void);
bool pp_audio_capture_drained(void);
unsigned pp_audio_play_begin(uint32_t generation, unsigned max_window);
void pp_audio_play_stop(void);
bool pp_play_pcm(uint32_t generation, const void *pcm, size_t bytes);
void pp_audio_play_end(uint32_t generation);
bool pp_audio_play_drained(void);
unsigned pp_audio_take_credit(void);
pp_playback_report_t pp_audio_playback_report(void);
typedef enum { PP_AUDIO_OK, PP_AUDIO_CAPTURE_OVERFLOW, PP_AUDIO_CODEC_FAILURE } pp_audio_error_t;
pp_audio_error_t pp_audio_take_error(void);
bool pp_audio_busy(void);
unsigned pp_audio_level(void);
void pp_audio_set_volume(unsigned volume);
unsigned pp_audio_volume(void);

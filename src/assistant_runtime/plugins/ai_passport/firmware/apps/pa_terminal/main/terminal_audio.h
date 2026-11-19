#pragma once
#include "terminal_limits.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    unsigned generation;
    unsigned char pcm[PP_CAPTURE_BYTES];
} pp_recording_t;

#define PP_CAPTURE_FRAMES 32u
bool pp_audio_start(unsigned volume);
bool pp_audio_quiesce(void);
bool pp_audio_capture_begin(void);
void pp_audio_capture_release(void); /* Nonblocking, also called by the key callback. */
void pp_audio_capture_discard(void);
bool pp_audio_capturing(void);
bool pp_audio_recorded(pp_recording_t *block);
unsigned pp_audio_recorded_count(void);
bool pp_audio_capture_drained(void);
bool pp_audio_play_begin(uint32_t generation);
void pp_audio_play_stop(void);
bool pp_play_pcm(uint32_t generation, const void *pcm, size_t bytes);
bool pp_audio_play_drained(void);
unsigned pp_audio_take_credit(void);
typedef enum { PP_AUDIO_OK, PP_AUDIO_CAPTURE_OVERFLOW, PP_AUDIO_CODEC_FAILURE } pp_audio_error_t;
pp_audio_error_t pp_audio_take_error(void);
bool pp_audio_busy(void);
unsigned pp_audio_level(void);
void pp_audio_set_volume(unsigned volume);
unsigned pp_audio_volume(void);

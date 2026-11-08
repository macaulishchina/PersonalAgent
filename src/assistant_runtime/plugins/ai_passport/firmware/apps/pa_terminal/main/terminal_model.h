#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    PP_NONE = 0,
    PP_CAPTURE = 1,
    PP_BEGIN = 2,
    PP_FINISH = 4,
    PP_DISCARD = 8,
    PP_MENU = 16,
    PP_TOGGLE = 32,
    PP_STOP = 64
} pp_action_t;
typedef struct {
    bool down, recording, second, released, wake_guard, stopped;
    uint32_t pressed, clicked;
} pp_button_t;
unsigned pp_press(pp_button_t *b, uint32_t now, bool playing);
unsigned pp_release(pp_button_t *b, uint32_t now);
unsigned pp_tick(pp_button_t *b, uint32_t now);
/* Copy a UTF-8 prefix without breaking a code point. Invalid sequences become '?'. */
size_t pp_text(char *out, size_t cap, const char *text);

/* One owner advances input. Audio/network workers carry generation, never
 * mutate this state. A key release stops capture even while ASR is opening. */
typedef enum {
    PP_INPUT_IDLE,
    PP_INPUT_PREBUFFER,
    PP_INPUT_OPENING,
    PP_INPUT_RECORDING,
    PP_INPUT_DRAINING,
    PP_INPUT_FINALIZING,
    PP_INPUT_CONFIRM,
    PP_INPUT_SUBMITTING,
    PP_INPUT_UNKNOWN
} pp_input_phase_t;
typedef struct {
    pp_input_phase_t phase;
    uint32_t generation, started;
    uint32_t max_audio_bytes, audio_bytes;
    bool released, limited;
} pp_input_t;
bool pp_input_start(pp_input_t *input, uint32_t now);
bool pp_input_begin(pp_input_t *input);
bool pp_input_ready(pp_input_t *input, uint32_t generation, unsigned max_audio_bytes);
/* Count only frames admitted to transport; hard audio limits do not depend on
 * the microphone clock matching the wall clock exactly. */
bool pp_input_audio(pp_input_t *input, unsigned bytes);
void pp_input_release(pp_input_t *input);
bool pp_input_limit(pp_input_t *input, uint32_t now, uint32_t limit_ms);
bool pp_input_drained(pp_input_t *input);
bool pp_input_transcript(pp_input_t *input, uint32_t generation);
bool pp_input_confirm(pp_input_t *input);
/* Caller has reconciled the saved request ID with its authoritative receipt. */
void pp_input_restore_confirmation(pp_input_t *input);
bool pp_input_done(pp_input_t *input, uint32_t generation);
void pp_input_abort(pp_input_t *input);
bool pp_input_capturing(const pp_input_t *input);
bool pp_input_listening(const pp_input_t *input);
const char *pp_input_name(pp_input_phase_t phase);

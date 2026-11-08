#include "terminal_model.h"
#include <string.h>
unsigned pp_press(pp_button_t *b, uint32_t now, bool playing) {
    if (b->wake_guard || b->down)
        return PP_NONE;
    b->second = b->released && now - b->clicked <= 300;
    b->stopped = playing;
    b->released = false;
    b->down = true;
    b->recording = false;
    b->pressed = now;
    return PP_CAPTURE | (playing ? PP_STOP : 0);
}
unsigned pp_release(pp_button_t *b, uint32_t now) {
    if (b->wake_guard) {
        b->wake_guard = false;
        return PP_NONE;
    }
    if (!b->down)
        return PP_NONE;
    b->down = false;
    if (b->recording) {
        b->recording = false;
        return PP_FINISH;
    }
    if (b->second) {
        b->second = false;
        return PP_DISCARD | PP_TOGGLE;
    }
    b->clicked = now;
    b->released = true;
    return PP_DISCARD;
}
unsigned pp_tick(pp_button_t *b, uint32_t now) {
    if (b->down && !b->recording && now - b->pressed >= 200) {
        b->recording = true;
        return PP_BEGIN;
    }
    if (b->released && now - b->clicked > 300) {
        b->released = false;
        return b->stopped ? PP_NONE : PP_MENU;
    }
    return PP_NONE;
}
size_t pp_text(char *out, size_t cap, const char *text) {
    size_t n = 0;
    if (!cap)
        return 0;
    while (*text) {
        unsigned c = (unsigned char)*text;
        size_t width = c < 128                  ? 1
                       : c >= 0xc2 && c <= 0xdf ? 2
                       : c >= 0xe0 && c <= 0xef ? 3
                       : c >= 0xf0 && c <= 0xf4 ? 4
                                                : 0;
        bool valid = width != 0;
        for (size_t i = 1; valid && i < width; i++)
            if (!text[i] || ((unsigned char)text[i] & 0xc0) != 0x80)
                valid = false;
        if (valid && width == 3 &&
            ((c == 0xe0 && (unsigned char)text[1] < 0xa0) ||
             (c == 0xed && (unsigned char)text[1] >= 0xa0)))
            valid = false;
        if (valid && width == 4 &&
            ((c == 0xf0 && (unsigned char)text[1] < 0x90) ||
             (c == 0xf4 && (unsigned char)text[1] >= 0x90)))
            valid = false;
        if (!valid) {
            if (n + 1 >= cap)
                break;
            out[n++] = '?';
            text++;
        } else {
            if (n + width >= cap)
                break;
            memcpy(out + n, text, width);
            n += width;
            text += width;
        }
    }
    out[n] = 0;
    return n;
}

bool pp_input_start(pp_input_t *s, uint32_t now) {
    if (s->phase != PP_INPUT_IDLE)
        return false;
    s->generation++;
    s->started = now;
    s->max_audio_bytes = 60u * 32000u;
    s->audio_bytes = 0;
    s->released = s->limited = false;
    s->phase = PP_INPUT_PREBUFFER;
    return true;
}
bool pp_input_begin(pp_input_t *s) {
    if (s->phase != PP_INPUT_PREBUFFER || s->released)
        return false;
    s->phase = PP_INPUT_OPENING;
    return true;
}
bool pp_input_ready(pp_input_t *s, uint32_t generation, unsigned max_audio_bytes) {
    if (s->generation != generation || s->phase != PP_INPUT_OPENING || max_audio_bytes < 32000 ||
        max_audio_bytes > 120u * 32000u || max_audio_bytes % 32000)
        return false;
    if (max_audio_bytes < s->max_audio_bytes)
        s->max_audio_bytes = max_audio_bytes;
    s->phase = s->released ? PP_INPUT_DRAINING : PP_INPUT_RECORDING;
    return true;
}
bool pp_input_audio(pp_input_t *s, unsigned bytes) {
    if ((s->phase != PP_INPUT_RECORDING && s->phase != PP_INPUT_DRAINING) || !bytes || bytes % 2 ||
        bytes > s->max_audio_bytes - s->audio_bytes)
        return false;
    s->audio_bytes += bytes;
    if (s->audio_bytes == s->max_audio_bytes) {
        s->limited = true;
        pp_input_release(s);
    }
    return true;
}
void pp_input_release(pp_input_t *s) {
    s->released = true;
    if (s->phase == PP_INPUT_PREBUFFER)
        pp_input_abort(s);
    else if (s->phase == PP_INPUT_RECORDING)
        s->phase = PP_INPUT_DRAINING;
}
bool pp_input_limit(pp_input_t *s, uint32_t now, uint32_t limit_ms) {
    if (s->max_audio_bytes / 32 < limit_ms)
        limit_ms = s->max_audio_bytes / 32;
    if (!pp_input_capturing(s) || s->phase == PP_INPUT_PREBUFFER || now - s->started < limit_ms)
        return false;
    s->limited = true;
    pp_input_release(s);
    return true;
}
bool pp_input_drained(pp_input_t *s) {
    if (s->phase != PP_INPUT_DRAINING)
        return false;
    s->phase = PP_INPUT_FINALIZING;
    return true;
}
bool pp_input_transcript(pp_input_t *s, uint32_t generation) {
    if (s->generation != generation || s->phase != PP_INPUT_FINALIZING)
        return false;
    s->phase = s->limited ? PP_INPUT_CONFIRM : PP_INPUT_SUBMITTING;
    return true;
}
bool pp_input_confirm(pp_input_t *s) {
    if (s->phase != PP_INPUT_CONFIRM)
        return false;
    s->phase = PP_INPUT_SUBMITTING;
    return true;
}
void pp_input_restore_confirmation(pp_input_t *s) {
    s->generation++;
    s->phase = PP_INPUT_CONFIRM;
    s->released = true;
    s->limited = true;
}
bool pp_input_done(pp_input_t *s, uint32_t generation) {
    if (s->generation != generation ||
        (s->phase != PP_INPUT_SUBMITTING && s->phase != PP_INPUT_UNKNOWN))
        return false;
    s->phase = PP_INPUT_IDLE;
    return true;
}
void pp_input_abort(pp_input_t *s) {
    s->generation++;
    s->phase = PP_INPUT_IDLE;
    s->released = true;
    s->limited = false;
}
bool pp_input_capturing(const pp_input_t *s) {
    return !s->released && (s->phase == PP_INPUT_PREBUFFER || s->phase == PP_INPUT_OPENING ||
                            s->phase == PP_INPUT_RECORDING);
}
const char *pp_input_name(pp_input_phase_t phase) {
    static const char *names[] = {"idle",       "prebuffer", "opening",    "recording", "draining",
                                  "finalizing", "confirm",   "submitting", "unknown"};
    return (unsigned)phase < sizeof(names) / sizeof(names[0]) ? names[phase] : "invalid";
}
bool pp_input_listening(const pp_input_t *s) {
    return s->phase != PP_INPUT_PREBUFFER && pp_input_capturing(s);
}

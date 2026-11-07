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

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

#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    PP_UI_IDLE,
    PP_UI_INPUT,
    PP_UI_REPLY,
    PP_UI_INFO,
    PP_UI_MENU,
    PP_UI_IMAGE
} pp_ui_view_t;
typedef struct {
    pp_ui_view_t view;
    const char *body, *state, *hint, *origin;
    const char *const *choices;
    unsigned choice_count, selection;
    bool queue, follow, online, volume_feedback, task_context;
    unsigned volume, level, elapsed, now, brightness, text_size;
    int battery, wifi;
    size_t page;
} pp_ui_frame_t;
bool pp_ui_init(void);
/* Returns the next local page's UTF-8 offset, zero at the end. */
size_t pp_ui_render(const pp_ui_frame_t *frame);
size_t pp_ui_previous(const char *text, size_t start);
/* Removes the old image while holding the render lock before the caller frees it. */
bool pp_ui_image(const void *pixels, unsigned width, unsigned height);

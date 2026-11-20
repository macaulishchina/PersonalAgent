#pragma once
#include "status_meter.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    PP_UI_IDLE,
    PP_UI_INPUT,
    PP_UI_REPLY,
    PP_UI_INFO,
    PP_UI_MENU,
    PP_UI_IMAGE,
    PP_UI_BRIGHTNESS
} pp_ui_view_t;
typedef struct {
    pp_ui_view_t view;
    pp_meter_t meter;
    const char *body, *state, *hint, *origin;
    const char *const *choices;
    const char *menu_detail;
    unsigned choice_count, selection;
    bool queue, follow, online, volume_feedback, task_context, local_page_hint, awaiting_reply,
        wifi_searching, menu_grid;
    unsigned volume, level, elapsed, now, brightness, text_size;
    int battery, wifi;
    size_t page;
} pp_ui_frame_t;
bool pp_ui_init(void);
void pp_ui_blank(void);
bool pp_ui_standby(bool enabled);
/* Returns the next local page's UTF-8 offset, zero at the end. */
size_t pp_ui_render(const pp_ui_frame_t *frame);
size_t pp_ui_previous(const char *text, size_t start);
/* Removes the old image while holding the render lock before the caller frees it. */
bool pp_ui_image(const void *pixels, unsigned width, unsigned height);
/* Poll begin/draw until completion. Draw borrows pixels until completion or pp_ui_image(NULL).
 * A busy renderer is retried by the controller rather than blocking a key handler. */
bool pp_ui_stream_begin(unsigned width, unsigned height);
bool pp_ui_stream_draw(const void *pixels, unsigned row, unsigned rows);

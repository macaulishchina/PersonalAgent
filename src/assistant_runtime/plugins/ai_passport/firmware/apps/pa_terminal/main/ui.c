#include "ui.h"
#include "status_widgets.h"
#include "bsp_display.h"
#include "bsp_pins.h"
#include "image_model.h"
#include "terminal_limits.h"
#include "text_layout.h"
#include "lvgl.h"
#include "src/misc/cache/instance/lv_image_cache.h"
#include <stdio.h>
#include <string.h>
#include <time.h>

LV_FONT_DECLARE(terminal_zh_16);
LV_FONT_DECLARE(terminal_zh_18);
LV_FONT_DECLARE(terminal_battery_12);
#define ASSET(name) extern const uint8_t name[] asm("_binary_" #name "_start")
ASSET(cat_idle_open_rgb565a8);
ASSET(cat_idle_blink_rgb565a8);
ASSET(cat_input_open_rgb565a8);
ASSET(cat_input_blink_rgb565a8);
#define AVATAR(data_, width_, height_)                                                             \
    {.header = {.magic = LV_IMAGE_HEADER_MAGIC,                                                    \
                .cf = LV_COLOR_FORMAT_RGB565A8,                                                    \
                .w = width_,                                                                       \
                .h = height_,                                                                      \
                .stride = width_ * 2},                                                             \
     .data_size = width_ * height_ * 3,                                                            \
     .data = data_}
static const lv_image_dsc_t idle_open = AVATAR(cat_idle_open_rgb565a8, 120, 144);
static const lv_image_dsc_t idle_blink = AVATAR(cat_idle_blink_rgb565a8, 120, 144);
static const lv_image_dsc_t input_open = AVATAR(cat_input_open_rgb565a8, 60, 72);
static const lv_image_dsc_t input_blink = AVATAR(cat_input_blink_rgb565a8, 60, 72);
static lv_image_dsc_t photo_descriptor;
static lv_image_dsc_t strip_descriptor;
static lv_area_t strip_area;
static const lv_area_t image_viewport = {12, 66, 12 + PP_IMAGE_VIEW_WIDTH - 1,
                                       66 + PP_IMAGE_VIEW_HEIGHT - 1};
static lv_area_t stream_area;
static lv_display_t *display;
static lv_obj_t *screen, *clock_label, *wifi, *volume, *battery, *mode;
static lv_obj_t *origin, *state, *content, *footer, *avatar, *photo, *strip_photo, *bars[5], *items[7];
static lv_obj_t *volume_track, *volume_fill;
static pp_ui_view_t previous_view = (pp_ui_view_t)-1;
static bool previous_menu_detail, previous_menu_grid;
static const lv_image_dsc_t *previous_avatar;
static unsigned filtered_level;
static bool photo_available;
static enum { STREAM_INACTIVE, STREAM_CLEARING, STREAM_READY } stream_view;
static bool invalidating_strip, strip_done;
static const lv_font_t *body_font = &terminal_zh_16;
/* Backlight starts off until the first populated frame has been drawn. */
static unsigned current_brightness;
enum { BATTERY_BODY_W = 25, BATTERY_H = 13, BATTERY_CAP_W = 2, BATTERY_CAP_GAP = 1 };
static int battery_percent = -1;
static char battery_value[4] = "--";

void pp_ui_blank(void) {
    bsp_display_backlight(0);
    current_brightness = 0;
}

bool pp_ui_standby(bool enabled) {
    if (bsp_lvgl_standby(enabled) != ESP_OK)
        return false;
    current_brightness = 0; /* The first complete wake frame restores the configured brightness. */
    return true;
}

static unsigned body_rows(bool input) {
    return (input ? 110u : 198u) / (body_font->line_height + 1);
}

static lv_obj_t *label(int x, int y, int w, int h, bool symbols) {
    lv_obj_t *object = lv_label_create(screen);
    lv_label_set_text_static(object, "");
    lv_obj_set_pos(object, x, y);
    lv_obj_set_size(object, w, h);
    lv_obj_set_style_text_font(object, symbols ? &lv_font_montserrat_14 : &terminal_zh_16, 0);
    lv_label_set_long_mode(object, LV_LABEL_LONG_CLIP);
    /* The installed font's line box is 21px, including its baseline. */
    lv_obj_set_style_text_line_space(object, 1, 0);
    return object;
}
static void text(lv_obj_t *object, const char *value) {
    if (strcmp(lv_label_get_text(object), value ? value : ""))
        lv_label_set_text(object, value ? value : "");
}
static void visible(lv_obj_t *object, bool show) {
    if (show)
        lv_obj_remove_flag(object, LV_OBJ_FLAG_HIDDEN);
    else
        lv_obj_add_flag(object, LV_OBJ_FLAG_HIDDEN);
}
static void text_color(lv_obj_t *object, uint32_t rgb) {
    lv_color_t color = lv_color_hex(rgb);
    if (!lv_color_eq(lv_obj_get_style_text_color(object, 0), color))
        lv_obj_set_style_text_color(object, color, 0);
}
static void draw_battery(lv_event_t *event) {
    lv_area_t body;
    lv_obj_get_coords(lv_event_get_current_target(event), &body);
    body.x2 = body.x1 + BATTERY_BODY_W - 1;
    const lv_area_t cap = {body.x2 + BATTERY_CAP_GAP + 1, body.y1 + 4,
                           body.x2 + BATTERY_CAP_GAP + BATTERY_CAP_W, body.y1 + 8};
    lv_layer_t *layer = lv_event_get_layer(event);
    lv_draw_rect_dsc_t rect;
    lv_draw_rect_dsc_init(&rect);
    rect.bg_opa = LV_OPA_COVER;
    rect.bg_color = lv_color_hex(0x718479);
    rect.radius = 1;
    lv_draw_rect(layer, &rect, &cap);
    rect.bg_color = lv_color_hex(0x46594f);
    rect.radius = 3;
    lv_draw_rect(layer, &rect, &body);

    lv_draw_label_dsc_t digits;
    lv_draw_label_dsc_init(&digits);
    digits.text = battery_value;
    digits.font = &terminal_battery_12;
    digits.align = LV_TEXT_ALIGN_CENTER;
    const int number_y = body.y1 + (BATTERY_H - digits.font->line_height) / 2;
    const lv_area_t number = {body.x1, number_y, body.x2, number_y + digits.font->line_height - 1};
    /* The same centered text crosses both halves of the rounded fill. */
    const lv_area_t original_clip = layer->_clip_area;
    const lv_area_t clip = {LV_MAX(body.x1, original_clip.x1), LV_MAX(body.y1, original_clip.y1),
                            LV_MIN(body.x2, original_clip.x2), LV_MIN(body.y2, original_clip.y2)};
    if (clip.x1 > clip.x2 || clip.y1 > clip.y2)
        return;
    const int width = battery_percent > 0 ? LV_MAX(1, battery_percent * BATTERY_BODY_W / 100) : 0;
    const int split = body.x1 + width;
    if (clip.x1 < split) {
        layer->_clip_area = clip;
        layer->_clip_area.x2 = LV_MIN(clip.x2, split - 1);
        rect.bg_color = lv_color_hex(battery_percent <= 20 ? 0xed817a : 0xdce8da);
        lv_draw_rect(layer, &rect, &body);
        digits.color = lv_color_hex(0x172923);
        lv_draw_label(layer, &digits, &number);
    }
    if (clip.x2 >= split) {
        layer->_clip_area = clip;
        layer->_clip_area.x1 = LV_MAX(clip.x1, split);
        digits.color = lv_color_hex(battery_percent == 0 ? 0xffc4be : 0xf1f5ed);
        lv_draw_label(layer, &digits, &number);
    }
    layer->_clip_area = original_clip;
}
static void update_battery(int percent) {
    percent = percent < 0 ? -1 : LV_MIN(percent, 100);
    if (percent == battery_percent)
        return;
    battery_percent = percent;
    if (percent < 0)
        strcpy(battery_value, "--");
    else
        snprintf(battery_value, sizeof(battery_value), "%u", (unsigned)percent);
    lv_obj_invalidate(battery);
}
static void activity_indicator(const pp_ui_frame_t *f, bool input) {
    const bool show_wait = f->awaiting_reply && !f->volume_feedback;
    const bool show_level = !show_wait && input && f->elapsed && f->level;
    const unsigned active_dot = f->now / 240 % 3;
    const unsigned weights[] = {5, 9, 12, 9, 5};
    filtered_level = (filtered_level * 3 + (f->level > 1024 ? 1024 : f->level)) / 4;
    /* The microphone bars double as three dots while waiting for a reply. */
    for (unsigned i = 0; i < 5; i++) {
        const bool dot = show_wait && i < 3;
        visible(bars[i], dot || show_level);
        if (!dot && !show_level)
            continue;
        const unsigned height = dot ? 4 : 2 + filtered_level * weights[i] / 1024;
        lv_obj_set_pos(bars[i], dot ? 86 + i * 9 : 101 + i * 8, dot ? 273 : 114 - height / 2);
        lv_obj_set_size(bars[i], dot ? 4 : 3, height);
        const lv_color_t color = lv_color_hex(!dot || i == active_dot ? 0xb6cfa4 : 0x526158);
        if (!lv_color_eq(lv_obj_get_style_bg_color(bars[i], 0), color))
            lv_obj_set_style_bg_color(bars[i], color, 0);
    }
}
static void rule(int y) {
    lv_obj_t *object = lv_obj_create(screen);
    lv_obj_remove_style_all(object);
    lv_obj_set_pos(object, 12, y);
    lv_obj_set_size(object, 216, 1);
    lv_obj_set_style_bg_color(object, lv_color_hex(0x34443c), 0);
    lv_obj_set_style_bg_opa(object, LV_OPA_COVER, 0);
}
static unsigned measure(uint32_t c, void *context) {
    (void)context;
    lv_font_glyph_dsc_t glyph;
    if (lv_font_get_glyph_dsc(body_font, &glyph, c, 0))
        return glyph.adv_w;
    return 16;
}

static void protect_stream_pixels(lv_event_t *event) {
    if (stream_view != STREAM_READY || invalidating_strip)
        return;
    lv_area_t *area = lv_event_get_param(event);
    /* LVGL invalidates widget bounds, in absolute screen coordinates. Never
     * recurse into invalidation from its event hook or hand it an empty area. */
    if (area->y2 < image_viewport.y1 || area->y1 > image_viewport.y2)
        return;
    if (area->y1 < image_viewport.y1)
        area->y2 = image_viewport.y1 - 1;
    else if (area->y2 > image_viewport.y2)
        area->y1 = image_viewport.y2 + 1;
    else
        *area = (lv_area_t){0, 0, 1, 0};
}
static void detach_strip(void) {
    visible(strip_photo, false);
    lv_image_set_src(strip_photo, NULL);
    lv_image_cache_drop(&strip_descriptor);
    strip_descriptor.data = NULL;
}
static void complete_image_refresh(lv_event_t *event) {
    (void)event;
    if (stream_view == STREAM_CLEARING)
        stream_view = STREAM_READY;
    if (strip_descriptor.data)
        strip_done = true;
}

bool pp_ui_init(void) {
    if (bsp_display_init() != ESP_OK || !(display = bsp_lvgl_init()) || !bsp_lvgl_lock(1000))
        return false;
    screen = lv_obj_create(NULL);
    lv_obj_remove_style_all(screen);
    lv_obj_set_size(screen, BSP_LCD_W, BSP_LCD_H);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x172923), 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(screen, lv_color_hex(0xe6eddf), 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    clock_label = label(12, 6, 44, 21, false);
    volume = label(58, 8, 20, 17, true);
    wifi = label(142, 8, 20, 17, true);
    battery = lv_obj_create(screen);
    lv_obj_remove_style_all(battery);
    lv_obj_set_pos(battery, 200, 9);
    lv_obj_set_size(battery, BATTERY_BODY_W + BATTERY_CAP_GAP + BATTERY_CAP_W, BATTERY_H);
    lv_obj_remove_flag(battery, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(battery, draw_battery, LV_EVENT_DRAW_MAIN, NULL);
    pp_status_widgets_create(screen);
    rule(29);
    rule(288);
    origin = label(12, 38, 216, 22, false);
    lv_label_set_long_mode(origin, LV_LABEL_LONG_DOT);
    state = label(12, 265, 216, 21, false);
    lv_obj_set_style_text_color(origin, lv_color_hex(0xb6cfa4), 0);
    lv_obj_set_style_text_color(state, lv_color_hex(0x9aaa99), 0);
    content = label(12, 66, 216, 198, false);
    footer = label(12, 294, 196, 22, false);
    lv_obj_set_style_text_color(footer, lv_color_hex(0xb3bfb0), 0);
    mode = label(212, 297, 18, 17, true);
    avatar = lv_image_create(screen);
    photo = lv_image_create(screen);
    lv_obj_set_pos(photo, image_viewport.x1, image_viewport.y1);
    lv_obj_set_size(photo, PP_IMAGE_VIEW_WIDTH, PP_IMAGE_VIEW_HEIGHT);
    /* LVGL fits the pixels proportionally using its small scan-line scratch buffer. */
    lv_image_set_inner_align(photo, LV_IMAGE_ALIGN_CONTAIN);
    visible(photo, false);
    strip_photo = lv_image_create(screen);
    lv_image_set_inner_align(strip_photo, LV_IMAGE_ALIGN_TOP_LEFT);
    visible(strip_photo, false);
    for (unsigned i = 0; i < 5; i++) {
        bars[i] = lv_obj_create(screen);
        lv_obj_remove_style_all(bars[i]);
        lv_obj_set_style_bg_color(bars[i], lv_color_hex(0xb6cfa4), 0);
        lv_obj_set_style_bg_opa(bars[i], LV_OPA_COVER, 0);
        lv_obj_set_style_radius(bars[i], 2, 0);
    }
    for (unsigned i = 0; i < 7; i++) {
        items[i] = label(18, 61 + i * 29, 204, 26, false);
        lv_label_set_long_mode(items[i], LV_LABEL_LONG_DOT);
        lv_obj_set_style_radius(items[i], 4, 0);
        lv_obj_set_style_pad_left(items[i], 6, 0);
        lv_obj_set_style_pad_top(items[i], 3, 0);
    }
    /* Reuse the status row, leaving all nine body lines and the footer intact.
     * Two cached rectangles; no per-key widget allocation or full-screen layer. */
    volume_track = lv_obj_create(screen);
    lv_obj_remove_style_all(volume_track);
    lv_obj_set_pos(volume_track, 108, 274);
    lv_obj_set_size(volume_track, 120, 4);
    lv_obj_set_style_radius(volume_track, 2, 0);
    lv_obj_set_style_bg_color(volume_track, lv_color_hex(0x34443c), 0);
    lv_obj_set_style_bg_opa(volume_track, LV_OPA_COVER, 0);
    lv_obj_remove_flag(volume_track, LV_OBJ_FLAG_SCROLLABLE);
    volume_fill = lv_obj_create(volume_track);
    lv_obj_remove_style_all(volume_fill);
    lv_obj_set_pos(volume_fill, 0, 0);
    lv_obj_set_size(volume_fill, 120, 4);
    lv_obj_set_style_radius(volume_fill, 2, 0);
    lv_obj_set_style_bg_color(volume_fill, lv_color_hex(0xb6cfa4), 0);
    lv_obj_set_style_bg_opa(volume_fill, LV_OPA_COVER, 0);
    visible(volume_track, false);
    lv_display_add_event_cb(display, protect_stream_pixels, LV_EVENT_INVALIDATE_AREA, NULL);
    lv_display_add_event_cb(display, complete_image_refresh, LV_EVENT_REFR_READY, NULL);
    lv_screen_load(screen);
    bsp_lvgl_unlock();
    return true;
}

bool pp_ui_image(const void *pixels, unsigned width, unsigned height) {
    if (!screen || !bsp_lvgl_lock(1000))
        return false;
    if (stream_view != STREAM_INACTIVE) {
        stream_view = STREAM_INACTIVE;
        detach_strip();
        strip_done = false;
        lv_obj_invalidate_area(screen, &image_viewport);
    }
    visible(photo, false);
    /* A descriptor points directly at the one owner-managed RGB565 buffer. */
    lv_image_cache_drop(&photo_descriptor);
    photo_available = pixels != NULL;
    if (pixels) {
        lv_obj_set_pos(photo, image_viewport.x1, image_viewport.y1);
        lv_obj_set_size(photo, PP_IMAGE_VIEW_WIDTH, PP_IMAGE_VIEW_HEIGHT);
        photo_descriptor = (lv_image_dsc_t){.header = {.magic = LV_IMAGE_HEADER_MAGIC,
                                                       .cf = LV_COLOR_FORMAT_RGB565,
                                                       .w = width,
                                                       .h = height,
                                                       .stride = width * 2},
                                            .data_size = width * height * 2,
                                            .data = pixels};
        lv_image_set_src(photo, &photo_descriptor);
    } else
        lv_image_set_src(photo, NULL);
    bsp_lvgl_unlock();
    return true;
}

bool pp_ui_stream_begin(unsigned width, unsigned height) {
    if (!screen || !width || width > PP_IMAGE_VIEW_WIDTH || !height ||
        height > PP_IMAGE_VIEW_HEIGHT || !bsp_lvgl_lock(1))
        return false;
    if (previous_view != PP_UI_IMAGE) {
        bsp_lvgl_unlock();
        return false;
    }
    if (stream_view != STREAM_INACTIVE) {
        bool ready = stream_view == STREAM_READY;
        bsp_lvgl_unlock();
        return ready;
    }
    stream_area.x1 = image_viewport.x1 + (PP_IMAGE_VIEW_WIDTH - width) / 2;
    stream_area.y1 = image_viewport.y1 + (PP_IMAGE_VIEW_HEIGHT - height) / 2;
    stream_area.x2 = stream_area.x1 + width - 1;
    stream_area.y2 = stream_area.y1 + height - 1;
    if (photo_available) {
        lv_obj_set_pos(photo, stream_area.x1, stream_area.y1);
        lv_obj_set_size(photo, width, height);
    }
    visible(photo, photo_available);
    stream_view = STREAM_CLEARING;
    lv_obj_invalidate_area(screen, &image_viewport);
    bsp_lvgl_unlock();
    return false;
}
bool pp_ui_stream_draw(const void *pixels, unsigned row, unsigned rows) {
    if (!pixels || !rows || !bsp_lvgl_lock(1))
        return false;
    if (stream_view != STREAM_READY || row + rows > (unsigned)lv_area_get_height(&stream_area)) {
        bsp_lvgl_unlock();
        return false;
    }
    if (strip_done) {
        /* Once rasterized, LCD I/O owns the separate LVGL flush buffer. */
        detach_strip();
        strip_done = false;
        bsp_lvgl_unlock();
        return true;
    }
    if (strip_descriptor.data) {
        bsp_lvgl_unlock();
        return false;
    }
    unsigned width = (unsigned)lv_area_get_width(&stream_area);
    strip_area = stream_area;
    strip_area.y1 += row;
    strip_area.y2 = strip_area.y1 + rows - 1;
    strip_descriptor = (lv_image_dsc_t){.header = {.magic = LV_IMAGE_HEADER_MAGIC,
                                                  .cf = LV_COLOR_FORMAT_RGB565,
                                                  .w = width, .h = rows, .stride = width * 2},
                                         .data_size = width * rows * 2, .data = pixels};
    lv_obj_set_pos(strip_photo, strip_area.x1, strip_area.y1);
    lv_obj_set_size(strip_photo, width, rows);
    lv_image_set_src(strip_photo, &strip_descriptor);
    visible(strip_photo, true);
    invalidating_strip = true;
    lv_obj_invalidate_area(screen, &strip_area);
    invalidating_strip = false;
    bsp_lvgl_unlock();
    return false;
}

size_t pp_ui_previous(const char *value, size_t start) {
    if (!bsp_lvgl_lock(100))
        return start;
    size_t offset = pp_layout_previous(value, start, 216, body_rows(false), measure, NULL);
    bsp_lvgl_unlock();
    return offset;
}

size_t pp_ui_render(const pp_ui_frame_t *f) {
    if (!screen || !bsp_lvgl_lock(100))
        return f->page;
    const bool brightness_edit = f->view == PP_UI_BRIGHTNESS;
    const lv_font_t *wanted_font = f->text_size == 18 ? &terminal_zh_18 : &terminal_zh_16;
    if (body_font != wanted_font || previous_view != f->view) {
        body_font = wanted_font;
        lv_obj_set_style_text_font(content, brightness_edit ? &terminal_zh_18 : body_font, 0);
    }
    struct tm clock;
    time_t now = time(NULL);
    /* Display local wall time (the device protocol uses UTC epochs). */
    now += 8 * 60 * 60;
    gmtime_r(&now, &clock);
    char value[64];
    if (now > 1700000000)
        snprintf(value, sizeof(value), "%02d:%02d", clock.tm_hour, clock.tm_min);
    else
        strcpy(value, "--:--");
    text(clock_label, value);
    text(volume, f->volume == 0   ? LV_SYMBOL_MUTE
                 : f->volume < 55 ? LV_SYMBOL_VOLUME_MID
                                  : LV_SYMBOL_VOLUME_MAX);
    text(wifi, LV_SYMBOL_WIFI);
    static const uint32_t wifi_pulse[] = {0x526158, 0x839278, 0xb6cfa4, 0x839278};
    text_color(wifi, f->wifi_searching ? wifi_pulse[f->now / 240 % 4]
                                      : f->wifi > -70 ? 0xb6cfa4
                                      : f->wifi > -90 ? 0x839278 : 0x526158);
    update_battery(f->battery);
    pp_status_widgets_update(&f->meter);
    text(mode, f->queue ? LV_SYMBOL_LIST : LV_SYMBOL_NEW_LINE);
    bool input = f->view == PP_UI_INPUT, idle = f->view == PP_UI_IDLE;
    bool menu_detail = f->view == PP_UI_MENU && f->menu_detail;
    visible(avatar, input || idle);
    visible(photo, f->view == PP_UI_IMAGE && photo_available);
    visible(origin, !idle);
    visible(content, menu_detail || (!idle && f->view != PP_UI_MENU && f->view != PP_UI_IMAGE));
    text_color(content, menu_detail ? 0x9aaa99 : 0xe6eddf);
    visible(state, true);
    text_color(origin, f->task_context ? 0xa7c7e8 : 0xb6cfa4);
    if (previous_view != f->view || previous_menu_detail != menu_detail) {
        lv_obj_set_pos(content, menu_detail ? 18 : 12,
                       brightness_edit ? 111 : menu_detail ? 222 : input ? 145 : 66);
        lv_obj_set_height(content, brightness_edit ? 33 : menu_detail ? 28 : input ? 110 : 198);
        lv_obj_set_style_text_align(content, brightness_edit ? LV_TEXT_ALIGN_CENTER : LV_TEXT_ALIGN_LEFT,
                                    0);
        lv_obj_set_pos(origin, 12, input ? 123 : 38);
    }
    if ((previous_view == PP_UI_BRIGHTNESS) != brightness_edit) {
        lv_obj_set_pos(volume_track, brightness_edit ? 36 : 108, brightness_edit ? 170 : 274);
        lv_obj_set_size(volume_track, brightness_edit ? 168 : 120, brightness_edit ? 6 : 4);
        lv_obj_set_height(volume_fill, brightness_edit ? 6 : 4);
    }
    previous_view = f->view;
    previous_menu_detail = menu_detail;
    if (idle || input) {
        unsigned blink_time = f->now % 11000;
        bool blink =
            (blink_time > 4200 && blink_time < 4350) || (blink_time > 8500 && blink_time < 8620);
        const lv_image_dsc_t *image =
            input ? (blink ? &input_blink : &input_open) : (blink ? &idle_blink : &idle_open);
        if (previous_avatar != image) {
            lv_image_set_src(avatar, image);
            previous_avatar = image;
        }
        lv_obj_set_pos(avatar, input ? 90 : 60, input ? 35 : 88);
    }
    activity_indicator(f, input);
    text(origin, input                       ? "你说的"
                 : f->origin && f->origin[0] ? f->origin
                 : f->view == PP_UI_MENU     ? "操作菜单"
                 : f->view == PP_UI_IMAGE    ? "图片"
                                             : "回复");
    if (f->volume_feedback && !brightness_edit)
        snprintf(value, sizeof(value), "音量 %u", f->volume);
    else if (input && f->elapsed)
        snprintf(value, sizeof(value), "%.1f 秒", f->elapsed / 1000.0);
    else if (idle || !f->origin || !f->state || strcmp(f->origin, f->state))
        snprintf(value, sizeof(value), "%s", f->state ? f->state : "");
    else
        value[0] = 0;
    text(state, value);
    text_color(state, f->volume_feedback ? 0xb6cfa4 : 0x9aaa99);
    visible(volume_track, f->volume_feedback || brightness_edit);
    if (brightness_edit) {
        const unsigned level = f->brightness - PP_BRIGHTNESS_MIN;
        visible(volume_fill, level > 0);
        lv_obj_set_width(volume_fill, level * 168 / (PP_BRIGHTNESS_MAX - PP_BRIGHTNESS_MIN));
    } else if (f->volume_feedback) {
        unsigned percent = f->volume > 100 ? 100 : f->volume;
        visible(volume_fill, percent > 0);
        lv_obj_set_width(volume_fill, percent * 120 / 100);
    }
    const bool grid = f->view == PP_UI_MENU && f->menu_grid;
    const unsigned first = f->selection >= 7 ? f->selection - 6 : 0;
    for (unsigned i = 0; i < 7; i++) {
        const bool tile = grid && i < PP_MENU_GRID_TILES;
        if (previous_menu_grid != grid) {
            lv_obj_set_pos(items[i], tile ? 18 + (i % 2) * 106 : 18,
                           tile ? 66 + (i / 2) * 66
                                : grid ? 198 + (i - PP_MENU_GRID_TILES) * 29 : 61 + i * 29);
            lv_obj_set_size(items[i], tile ? 98 : 204, tile ? 58 : 26);
            lv_obj_set_style_pad_top(items[i], tile ? 18 : 3, 0);
            lv_obj_set_style_pad_right(items[i], tile ? 6 : 0, 0);
            lv_obj_set_style_text_align(items[i], grid ? LV_TEXT_ALIGN_CENTER : LV_TEXT_ALIGN_LEFT, 0);
        }
        const bool show = f->view == PP_UI_MENU && first + i < f->choice_count;
        visible(items[i], show);
        if (show) {
            text(items[i], f->choices[first + i]);
            const bool selected = first + i == f->selection;
            const uint32_t color = f->task_context ? 0x263c47
                                   : grid ? (selected ? 0x355843 : 0x233c30)
                                          : 0x2a3c31;
            lv_obj_set_style_bg_color(items[i], lv_color_hex(color), 0);
            lv_obj_set_style_bg_opa(items[i], selected || tile ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
        }
    }
    previous_menu_grid = grid;
    char page[1152];
    const char *body = f->body ? f->body : "";
    pp_text_layout_t layout = {.next = f->page};
    if (brightness_edit)
        snprintf(page, sizeof(page), "%u%%", f->brightness);
    else if (f->follow || input)
        pp_layout_tail(page, sizeof(page), body, 216, body_rows(input), measure, NULL);
    else
        layout =
            pp_layout_page(page, sizeof(page), body, f->page, 216, body_rows(false), measure, NULL);
    text(content, menu_detail ? f->menu_detail : page);
    const char *hint = f->hint && f->hint[0] ? f->hint : "上下翻页 · OK 菜单";
    if (f->local_page_hint && (layout.more || f->page))
        hint = layout.more ? (f->page ? "上下翻页 · OK 返回" : "向下翻页 · OK 返回")
                           : "向上翻页 · OK 返回";
    text(footer, hint);
    const bool first_frame = current_brightness == 0;
    if (first_frame)
        lv_refr_now(lv_obj_get_display(screen));
    bsp_lvgl_unlock();
    unsigned brightness = f->brightness;
    if (brightness < PP_BRIGHTNESS_MIN || brightness > PP_BRIGHTNESS_MAX)
        brightness = first_frame ? PP_BRIGHTNESS_DEFAULT : current_brightness;
    if (brightness != current_brightness) {
        bsp_display_backlight(brightness);
        current_brightness = brightness;
    }
    return layout.more ? layout.next : 0;
}

#include "ui.h"
#include "bsp_display.h"
#include "bsp_pins.h"
#include "text_layout.h"
#include "lvgl.h"
#include "src/misc/cache/instance/lv_image_cache.h"
#include <stdio.h>
#include <string.h>
#include <time.h>

LV_FONT_DECLARE(terminal_zh_16);
LV_FONT_DECLARE(terminal_zh_18);
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
static lv_obj_t *screen, *clock_label, *wifi, *volume, *battery, *battery_text, *mode;
static lv_obj_t *origin, *state, *content, *footer, *avatar, *photo, *bars[5], *items[7];
static lv_obj_t *volume_track, *volume_fill;
static pp_ui_view_t previous_view = (pp_ui_view_t)-1;
static const lv_image_dsc_t *previous_avatar;
static unsigned filtered_level;
static bool photo_available;
static const lv_font_t *body_font = &terminal_zh_16;
static unsigned current_brightness = 65;

static unsigned body_rows(bool input) {
    return (input ? 110u : 198u) / (body_font->line_height + 1);
}

static lv_obj_t *label(int x, int y, int w, int h, bool symbols) {
    lv_obj_t *object = lv_label_create(screen);
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

bool pp_ui_init(void) {
    if (bsp_display_init() != ESP_OK || !bsp_lvgl_init() || !bsp_lvgl_lock(1000))
        return false;
    screen = lv_obj_create(NULL);
    lv_obj_remove_style_all(screen);
    lv_obj_set_size(screen, BSP_LCD_W, BSP_LCD_H);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x172923), 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(screen, lv_color_hex(0xe6eddf), 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    clock_label = label(12, 6, 65, 21, false);
    volume = label(144, 8, 20, 17, true);
    wifi = label(172, 8, 20, 17, true);
    battery = lv_obj_create(screen);
    lv_obj_remove_style_all(battery);
    lv_obj_set_pos(battery, 198, 6);
    lv_obj_set_size(battery, 29, 18);
    lv_obj_set_style_border_width(battery, 1, 0);
    lv_obj_set_style_border_color(battery, lv_color_hex(0xa8b8a6), 0);
    lv_obj_set_style_radius(battery, 2, 0);
    lv_obj_remove_flag(battery, LV_OBJ_FLAG_SCROLLABLE);
    battery_text = label(199, 6, 27, 18, true);
    lv_obj_set_style_text_align(battery_text, LV_TEXT_ALIGN_CENTER, 0);
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
    visible(photo, false);
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
    lv_screen_load(screen);
    bsp_lvgl_unlock();
    bsp_display_backlight(65);
    return true;
}

bool pp_ui_image(const void *pixels, unsigned width, unsigned height) {
    if (!screen || !bsp_lvgl_lock(1000))
        return false;
    visible(photo, false);
    /* A descriptor points directly at the one owner-managed RGB565 buffer. */
    lv_image_cache_drop(&photo_descriptor);
    photo_available = pixels != NULL;
    if (pixels) {
        photo_descriptor = (lv_image_dsc_t){.header = {.magic = LV_IMAGE_HEADER_MAGIC,
                                                       .cf = LV_COLOR_FORMAT_RGB565,
                                                       .w = width,
                                                       .h = height,
                                                       .stride = width * 2},
                                            .data_size = width * height * 2,
                                            .data = pixels};
        lv_image_set_src(photo, &photo_descriptor);
        lv_obj_set_pos(photo, (240 - width) / 2, 72 + (144 - height) / 2);
    }
    bsp_lvgl_unlock();
    return true;
}

size_t pp_ui_previous(const char *value, size_t start) {
    if (!bsp_lvgl_lock(100))
        return start;
    size_t offset = pp_layout_previous(value, start, 216, body_rows(false), measure, NULL);
    bsp_lvgl_unlock();
    return offset;
}

size_t pp_ui_render(const pp_ui_frame_t *f) {
    /* Backlight PWM does not require a redraw or an LVGL allocation. */
    if (f->brightness >= 40 && f->brightness <= 100 && f->brightness != current_brightness) {
        bsp_display_backlight(f->brightness);
        current_brightness = f->brightness;
    }
    if (!screen || !bsp_lvgl_lock(100))
        return f->page;
    const lv_font_t *wanted_font = f->text_size == 18 ? &terminal_zh_18 : &terminal_zh_16;
    if (body_font != wanted_font) {
        body_font = wanted_font;
        lv_obj_set_style_text_font(content, body_font, 0);
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
    lv_obj_set_style_text_color(wifi,
                                lv_color_hex(f->wifi > -70   ? 0xb6cfa4
                                             : f->wifi > -90 ? 0x839278
                                                             : 0x526158),
                                0);
    if (f->battery < 0)
        strcpy(value, "--");
    else
        snprintf(value, sizeof(value), "%d", f->battery > 100 ? 100 : f->battery);
    text(battery_text, value);
    text(mode, f->queue ? LV_SYMBOL_LIST : LV_SYMBOL_NEW_LINE);
    text(footer, f->hint && f->hint[0] ? f->hint : "上下翻页 · OK 菜单");
    bool input = f->view == PP_UI_INPUT, idle = f->view == PP_UI_IDLE;
    visible(avatar, input || idle);
    visible(photo, f->view == PP_UI_IMAGE && photo_available);
    visible(origin, !idle);
    visible(content, !idle && f->view != PP_UI_MENU && f->view != PP_UI_IMAGE);
    visible(state, true);
    lv_obj_set_style_text_color(origin, lv_color_hex(f->task_context ? 0xa7c7e8 : 0xb6cfa4), 0);
    if (previous_view != f->view) {
        lv_obj_set_pos(content, 12, input ? 145 : 66);
        lv_obj_set_height(content, input ? 110 : 198);
        lv_obj_set_pos(origin, 12, input ? 123 : 38);
    }
    previous_view = f->view;
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
    /* Five tiny native rectangles, not FFT/particle animation or an ARGB layer. */
    filtered_level = (filtered_level * 3 + (f->level > 1024 ? 1024 : f->level)) / 4;
    for (unsigned i = 0; i < 5; i++) {
        visible(bars[i], input && f->elapsed > 0 && f->level > 0);
        unsigned weight = i == 2 ? 12 : i == 1 || i == 3 ? 9 : 5;
        unsigned height = 2 + filtered_level * weight / 1024;
        lv_obj_set_pos(bars[i], 101 + i * 8, 114 - height / 2);
        lv_obj_set_size(bars[i], 3, height);
    }
    text(origin, input                       ? "你说的"
                 : f->origin && f->origin[0] ? f->origin
                 : f->view == PP_UI_MENU     ? "操作菜单"
                 : f->view == PP_UI_IMAGE    ? "图片"
                                             : "回复");
    if (f->volume_feedback)
        snprintf(value, sizeof(value), "音量 %u", f->volume);
    else if (input && f->elapsed)
        snprintf(value, sizeof(value), "%.1f 秒", f->elapsed / 1000.0);
    else
        snprintf(value, sizeof(value), "%s", f->state ? f->state : "");
    text(state, value);
    lv_obj_set_style_text_color(state, lv_color_hex(f->volume_feedback ? 0xb6cfa4 : 0x9aaa99), 0);
    visible(volume_track, f->volume_feedback);
    if (f->volume_feedback) {
        unsigned percent = f->volume > 100 ? 100 : f->volume;
        visible(volume_fill, percent > 0);
        lv_obj_set_width(volume_fill, percent * 120 / 100);
    }
    for (unsigned i = 0; i < 7; i++) {
        unsigned first = f->selection >= 7 ? f->selection - 6 : 0;
        bool show = f->view == PP_UI_MENU && first + i < f->choice_count;
        visible(items[i], show);
        if (show) {
            text(items[i], f->choices[first + i]);
            lv_obj_set_style_bg_color(items[i], lv_color_hex(f->task_context ? 0x263c47 : 0x2a3c31),
                                      0);
            lv_obj_set_style_bg_opa(items[i],
                                    first + i == f->selection ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
        }
    }
    char page[1152];
    const char *body = f->body ? f->body : "";
    pp_text_layout_t layout = {.next = f->page};
    if (f->follow || input)
        pp_layout_tail(page, sizeof(page), body, 216, body_rows(input), measure, NULL);
    else
        layout =
            pp_layout_page(page, sizeof(page), body, f->page, 216, body_rows(false), measure, NULL);
    text(content, page);
    bsp_lvgl_unlock();
    return layout.more ? layout.next : 0;
}

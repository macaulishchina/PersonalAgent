#include "ui.h"
#include "bsp_display.h"
#include "bsp_pins.h"
#include "lvgl.h"
#include <stddef.h>
LV_FONT_DECLARE(terminal_zh_16);
static lv_obj_t *screen, *brand, *state_label, *content, *footer, *mode;
static lv_obj_t *label(int x, int y, int w, int h) {
    lv_obj_t *l = lv_label_create(screen);
    lv_obj_set_pos(l, x, y);
    lv_obj_set_size(l, w, h);
    lv_obj_set_style_text_font(l, &terminal_zh_16, 0);
    lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
    return l;
}
bool pp_ui_init(void) {
    if (bsp_display_init() != ESP_OK || !bsp_lvgl_init() || !bsp_lvgl_lock(1000))
        return false;
    screen = lv_obj_create(NULL);
    lv_obj_remove_style_all(screen);
    lv_obj_set_size(screen, BSP_LCD_W, BSP_LCD_H);
    lv_obj_set_style_bg_color(screen, lv_color_hex(0x17251f), 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(screen, lv_color_hex(0xe6ede7), 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    brand = label(24, 26, 190, 24);
    state_label = label(24, 58, 192, 22);
    content = label(24, 94, 192, 156);
    footer = label(24, 264, 192, 24);
    mode = label(24, 292, 190, 22);
    lv_obj_set_style_text_color(state_label, lv_color_hex(0x98c5ac), 0);
    lv_obj_set_style_text_color(footer, lv_color_hex(0xa4afa6), 0);
    lv_screen_load(screen);
    bsp_lvgl_unlock();
    bsp_display_backlight(65);
    return true;
}
void pp_ui_show(const char *nickname, const char *state, const char *body, const char *hint,
                bool queue) {
    if (!screen || !bsp_lvgl_lock(100))
        return;
    lv_label_set_text(brand, nickname);
    lv_label_set_text(state_label, state);
    // A page is at most 80 Unicode characters. Fold formatting whitespace for
    // the small display; otherwise many line breaks can hide part of a page.
    // The server keeps the original text and character offsets unchanged.
    char page[1024];
    size_t n = 0;
    while (body[n] && n + 1 < sizeof(page)) {
        page[n] = (body[n] == '\n' || body[n] == '\r' || body[n] == '\t') ? ' ' : body[n];
        n++;
    }
    page[n] = '\0';
    lv_label_set_text(content, page);
    lv_label_set_text(footer, hint);
    lv_label_set_text(mode, queue ? "排队 · 双击切换" : "引导 · 双击切换");
    bsp_lvgl_unlock();
}

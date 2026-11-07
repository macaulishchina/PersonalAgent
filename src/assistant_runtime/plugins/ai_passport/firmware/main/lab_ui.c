#include "lab_ui.h"
#include "lab_language.h"
#include "bsp_display.h"
#include "bsp_pins.h"
#include "lvgl.h"
#include <string.h>

#ifdef PPDEMO_ZH
LV_FONT_DECLARE(ppdemo_zh_14);
LV_FONT_DECLARE(ppdemo_zh_16);
LV_FONT_DECLARE(ppdemo_zh_20);
#define FONT_SMALL (&ppdemo_zh_14)
#define FONT_BODY (&ppdemo_zh_16)
#define FONT_TITLE (&ppdemo_zh_20)
#define FONT_REPORT (&ppdemo_zh_14)
#else
#define FONT_SMALL (&lv_font_montserrat_14)
#define FONT_BODY (&lv_font_montserrat_14)
#define FONT_TITLE (&lv_font_montserrat_20)
#define FONT_REPORT (&lv_font_montserrat_12)
#endif
static lv_obj_t *screen, *heading, *tag, *content, *hint, *bars[6];
static lv_obj_t *label(int x, int y, int width, const lv_font_t *font)
{
    lv_obj_t *o = lv_label_create(screen);
    lv_obj_set_pos(o, x, y); lv_obj_set_width(o, width);
    lv_obj_set_style_text_font(o, font, 0);
    lv_label_set_long_mode(o, LV_LABEL_LONG_WRAP);
    return o;
}
bool lab_ui_init(void)
{
    if (!bsp_lvgl_lock(1000)) return false;
    screen = lv_obj_create(NULL);
    lv_obj_remove_style_all(screen);
    lv_obj_set_size(screen, BSP_LCD_W, BSP_LCD_H);
    lv_obj_set_style_pad_all(screen, 0, 0);
    lv_obj_set_style_bg_opa(screen, LV_OPA_COVER, 0);
    lv_obj_remove_flag(screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_t *brand = label(24, 25, 190, FONT_SMALL);
    lv_label_set_text(brand, LAB_TEXT("PP  /  DEMO LAB", "PP  /  中文体验"));
    lv_obj_set_style_text_color(brand, lv_color_hex(0x738b9d), 0);
    heading = label(22, 58, 198, FONT_TITLE);
    tag = label(24, 87, 190, FONT_SMALL);
    content = label(24, 117, 194, FONT_BODY);
    lv_obj_set_height(content, 146);
    lv_obj_set_style_text_line_space(content, 0, 0);
    lv_label_set_long_mode(content, LV_LABEL_LONG_SCROLL);
    hint = label(24, 274, 194, FONT_SMALL);
    lv_obj_set_style_text_color(hint, lv_color_hex(0x738b9d), 0);
    for (int i = 0; i < 6; i++) {
        bars[i] = lv_obj_create(screen);
        lv_obj_remove_style_all(bars[i]); lv_obj_set_pos(bars[i], 24+i*32, 132);
        lv_obj_set_size(bars[i], 32, 112); lv_obj_set_style_bg_opa(bars[i], LV_OPA_COVER, 0);
        lv_obj_add_flag(bars[i], LV_OBJ_FLAG_HIDDEN);
    }
    lv_screen_load(screen); bsp_lvgl_unlock(); return true;
}
void lab_ui_show(const char *title, const char *meta, const char *body, const char *footer, int pattern)
{
    if (!screen || !bsp_lvgl_lock(500)) return;
    static const uint32_t colors[] = {0x101e2c,0xe63845,0x24bb69,0x2864e6,0xffffff,0x000000,0x808080};
    uint32_t background = pattern >= 0 && pattern < 7 ? colors[pattern] : colors[0];
    uint32_t ink = pattern == 4 ? 0x172b3a : 0xe5edf5;
    lv_obj_set_style_bg_color(screen, lv_color_hex(background), 0);
    lv_obj_set_style_text_color(heading, lv_color_hex(ink), 0);
    lv_obj_set_style_text_color(content, lv_color_hex(ink), 0);
    lv_obj_set_style_text_color(tag, lv_color_hex(pattern == 4 ? 0x17765f : 0x6ce4b9), 0);
    lv_label_set_text(heading, title); lv_label_set_text(tag, meta);
    lv_obj_set_style_text_font(content, !strcmp(title, LAB_TEXT("Manual report", "测试报告")) ? FONT_REPORT : FONT_BODY, 0);
    lv_label_set_text(content, body); lv_label_set_text(hint, footer);
    for (int i = 0; i < 6; i++) {
        if (pattern >= 7) {
            lv_obj_remove_flag(bars[i], LV_OBJ_FLAG_HIDDEN);
            uint32_t c = pattern == 7 ? colors[1+i%6] : (uint32_t)(i*51)*0x010101;
            lv_obj_set_style_bg_color(bars[i], lv_color_hex(c), 0);
        } else lv_obj_add_flag(bars[i], LV_OBJ_FLAG_HIDDEN);
    }
    bsp_lvgl_unlock();
}

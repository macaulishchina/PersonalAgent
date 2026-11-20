/*******************************************************************************
 * Size: 12 px
 * Bpp: 2
 * Opts: --font ../../managed_components/lvgl__lvgl/scripts/built_in_font/SourceHanSansSC-Normal.otf --symbols 0123456789- --size 12 --bpp 2 --no-compress --format lvgl --lv-include lvgl.h --lv-font-name terminal_battery_12 -o main/fonts/terminal_battery_12.c
 ******************************************************************************/

#ifdef LV_LVGL_H_INCLUDE_SIMPLE
#include "lvgl.h"
#else
#include "lvgl.h"
#endif

#ifndef TERMINAL_BATTERY_12
#define TERMINAL_BATTERY_12 1
#endif

#if TERMINAL_BATTERY_12

/*-----------------
 *    BITMAPS
 *----------------*/

/*Store the image of the glyphs*/
static LV_ATTRIBUTE_LARGE_CONST const uint8_t glyph_bitmap[] = {
    /* U+002D "-" */
    0x7d,

    /* U+0030 "0" */
    0xf, 0x83, 0x9, 0x30, 0x36, 0x3, 0x60, 0x36,
    0x3, 0x30, 0x23, 0x9, 0xf, 0x80,

    /* U+0031 "1" */
    0x2c, 0x7, 0x0, 0xc0, 0x30, 0xc, 0x3, 0x0,
    0xc0, 0x30, 0xff, 0x80,

    /* U+0032 "2" */
    0x1f, 0x81, 0xd, 0x0, 0x60, 0x9, 0x0, 0xc0,
    0x24, 0xa, 0x2, 0x80, 0x7f, 0xf0,

    /* U+0033 "3" */
    0x1f, 0x81, 0x9, 0x0, 0x60, 0xc, 0xb, 0x40,
    0xa, 0x0, 0x35, 0xa, 0x2f, 0x80,

    /* U+0034 "4" */
    0x1, 0xc0, 0xf, 0x0, 0x9c, 0x3, 0x30, 0x20,
    0xc1, 0x83, 0xb, 0xff, 0x0, 0x30, 0x0, 0xc0,

    /* U+0035 "5" */
    0x2f, 0xd3, 0x0, 0x30, 0x3, 0xb8, 0x10, 0xa0,
    0x3, 0x0, 0x35, 0xa, 0x2f, 0x80,

    /* U+0036 "6" */
    0xb, 0xd0, 0xa0, 0x3, 0x0, 0x9, 0xe0, 0x74,
    0x60, 0x80, 0xc3, 0x3, 0x9, 0x18, 0xb, 0x80,

    /* U+0037 "7" */
    0x7f, 0xf0, 0x1, 0x40, 0xc, 0x0, 0x90, 0x3,
    0x0, 0xc, 0x0, 0x60, 0x1, 0x80, 0x9, 0x0,

    /* U+0038 "8" */
    0xf, 0x80, 0xc2, 0x83, 0x2, 0x5, 0x14, 0xa,
    0x80, 0xc2, 0x85, 0x3, 0xc, 0x1c, 0x1f, 0x80,

    /* U+0039 "9" */
    0x1f, 0x43, 0x9, 0x60, 0x23, 0x7, 0x1e, 0x70,
    0x3, 0x0, 0x61, 0xc, 0x2f, 0x40
};


/*---------------------
 *  GLYPH DESCRIPTION
 *--------------------*/

static const lv_font_fmt_txt_glyph_dsc_t glyph_dsc[] = {
    {.bitmap_index = 0, .adv_w = 0, .box_w = 0, .box_h = 0, .ofs_x = 0, .ofs_y = 0} /* id = 0 reserved */,
    {.bitmap_index = 0, .adv_w = 66, .box_w = 4, .box_h = 1, .ofs_x = 0, .ofs_y = 3},
    {.bitmap_index = 1, .adv_w = 105, .box_w = 6, .box_h = 9, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 15, .adv_w = 105, .box_w = 5, .box_h = 9, .ofs_x = 1, .ofs_y = 0},
    {.bitmap_index = 27, .adv_w = 105, .box_w = 6, .box_h = 9, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 41, .adv_w = 105, .box_w = 6, .box_h = 9, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 55, .adv_w = 105, .box_w = 7, .box_h = 9, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 71, .adv_w = 105, .box_w = 6, .box_h = 9, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 85, .adv_w = 105, .box_w = 7, .box_h = 9, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 101, .adv_w = 105, .box_w = 7, .box_h = 9, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 117, .adv_w = 105, .box_w = 7, .box_h = 9, .ofs_x = 0, .ofs_y = 0},
    {.bitmap_index = 133, .adv_w = 105, .box_w = 6, .box_h = 9, .ofs_x = 0, .ofs_y = 0}
};

/*---------------------
 *  CHARACTER MAPPING
 *--------------------*/

static const uint8_t glyph_id_ofs_list_0[] = {
    0, 0, 0, 1, 2, 3, 4, 5,
    6, 7, 8, 9, 10
};

/*Collect the unicode lists and glyph_id offsets*/
static const lv_font_fmt_txt_cmap_t cmaps[] =
{
    {
        .range_start = 45, .range_length = 13, .glyph_id_start = 1,
        .unicode_list = NULL, .glyph_id_ofs_list = glyph_id_ofs_list_0, .list_length = 13, .type = LV_FONT_FMT_TXT_CMAP_FORMAT0_FULL
    }
};



/*--------------------
 *  ALL CUSTOM DATA
 *--------------------*/

#if LVGL_VERSION_MAJOR == 8
/*Store all the custom data of the font*/
static  lv_font_fmt_txt_glyph_cache_t cache;
#endif

#if LVGL_VERSION_MAJOR >= 8
static const lv_font_fmt_txt_dsc_t font_dsc = {
#else
static lv_font_fmt_txt_dsc_t font_dsc = {
#endif
    .glyph_bitmap = glyph_bitmap,
    .glyph_dsc = glyph_dsc,
    .cmaps = cmaps,
    .kern_dsc = NULL,
    .kern_scale = 0,
    .cmap_num = 1,
    .bpp = 2,
    .kern_classes = 0,
    .bitmap_format = 0,
#if LVGL_VERSION_MAJOR == 8
    .cache = &cache
#endif
};



/*-----------------
 *  PUBLIC FONT
 *----------------*/

/*Initialize a public general font descriptor*/
#if LVGL_VERSION_MAJOR >= 8
const lv_font_t terminal_battery_12 = {
#else
lv_font_t terminal_battery_12 = {
#endif
    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,    /*Function pointer to get glyph's data*/
    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,    /*Function pointer to get glyph's bitmap*/
    .line_height = 9,          /*The maximum line height required by the font*/
    .base_line = 0,             /*Baseline measured from the bottom of the line*/
#if !(LVGL_VERSION_MAJOR == 6 && LVGL_VERSION_MINOR == 0)
    .subpx = LV_FONT_SUBPX_NONE,
#endif
#if LV_VERSION_CHECK(7, 4, 0) || LVGL_VERSION_MAJOR >= 8
    .underline_position = -1,
    .underline_thickness = 1,
#endif
    .dsc = &font_dsc,          /*The custom font data. Will be accessed by `get_glyph_bitmap/dsc` */
#if LV_VERSION_CHECK(8, 2, 0) || LVGL_VERSION_MAJOR >= 9
    .fallback = NULL,
#endif
    .user_data = NULL,
};



#endif /*#if TERMINAL_BATTERY_12*/

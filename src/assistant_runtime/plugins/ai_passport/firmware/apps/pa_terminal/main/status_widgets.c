#include "status_widgets.h"
#include "src/misc/cache/instance/lv_image_cache.h"
#include <stdio.h>
#include <string.h>

enum { SPEED_W = 32, SPEED_H = 13, SPEED_STRIDE = SPEED_W / 8 };
static lv_obj_t *memory_icon, *speed_icon;
static pp_meter_t shown;
static bool first = true;
/* Two ARGB8888 palette entries followed by the 1-bit glyph raster (52 bytes). */
static struct {
    uint32_t palette[2];
    uint8_t pixels[SPEED_H][SPEED_STRIDE];
} speed = {.palette = {0, 0xffcad9c7}};
static const lv_image_dsc_t speed_image = {
    .header = {.magic = LV_IMAGE_HEADER_MAGIC, .cf = LV_COLOR_FORMAT_I1,
               .w = SPEED_W, .h = SPEED_H, .stride = SPEED_STRIDE},
    .data = (const uint8_t *)&speed, .data_size = sizeof(speed)
};

static void draw_meter(lv_layer_t *layer, lv_point_t center, unsigned radius, unsigned width,
                       uint32_t track, uint32_t fill, unsigned percent) {
    lv_draw_arc_dsc_t arc;
    lv_draw_arc_dsc_init(&arc);
    arc.center = center;
    arc.radius = radius;
    arc.width = width;
    arc.color = lv_color_hex(track);
    arc.start_angle = 0;
    arc.end_angle = 360;
    lv_draw_arc(layer, &arc);
    if (percent) {
        arc.color = lv_color_hex(fill);
        if (percent < 100) {
            arc.start_angle = 270;
            arc.end_angle = (270 + (percent * 360 + 50) / 100) % 360;
        }
        lv_draw_arc(layer, &arc);
    }
}

static void draw_memory(lv_event_t *event) {
    lv_area_t area;
    lv_obj_get_coords(lv_event_get_current_target(event), &area);
    lv_point_t center = {area.x1 + 8, area.y1 + 8};
    lv_layer_t *layer = lv_event_get_layer(event);
    draw_meter(layer, center, 8, 3, shown.flash_valid ? 0x456952 : 0x34443c,
               0x8bc79a, shown.flash);
    draw_meter(layer, center, 5, 5, 0x889382, 0xeeeede, shown.sram);
}

static void pixel(unsigned x, unsigned y) {
    speed.pixels[y][x / 8] |= 0x80u >> (x % 8);
}

/* Five rows of three pixels, kept in flash. */
static const uint8_t digits[][5] = {
    {7,5,5,5,7}, {2,6,2,2,7}, {7,1,7,4,7}, {7,1,7,1,7}, {5,5,7,1,1},
    {7,4,7,1,7}, {7,4,7,5,7}, {7,1,2,2,2}, {7,5,7,5,7}, {7,5,7,1,7}
};
static const uint8_t unit_k[] = {4,5,6,6,5}, unit_m[] = {5,7,7,5,5}, dash[] = {0,0,7,0,0};

static void draw_rate(unsigned y, uint16_t rate, bool valid) {
    char value[8] = "-";
    if (valid) {
        if (rate < 10000)
            snprintf(value, sizeof(value), "%uk", rate);
        else
            snprintf(value, sizeof(value), "%uM", (rate + 512) / 1024);
    }
    unsigned x = 21 - (strlen(value) * 4 - 1);
    for (const char *c = value; *c; ++c, x += 4) {
        const uint8_t *glyph = *c == '-' ? dash : *c == 'k' ? unit_k :
                               *c == 'M' ? unit_m : digits[*c - '0'];
        for (unsigned row = 0; row < 5; row++)
            for (unsigned col = 0; col < 3; col++)
                if (glyph[row] & (4u >> col))
                    pixel(x + col, y + row);
    }
}

void pp_status_widgets_create(lv_obj_t *screen) {
    memory_icon = lv_obj_create(screen);
    lv_obj_remove_style_all(memory_icon);
    lv_obj_set_pos(memory_icon, 121, 8);
    lv_obj_set_size(memory_icon, 16, 16);
    lv_obj_remove_flag(memory_icon, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_event_cb(memory_icon, draw_memory, LV_EVENT_DRAW_MAIN, NULL);
    speed_icon = lv_image_create(screen);
    lv_obj_set_pos(speed_icon, 159, 9);
    lv_image_set_src(speed_icon, &speed_image);
}

void pp_status_widgets_update(const pp_meter_t *meter) {
    if (first || meter->flash != shown.flash || meter->sram != shown.sram ||
        meter->flash_valid != shown.flash_valid)
        lv_obj_invalidate(memory_icon);
    if (first || meter->rates_valid != shown.rates_valid || meter->up_k != shown.up_k ||
        meter->down_k != shown.down_k) {
        lv_image_cache_drop(&speed_image);
        memset(speed.pixels, 0, sizeof(speed.pixels));
        draw_rate(0, meter->up_k, meter->rates_valid);
        draw_rate(7, meter->down_k, meter->rates_valid);
        for (unsigned y = 1; y <= 11; y++) {
            pixel(25, y);
            pixel(29, y);
        }
        for (unsigned d = 1; d <= 2; d++) {
            pixel(25 - d, 1 + d);
            pixel(25 + d, 1 + d);
            pixel(29 - d, 11 - d);
            pixel(29 + d, 11 - d);
        }
        lv_obj_invalidate(speed_icon);
    }
    shown = *meter;
    first = false;
}

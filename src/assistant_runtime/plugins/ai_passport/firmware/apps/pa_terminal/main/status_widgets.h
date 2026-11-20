#pragma once
#include "lvgl.h"
#include "status_meter.h"

void pp_status_widgets_create(lv_obj_t *screen);
void pp_status_widgets_update(const pp_meter_t *meter);

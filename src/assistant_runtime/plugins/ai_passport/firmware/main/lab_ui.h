#pragma once
#include <stdbool.h>
bool lab_ui_init(void);
void lab_ui_show(const char *title, const char *meta, const char *body,
                 const char *footer, int pattern);

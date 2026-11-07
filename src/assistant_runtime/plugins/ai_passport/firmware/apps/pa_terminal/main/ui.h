#pragma once
#include <stdbool.h>
bool pp_ui_init(void);
void pp_ui_show(const char *nickname, const char *state, const char *body, const char *hint,
                bool queue);

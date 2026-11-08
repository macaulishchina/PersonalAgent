#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum {
    PP_SCREEN_HOME,
    PP_SCREEN_CHAT,
    PP_SCREEN_HISTORY,
    PP_SCREEN_TASKS,
    PP_SCREEN_TASK,
    PP_SCREEN_MENU,
    PP_SCREEN_SETTINGS,
    PP_SCREEN_MAINTENANCE,
    PP_SCREEN_VOLUME,
    PP_SCREEN_NETWORK,
    PP_SCREEN_QUESTION
} pp_screen_t;
typedef enum {
    PP_SELECT_NONE,
    PP_SELECT_TASKS,
    PP_SELECT_VOLUME,
    PP_SELECT_CANCEL,
    PP_SELECT_SETTINGS,
    PP_SELECT_HOME,
    PP_SELECT_KEEP_ONLINE,
    PP_SELECT_NETWORK,
    PP_SELECT_SLEEP,
    PP_SELECT_MAINTENANCE,
    PP_SELECT_BACK,
    PP_SELECT_RELOAD,
    PP_SELECT_RETRY,
    PP_SELECT_REPROVISION,
    PP_SELECT_FACTORY
} pp_selection_t;
typedef struct {
    pp_screen_t screen;
    unsigned selection;
    bool up_down, up_consumed;
    uint32_t up_started;
} pp_navigation_t;
/* Navigation owns presentation only. Returning home never cancels accepted work. */
void pp_navigation_open(pp_navigation_t *nav, pp_screen_t screen);
void pp_navigation_back(pp_navigation_t *nav);
bool pp_navigation_menu(const pp_navigation_t *nav);
bool pp_navigation_audio(const pp_navigation_t *nav);
unsigned pp_navigation_count(const pp_navigation_t *nav);
const char *pp_navigation_label(const pp_navigation_t *nav, unsigned index);
const char *pp_navigation_title(const pp_navigation_t *nav);
pp_selection_t pp_navigation_selected(const pp_navigation_t *nav);
void pp_navigation_move(pp_navigation_t *nav, int direction);
void pp_navigation_up_press(pp_navigation_t *nav, uint32_t now);
bool pp_navigation_up_tick(pp_navigation_t *nav, uint32_t now);
/* true: a short up action. A consumed hold never also emits a short action. */
bool pp_navigation_up_release(pp_navigation_t *nav);

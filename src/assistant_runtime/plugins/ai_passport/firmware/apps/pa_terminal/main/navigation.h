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
    PP_SCREEN_SOUND,
    PP_SCREEN_CONNECTIVITY,
    PP_SCREEN_POWER,
    PP_SCREEN_MAINTENANCE,
    PP_SCREEN_VOLUME,
    PP_SCREEN_BRIGHTNESS,
    PP_SCREEN_NETWORK,
    PP_SCREEN_QUESTION,
    PP_SCREEN_COUNT
} pp_screen_t;
typedef enum {
    PP_SELECT_NONE,
    PP_SELECT_TASKS,
    PP_SELECT_VOLUME,
    PP_SELECT_BRIGHTNESS,
    PP_SELECT_CANCEL,
    PP_SELECT_SOUND,
    PP_SELECT_CONNECTIVITY,
    PP_SELECT_POWER,
    PP_SELECT_HOME,
    PP_SELECT_KEEP_ONLINE,
    PP_SELECT_STANDBY_WIFI,
    PP_SELECT_NETWORK,
    PP_SELECT_SLEEP,
    PP_SELECT_MAINTENANCE,
    PP_SELECT_BACK,
    PP_SELECT_RELOAD,
    PP_SELECT_RETRY,
    PP_SELECT_REPROVISION,
    PP_SELECT_REPAIR,
    PP_SELECT_FACTORY
} pp_selection_t;
/* Navigation owns presentation only. Returning home never cancels accepted work. */
typedef enum {
    PP_ROUTE_CONFIRM,
    PP_ROUTE_LOCAL,
    PP_ROUTE_SETUP,
    PP_ROUTE_REVIEW,
    PP_ROUTE_SESSION
} pp_route_t;
typedef enum { PP_ARROW_NONE, PP_ARROW_UP, PP_ARROW_DOWN } pp_arrow_t;
typedef struct {
    pp_screen_t screen;
    bool can_cancel;
    unsigned selection;
    uint8_t focus[PP_SCREEN_COUNT];
    uint32_t page_epoch;
    pp_arrow_t arrow;
    bool arrow_consumed, arrow_can_jump, arrow_can_sleep;
    uint32_t arrow_started, arrow_epoch;
    bool ok_held, ok_wifi_retry, ok_consumed, transition_click;
    pp_route_t ok_route;
    uint32_t ok_epoch, ok_started, transition_at;
} pp_navigation_t;

pp_route_t pp_navigation_route(const pp_navigation_t *nav, bool confirmation, bool setup,
                               bool review);
void pp_navigation_open(pp_navigation_t *nav, pp_screen_t screen);
bool pp_navigation_local(const pp_navigation_t *nav);
void pp_navigation_back(pp_navigation_t *nav);
bool pp_navigation_menu(const pp_navigation_t *nav);
bool pp_navigation_audio(const pp_navigation_t *nav);
unsigned pp_navigation_count(const pp_navigation_t *nav);
const char *pp_navigation_label(const pp_navigation_t *nav, unsigned index);
const char *pp_navigation_title(const pp_navigation_t *nav);
const char *pp_navigation_detail(const pp_navigation_t *nav);
pp_selection_t pp_navigation_selected(const pp_navigation_t *nav);
void pp_navigation_move(pp_navigation_t *nav, int direction);
void pp_navigation_set_execution(pp_navigation_t *nav, bool can_cancel);
/* A press belongs to its original page and route. Double-click tails cannot
 * activate a control on the destination page. */
bool pp_navigation_ok_press(pp_navigation_t *nav, pp_route_t route, uint32_t now,
                            bool wifi_retry);
bool pp_navigation_ok_wifi_tick(pp_navigation_t *nav, pp_route_t route, uint32_t now);
bool pp_navigation_ok_release(pp_navigation_t *nav, pp_route_t route);
void pp_navigation_ok_transition(pp_navigation_t *nav, uint32_t now);
void pp_navigation_arrow_press(pp_navigation_t *nav, pp_arrow_t key, uint32_t now,
                               bool can_jump, bool can_sleep);
pp_arrow_t pp_navigation_arrow_tick(pp_navigation_t *nav, uint32_t now, bool can_jump);
bool pp_navigation_standby_ready(const pp_navigation_t *nav, uint32_t now);
/* A consumed hold never also emits a short action. */
bool pp_navigation_arrow_release(pp_navigation_t *nav, pp_arrow_t key);

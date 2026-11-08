#include "navigation.h"

typedef struct {
    const char *label;
    pp_selection_t action;
} option_t;
static const option_t root[] = {{"任务消息", PP_SELECT_TASKS},
                                {"音量", PP_SELECT_VOLUME},
                                {"取消当前执行", PP_SELECT_CANCEL},
                                {"设置", PP_SELECT_SETTINGS},
                                {"返回主页", PP_SELECT_HOME}};
static const option_t settings[] = {{"保持在线", PP_SELECT_KEEP_ONLINE},
                                    {"网络状态", PP_SELECT_NETWORK},
                                    {"休眠", PP_SELECT_SLEEP},
                                    {"设备维护", PP_SELECT_MAINTENANCE},
                                    {"返回上级", PP_SELECT_BACK}};
static const option_t maintenance[] = {{"重载配置", PP_SELECT_RELOAD},
                                       {"重试连接", PP_SELECT_RETRY},
                                       {"重新配网", PP_SELECT_REPROVISION},
                                       {"恢复出厂", PP_SELECT_FACTORY},
                                       {"返回上级", PP_SELECT_BACK}};
static const option_t *options(const pp_navigation_t *n) {
    return n->screen == PP_SCREEN_MENU          ? root
           : n->screen == PP_SCREEN_SETTINGS    ? settings
           : n->screen == PP_SCREEN_MAINTENANCE ? maintenance
                                                : NULL;
}
void pp_navigation_open(pp_navigation_t *n, pp_screen_t screen) {
    n->screen = screen;
    n->selection = 0;
}
void pp_navigation_back(pp_navigation_t *n) {
    pp_screen_t next = n->screen == PP_SCREEN_TASK ? PP_SCREEN_TASKS
                       : n->screen == PP_SCREEN_MAINTENANCE || n->screen == PP_SCREEN_NETWORK
                           ? PP_SCREEN_SETTINGS
                       : n->screen == PP_SCREEN_SETTINGS || n->screen == PP_SCREEN_VOLUME ||
                               n->screen == PP_SCREEN_TASKS
                           ? PP_SCREEN_MENU
                           : PP_SCREEN_HOME;
    pp_navigation_open(n, next);
}
bool pp_navigation_menu(const pp_navigation_t *n) { return options(n) != NULL; }
bool pp_navigation_audio(const pp_navigation_t *n) {
    return n->screen == PP_SCREEN_HOME || n->screen == PP_SCREEN_CHAT ||
           n->screen == PP_SCREEN_HISTORY || n->screen == PP_SCREEN_QUESTION;
}
unsigned pp_navigation_count(const pp_navigation_t *n) { return options(n) ? 5 : 0; }
const char *pp_navigation_label(const pp_navigation_t *n, unsigned i) {
    return i < pp_navigation_count(n) ? options(n)[i].label : "";
}
const char *pp_navigation_title(const pp_navigation_t *n) {
    return n->screen == PP_SCREEN_MENU          ? "操作菜单"
           : n->screen == PP_SCREEN_SETTINGS    ? "设置"
           : n->screen == PP_SCREEN_MAINTENANCE ? "设备维护"
           : n->screen == PP_SCREEN_TASKS       ? "任务消息"
           : n->screen == PP_SCREEN_TASK        ? "任务报告"
           : n->screen == PP_SCREEN_VOLUME      ? "音量"
                                                : "网络状态";
}
pp_selection_t pp_navigation_selected(const pp_navigation_t *n) {
    return n->selection < pp_navigation_count(n) ? options(n)[n->selection].action : PP_SELECT_NONE;
}
void pp_navigation_move(pp_navigation_t *n, int direction) {
    unsigned count = pp_navigation_count(n);
    if (count)
        n->selection = (n->selection + (direction < 0 ? count - 1 : 1)) % count;
}
void pp_navigation_up_press(pp_navigation_t *n, uint32_t now) {
    if (!n->up_down) {
        n->up_down = true;
        n->up_consumed = false;
        n->up_started = now;
    }
}
bool pp_navigation_up_tick(pp_navigation_t *n, uint32_t now) {
    if (!n->up_down || n->up_consumed || now - n->up_started < 650)
        return false;
    n->up_consumed = true;
    return true;
}
bool pp_navigation_up_release(pp_navigation_t *n) {
    bool short_press = n->up_down && !n->up_consumed;
    n->up_down = false;
    n->up_consumed = false;
    return short_press;
}

#include "navigation.h"
#include "terminal_limits.h"

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
                                    {"熄屏保持 Wi-Fi", PP_SELECT_STANDBY_WIFI},
                                    {"返回上级", PP_SELECT_BACK}};
static const option_t maintenance[] = {
    {"同步 PA 设置", PP_SELECT_RELOAD}, {"连接已存网络", PP_SELECT_RETRY},
    {"设置 Wi-Fi", PP_SELECT_REPROVISION}, {"配对与重置 >", PP_SELECT_PAIRING_RESET},
    {"返回上级", PP_SELECT_BACK}};
static const option_t pairing_reset[] = {
    {"更换 PA 配对", PP_SELECT_REPAIR}, {"恢复出厂设置", PP_SELECT_FACTORY},
    {"返回设备维护", PP_SELECT_BACK}};
/* Screen metadata defines return paths and which pages own local input even
 * while PA is disconnected or sending a new handshake. */
typedef struct {
    const char *title;
    pp_screen_t parent;
    const option_t *options;
    unsigned count;
    bool local;
} page_t;
#define MENU(items) items, sizeof(items) / sizeof(items[0]), true
static const page_t pages[PP_SCREEN_COUNT] = {
    [PP_SCREEN_HOME] = {"主页", PP_SCREEN_HOME},
    [PP_SCREEN_CHAT] = {"对话", PP_SCREEN_HOME},
    [PP_SCREEN_HISTORY] = {"历史记录", PP_SCREEN_HOME},
    [PP_SCREEN_TASKS] = {"任务消息", PP_SCREEN_MENU},
    [PP_SCREEN_TASK] = {"任务报告", PP_SCREEN_TASKS},
    [PP_SCREEN_MENU] = {"操作菜单", PP_SCREEN_HOME, MENU(root)},
    [PP_SCREEN_SETTINGS] = {"设置", PP_SCREEN_MENU, MENU(settings)},
    [PP_SCREEN_MAINTENANCE] = {"设备维护", PP_SCREEN_SETTINGS, MENU(maintenance)},
    [PP_SCREEN_PAIRING_RESET] = {"配对与重置", PP_SCREEN_MAINTENANCE, MENU(pairing_reset)},
    [PP_SCREEN_VOLUME] = {"音量", PP_SCREEN_MENU, NULL, 0, true},
    [PP_SCREEN_NETWORK] = {"网络状态", PP_SCREEN_SETTINGS, NULL, 0, true},
    [PP_SCREEN_QUESTION] = {"等待回答", PP_SCREEN_HOME},
};
static void open_page(pp_navigation_t *n, pp_screen_t screen, bool restore_focus) {
    if (screen == PP_SCREEN_SETTINGS &&
        (n->screen == PP_SCREEN_HOME || n->screen == PP_SCREEN_MENU))
        n->settings_parent = n->screen;
    n->focus[n->screen] = n->selection;
    if (n->screen != screen)
        n->page_epoch++;
    n->screen = screen;
    n->selection = restore_focus ? n->focus[screen] : 0;
}
void pp_navigation_open(pp_navigation_t *n, pp_screen_t screen) {
    open_page(n, screen, false);
}
void pp_navigation_back(pp_navigation_t *n) {
    open_page(n, n->screen == PP_SCREEN_SETTINGS ? n->settings_parent : pages[n->screen].parent,
              true);
}
bool pp_navigation_local(const pp_navigation_t *n) { return pages[n->screen].local; }
pp_route_t pp_navigation_route(const pp_navigation_t *n, bool confirmation, bool setup,
                               bool review) {
    if (confirmation)
        return PP_ROUTE_CONFIRM;
    if (pp_navigation_local(n))
        return PP_ROUTE_LOCAL;
    if (n->screen == PP_SCREEN_HOME && setup)
        return PP_ROUTE_SETUP;
    return review ? PP_ROUTE_REVIEW : PP_ROUTE_SESSION;
}
bool pp_navigation_menu(const pp_navigation_t *n) { return pages[n->screen].options != NULL; }
bool pp_navigation_audio(const pp_navigation_t *n) {
    return n->screen == PP_SCREEN_HOME || n->screen == PP_SCREEN_CHAT ||
           n->screen == PP_SCREEN_HISTORY || n->screen == PP_SCREEN_QUESTION;
}
unsigned pp_navigation_count(const pp_navigation_t *n) { return pages[n->screen].count; }
const char *pp_navigation_label(const pp_navigation_t *n, unsigned i) {
    return i < pp_navigation_count(n) ? pages[n->screen].options[i].label : "";
}
const char *pp_navigation_title(const pp_navigation_t *n) { return pages[n->screen].title; }
const char *pp_navigation_detail(const pp_navigation_t *n) {
    switch (pp_navigation_selected(n)) {
    case PP_SELECT_RELOAD: return "从 PA 获取设备设置";
    case PP_SELECT_RETRY: return "用已存 Wi-Fi 连接 PA";
    case PP_SELECT_REPROVISION: return "开启热点，手机填写网络";
    case PP_SELECT_PAIRING_RESET: return "配对管理和出厂重置";
    case PP_SELECT_REPAIR: return "保留 Wi-Fi，绑定新会话";
    case PP_SELECT_FACTORY: return "清除本机网络与配对";
    default: return NULL;
    }
}
pp_selection_t pp_navigation_selected(const pp_navigation_t *n) {
    return n->selection < pp_navigation_count(n) ? pages[n->screen].options[n->selection].action
                                                 : PP_SELECT_NONE;
}
void pp_navigation_move(pp_navigation_t *n, int direction) {
    unsigned count = pp_navigation_count(n);
    if (count)
        n->selection = (n->selection + (direction < 0 ? count - 1 : 1)) % count;
}
bool pp_navigation_ok_press(pp_navigation_t *n, pp_route_t route, uint32_t now,
                            bool wifi_retry) {
    if (n->ok_held || (n->transition_click && now - n->transition_at <= PP_OK_DOUBLE_WINDOW_MS))
        return false;
    n->transition_click = false;
    n->ok_held = true;
    n->ok_wifi_retry = wifi_retry;
    n->ok_consumed = false;
    n->ok_route = route;
    n->ok_epoch = n->page_epoch;
    n->ok_started = now;
    return true;
}
static bool owns_ok(const pp_navigation_t *n, pp_route_t route) {
    return n->ok_held && n->ok_epoch == n->page_epoch && n->ok_route == route;
}
bool pp_navigation_ok_wifi_tick(pp_navigation_t *n, pp_route_t route, uint32_t now) {
    if (!n->ok_wifi_retry || n->ok_consumed || !owns_ok(n, route) ||
        now - n->ok_started < PP_OK_HOLD_MS)
        return false;
    n->ok_consumed = true;
    return true;
}
bool pp_navigation_ok_release(pp_navigation_t *n, pp_route_t route) {
    bool owned = !n->ok_consumed && owns_ok(n, route);
    n->ok_held = false;
    return owned;
}
void pp_navigation_ok_transition(pp_navigation_t *n, uint32_t now) {
    n->transition_click = true;
    n->transition_at = now;
}
void pp_navigation_arrow_press(pp_navigation_t *n, pp_arrow_t key, uint32_t now,
                               bool can_jump) {
    if (n->arrow != PP_ARROW_NONE)
        return;
    n->arrow = key;
    n->arrow_consumed = false;
    n->arrow_can_jump = key == PP_ARROW_DOWN && can_jump;
    n->arrow_started = now;
    n->arrow_epoch = n->page_epoch;
}
pp_arrow_t pp_navigation_arrow_tick(pp_navigation_t *n, uint32_t now, bool can_jump) {
    if (n->arrow == PP_ARROW_NONE || n->arrow_consumed || now - n->arrow_started < 650)
        return PP_ARROW_NONE;
    if (n->arrow == PP_ARROW_DOWN && !n->arrow_can_jump)
        return PP_ARROW_NONE;
    n->arrow_consumed = true;
    return n->arrow == PP_ARROW_UP || (can_jump && n->arrow_epoch == n->page_epoch)
               ? n->arrow : PP_ARROW_NONE;
}
bool pp_navigation_arrow_release(pp_navigation_t *n, pp_arrow_t key) {
    if (n->arrow != key)
        return false;
    bool short_press = !n->arrow_consumed && (!n->arrow_can_jump || n->arrow_epoch == n->page_epoch);
    n->arrow = PP_ARROW_NONE;
    return short_press;
}

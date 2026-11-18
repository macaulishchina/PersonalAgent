#include "navigation.h"
#include <assert.h>
#include <string.h>
int main(void) {
    pp_navigation_t nav = {0};
    assert(nav.screen == PP_SCREEN_HOME && !pp_navigation_menu(&nav));
    assert(pp_navigation_audio(&nav));
    pp_navigation_open(&nav, PP_SCREEN_MENU);
    assert(pp_navigation_count(&nav) == 5);
    assert(!pp_navigation_audio(&nav));
    assert(pp_navigation_selected(&nav) == PP_SELECT_TASKS);
    pp_navigation_move(&nav, -1);
    assert(pp_navigation_selected(&nav) == PP_SELECT_HOME);
    pp_navigation_move(&nav, 1);
    assert(pp_navigation_selected(&nav) == PP_SELECT_TASKS);
    pp_navigation_open(&nav, PP_SCREEN_SETTINGS);
    assert(pp_navigation_count(&nav) == 5);
    assert(pp_navigation_selected(&nav) == PP_SELECT_KEEP_ONLINE);
    pp_navigation_open(&nav, PP_SCREEN_MAINTENANCE);
    assert(pp_navigation_selected(&nav) == PP_SELECT_RELOAD);
    pp_navigation_move(&nav, -1);
    assert(pp_navigation_selected(&nav) == PP_SELECT_BACK);
    pp_navigation_back(&nav);
    assert(nav.screen == PP_SCREEN_SETTINGS);
    pp_navigation_back(&nav);
    assert(nav.screen == PP_SCREEN_MENU);
    pp_navigation_back(&nav);
    assert(nav.screen == PP_SCREEN_HOME);
    pp_navigation_open(&nav, PP_SCREEN_TASK);
    pp_navigation_back(&nav);
    assert(nav.screen == PP_SCREEN_TASKS);
    pp_navigation_back(&nav);
    assert(nav.screen == PP_SCREEN_MENU);
    for (unsigned i = PP_SCREEN_MENU; i <= PP_SCREEN_MAINTENANCE; i++) {
        pp_navigation_open(&nav, i);
        assert(pp_navigation_local(&nav));
        assert(pp_navigation_count(&nav) <= 7);
        for (unsigned k = 0; k < pp_navigation_count(&nav); k++)
            assert(strlen(pp_navigation_label(&nav, k)) > 0);
    }
    pp_navigation_open(&nav, PP_SCREEN_HOME);
    pp_navigation_open(&nav, PP_SCREEN_SETTINGS);
    pp_navigation_open(&nav, PP_SCREEN_NETWORK);
    pp_navigation_back(&nav);
    assert(nav.screen == PP_SCREEN_SETTINGS);
    pp_navigation_open(&nav, PP_SCREEN_MAINTENANCE);
    for (unsigned i = 0; i < 3; i++)
        pp_navigation_move(&nav, 1);
    assert(pp_navigation_selected(&nav) == PP_SELECT_REPAIR);
    pp_navigation_back(&nav);
    pp_navigation_back(&nav);
    assert(nav.screen == PP_SCREEN_HOME);
    assert(pp_navigation_route(&nav, false, true, false) == PP_ROUTE_SETUP);
    assert(pp_navigation_route(&nav, true, true, false) == PP_ROUTE_CONFIRM);
    assert(pp_navigation_route(&nav, false, false, true) == PP_ROUTE_REVIEW);
    assert(pp_navigation_route(&nav, false, false, false) == PP_ROUTE_SESSION);
    pp_navigation_open(&nav, PP_SCREEN_SETTINGS);
    assert(pp_navigation_route(&nav, false, true, true) == PP_ROUTE_LOCAL);
    assert(pp_navigation_route(&nav, true, true, true) == PP_ROUTE_CONFIRM);
    pp_navigation_up_press(&nav, 100);
    assert(!pp_navigation_up_tick(&nav, 749));
    assert(pp_navigation_up_release(&nav));
    assert(!pp_navigation_up_release(&nav));
    pp_navigation_up_press(&nav, UINT32_MAX - 300);
    assert(pp_navigation_up_tick(&nav, 350));
    pp_navigation_open(&nav, PP_SCREEN_HOME);
    assert(!pp_navigation_up_tick(&nav, 351));
    assert(!pp_navigation_up_release(&nav));
    assert(!pp_navigation_up_release(&nav));
    pp_navigation_up_press(&nav, 2000);
    assert(pp_navigation_up_release(&nav));
    pp_navigation_open(&nav, PP_SCREEN_QUESTION);
    assert(pp_navigation_audio(&nav));
    pp_navigation_back(&nav);
    assert(nav.screen == PP_SCREEN_HOME);
    return 0;
}

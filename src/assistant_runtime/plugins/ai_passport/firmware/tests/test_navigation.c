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
        assert(pp_navigation_count(&nav) <= 5);
        for (unsigned k = 0; k < pp_navigation_count(&nav); k++)
            assert(strlen(pp_navigation_label(&nav, k)) > 0);
    }
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

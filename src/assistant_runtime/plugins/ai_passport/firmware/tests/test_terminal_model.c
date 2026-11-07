#include "terminal_model.h"
#include <assert.h>
#include <string.h>
int main(void) {
    pp_button_t b = {0};
    assert(pp_press(&b, 0, false) == PP_CAPTURE);
    assert(pp_tick(&b, 199) == PP_NONE);
    assert(pp_tick(&b, 200) == PP_BEGIN);
    assert(pp_tick(&b, 201) == PP_NONE);
    assert(pp_release(&b, 1000) == PP_FINISH);
    assert(pp_tick(&b, 1400) == PP_NONE);
    assert(pp_press(&b, 2000, false) == PP_CAPTURE);
    assert(pp_release(&b, 2100) == PP_DISCARD);
    assert(pp_tick(&b, 2300) == PP_NONE);
    assert(pp_press(&b, 2350, false) == PP_CAPTURE);
    assert(pp_release(&b, 2400) == (PP_DISCARD | PP_TOGGLE));
    assert(pp_tick(&b, 3000) == PP_NONE);
    assert(pp_press(&b, 4000, true) == (PP_CAPTURE | PP_STOP));
    assert(pp_release(&b, 4100) == PP_DISCARD);
    assert(pp_tick(&b, 4500) == PP_NONE);
    b = (pp_button_t){.wake_guard = true};
    assert(pp_press(&b, 0, false) == PP_NONE);
    assert(pp_release(&b, 1) == PP_NONE);
    assert(pp_press(&b, 10, false) == PP_CAPTURE);
    assert(pp_release(&b, 11) == PP_DISCARD);
    assert(pp_tick(&b, 312) == PP_MENU);
    b = (pp_button_t){0};
    pp_press(&b, UINT32_MAX - 100, false);
    assert(pp_tick(&b, 100) == PP_BEGIN);
    char out[8];
    assert(pp_text(out, 7, "中文X") == 6);
    assert(!strcmp(out, "中文"));
    assert(pp_text(out, 8, "中\xff文") == 7);
    assert(!strcmp(out, "中?文"));
    assert(pp_text(out, 2, "\xf0\x9f") == 1);
    assert(!strcmp(out, "?"));
    return 0;
}

#include "text_layout.h"
#include <assert.h>
#include <string.h>

static unsigned measure(uint32_t c, void *arg) {
    (void)arg;
    return c > 127 ? 16 : 8;
}
int main(void) {
    char out[128];
    pp_text_layout_t p = pp_layout_page(out, sizeof(out), "一二三四五六七", 0, 32, 2, measure, 0);
    assert(!strcmp(out, "一二\n三四") && p.next == 12 && p.more);
    pp_layout_page(out, sizeof(out), "一二三四五六七", p.next, 32, 2, measure, 0);
    assert(!strcmp(out, "五六\n七"));
    pp_layout_tail(out, sizeof(out), "一二三四五六七", 32, 2, measure, 0);
    assert(!strcmp(out, "五六\n七"));
    pp_layout_tail(out, sizeof(out), "ab\ncd\nef\ngh", 32, 2, measure, 0);
    assert(!strcmp(out, "ef\ngh"));
    pp_layout_page(out, sizeof(out), "a\r\nb\t好", 0, 32, 3, measure, 0);
    assert(!strcmp(out, "a\nb 好"));
    assert(pp_layout_previous("一二三四五六七", 12, 32, 2, measure, 0) == 0);
    assert(pp_layout_previous("一二三四五六七", 22, 32, 2, measure, 0) == 12);
    pp_layout_page(out, 5, "你好", 0, 32, 2, measure, 0);
    assert(!strcmp(out, "你"));
    pp_text_suffix(out, 8, "一二三四");
    assert(!strcmp(out, "三四"));
    pp_layout_tail(out, sizeof(out), "", 32, 2, measure, 0);
    assert(!out[0]);
    pp_layout_page(out, sizeof(out), "bad\xff\xe4", 0, 100, 2, measure, 0);
    assert(!strcmp(out, "bad??"));
    return 0;
}

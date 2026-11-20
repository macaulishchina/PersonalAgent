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
    const char *excerpt = "受凉。又嘱托茶房好好照应我。我心里暗笑他的迂；他们只认得钱，"
                          "托他们只是白托！而且我这样大年纪的人，难道还不能料理自己么？"
                          "唉，我现在想想，那时真是太聪明了！\n\n我";
    char screen[1024];
    for (unsigned size = 16; size <= 18; size += 2) {
        pp_layout_cursor_t cursor = {0};
        unsigned width = 216 * 16 / size;
        pp_layout_feed(&cursor, screen, sizeof(screen), excerpt, width, 9, measure, 0);
        assert(cursor.characters == 80);
        p = pp_layout_feed(&cursor, screen, sizeof(screen),
                           "买票的时候，看见父亲还站在那里。请接着读下去。请接着读下去。请接着读下去。请接着读下去。请接着读下去。请接着读下去。",
                           width, 9, measure, 0);
        assert(cursor.characters > 81 && cursor.lines == 9 && p.more);
        assert(strstr(screen, "我买票") && !strstr(screen, "\n\n"));
    }
    pp_layout_cursor_t c = {0};
    pp_layout_feed(&c, screen, sizeof(screen), "甲乙\r", 32, 2, measure, 0);
    p = pp_layout_feed(&c, screen, sizeof(screen), "\n\n丙丁\n\n戊", 32, 2, measure, 0);
    assert(!strcmp(screen, "甲乙\n丙丁") && c.characters == 9 && p.more);
    assert(pp_layout_previous("甲乙\r\n\n丙丁\n\n戊", strlen("甲乙\r\n\n丙丁\n\n"),
                              32, 2, measure, 0) == 0);
    return 0;
}

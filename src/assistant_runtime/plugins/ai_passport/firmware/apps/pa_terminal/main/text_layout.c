#include "text_layout.h"
#include <string.h>

static size_t decode(const char *s, uint32_t *value) {
    const unsigned char *p = (const unsigned char *)s;
    if (*p < 0x80) {
        *value = *p;
        return *p ? 1 : 0;
    }
    unsigned n = *p >= 0xc2 && *p <= 0xdf   ? 2
                 : *p >= 0xe0 && *p <= 0xef ? 3
                 : *p >= 0xf0 && *p <= 0xf4 ? 4
                                            : 0;
    uint32_t c = n ? *p & (0x7f >> n) : 0;
    for (unsigned i = 1; i < n; i++) {
        if ((p[i] & 0xc0) != 0x80) {
            n = 0;
            break;
        }
        c = c << 6 | (p[i] & 0x3f);
    }
    if (!n || (n == 3 && c < 0x800) || (n == 4 && c < 0x10000) || (c >= 0xd800 && c <= 0xdfff) ||
        c > 0x10ffff) {
        *value = '?';
        return 1;
    }
    *value = c;
    return n;
}

pp_text_layout_t pp_layout_feed(pp_layout_cursor_t *c, char *out, size_t cap, const char *text,
                                unsigned width, unsigned rows, pp_glyph_width_t measure,
                                void *context) {
    size_t pos = 0;
    while (width && rows && measure && text[pos]) {
        uint32_t glyph;
        size_t bytes = decode(text + pos, &glyph);
        if (glyph == '\r' || glyph == '\n') {
            c->paragraph_break = c->lines != 0;
        } else {
            unsigned advance = measure(glyph == '\t' ? ' ' : glyph, context);
            bool wrap = c->paragraph_break || (c->x && advance > width - c->x);
            bool replace = glyph == '?' && (unsigned char)text[pos] >= 0x80;
            size_t length = replace ? 1 : bytes;
            if ((wrap && c->lines >= rows) || (cap && c->written + wrap + length >= cap))
                break;
            if (wrap) {
                if (cap)
                    out[c->written] = '\n';
                c->written++;
                c->lines++;
                c->x = 0;
                c->paragraph_break = false;
            }
            if (!c->lines)
                c->lines = 1;
            if (cap) {
                if (replace || glyph == '\t')
                    out[c->written] = replace ? '?' : ' ';
                else
                    memcpy(out + c->written, text + pos, length);
            }
            c->written += length;
            c->x += advance > width - c->x ? width - c->x : advance;
        }
        pos += bytes;
        c->characters++;
    }
    if (cap)
        out[c->written] = 0;
    return (pp_text_layout_t){.next = pos, .lines = c->lines, .more = text[pos] != 0};
}

pp_text_layout_t pp_layout_page(char *out, size_t cap, const char *text, size_t start,
                                unsigned width, unsigned rows, pp_glyph_width_t measure,
                                void *context) {
    pp_layout_cursor_t cursor = {0};
    pp_text_layout_t page = pp_layout_feed(&cursor, out, cap, start <= strlen(text) ? text + start : "",
                                           width, rows, measure, context);
    page.next += start;
    return page;
}

size_t pp_layout_previous(const char *text, size_t start, unsigned width, unsigned rows,
                          pp_glyph_width_t measure, void *context) {
    if (start > strlen(text))
        start = strlen(text);
    size_t cursor = 0, previous = 0;
    while (cursor < start && text[cursor]) {
        pp_text_layout_t page =
            pp_layout_page(NULL, 0, text, cursor, width, rows, measure, context);
        if (page.next <= cursor || page.next >= start)
            return cursor;
        previous = cursor;
        cursor = page.next;
    }
    return previous;
}

void pp_layout_tail(char *out, size_t cap, const char *text, unsigned width, unsigned rows,
                    pp_glyph_width_t measure, void *context) {
    /* Only a ring of visible row starts, regardless of transcript length. */
    size_t starts[12] = {0}, pos = 0;
    unsigned count = 0;
    if (rows > 12)
        rows = 12;
    if (!rows) {
        if (cap)
            out[0] = 0;
        return;
    }
    while (text[pos]) {
        starts[count++ % rows] = pos;
        pp_text_layout_t row = pp_layout_page(NULL, 0, text, pos, width, 1, measure, context);
        if (row.next <= pos)
            break;
        pos = row.next;
    }
    size_t first = count > rows ? starts[count % rows] : 0;
    pp_layout_page(out, cap, text, first, width, rows, measure, context);
}

void pp_text_suffix(char *out, size_t cap, const char *text) {
    if (!cap)
        return;
    size_t length = strlen(text), start = length >= cap ? length - cap + 1 : 0;
    while (((unsigned char)text[start] & 0xc0) == 0x80)
        start++;
    memcpy(out, text + start, length - start + 1);
}

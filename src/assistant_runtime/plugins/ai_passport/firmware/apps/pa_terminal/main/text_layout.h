#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef unsigned (*pp_glyph_width_t)(uint32_t glyph, void *context);
typedef struct {
    size_t next; /* UTF-8 byte boundary in the original text, never a pixel offset. */
    unsigned lines;
    bool more;
} pp_text_layout_t;

/* Explicit whole lines. No scrolling viewport or partially clipped top row.
 * Width comes from the installed font; no assumption that ASCII and CJK match. */
pp_text_layout_t pp_layout_page(char *out, size_t capacity, const char *text, size_t start,
                                unsigned width, unsigned rows, pp_glyph_width_t measure,
                                void *context);
size_t pp_layout_previous(const char *text, size_t start, unsigned width, unsigned rows,
                          pp_glyph_width_t measure, void *context);
void pp_layout_tail(char *out, size_t capacity, const char *text, unsigned width, unsigned rows,
                    pp_glyph_width_t measure, void *context);
/* Copies a bounded suffix, aligned to a Unicode character. Full text remains on PA. */
void pp_text_suffix(char *out, size_t capacity, const char *text);

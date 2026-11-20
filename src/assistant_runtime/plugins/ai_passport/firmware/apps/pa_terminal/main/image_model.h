#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PP_IMAGE_MAX_WIDTH 160u
#define PP_IMAGE_MAX_HEIGHT 96u
#define PP_IMAGE_PREVIEW_WIDTH 64u
#define PP_IMAGE_PREVIEW_HEIGHT 32u
#define PP_IMAGE_VIEW_WIDTH 216u
#define PP_IMAGE_VIEW_HEIGHT 194u
#define PP_IMAGE_STRIP_ROWS 8u
#define PP_IMAGE_STRIP_HEADER 20u
#define PP_IMAGE_STRIP_MAX (PP_IMAGE_STRIP_HEADER + PP_IMAGE_VIEW_WIDTH * PP_IMAGE_STRIP_ROWS * 2u)

typedef struct {
    uint32_t request;
    unsigned row, rows;
} pp_image_strip_t;
bool pp_image_strip_tag(const void *frame, size_t bytes);
bool pp_image_strip_parse(pp_image_strip_t *strip, const void *frame, size_t bytes,
                          unsigned width, unsigned height);
void pp_image_preview_dimensions(unsigned width, unsigned height, unsigned *w, unsigned *h);
void pp_image_preview_rows(unsigned char *out, unsigned w, unsigned h, const unsigned char *pixels,
                           unsigned width, unsigned height, unsigned row, unsigned rows);

/* Pixel storage is separate from the heap needed by transport and audio. */
size_t pp_image_headroom(bool background);
bool pp_image_has_headroom(size_t available, size_t bytes, bool background);
bool pp_image_request_size(size_t available, size_t largest, size_t limit, bool background,
                           unsigned *width, unsigned *height);

typedef struct {
    char version[65];
    unsigned width, height;
    size_t bytes, received;
} pp_image_transfer_t;
/* Validates native-pixel metadata before any allocation. */
bool pp_image_begin(pp_image_transfer_t *image, const char *format, const char *version,
                    unsigned width, unsigned height, size_t bytes, unsigned requested_width,
                    unsigned requested_height);
/* Exact sequence, bounded chunks, complete pixels; no gaps, overlaps or stale versions. */
bool pp_image_chunk(pp_image_transfer_t *image, const char *version, size_t offset, size_t length,
                    size_t next);
/* In-place nearest-neighbor downsampling after the display released this
 * buffer. No second pixel allocation. Caller recomputes the derived hash. */
bool pp_image_preview(pp_image_transfer_t *image, unsigned char *pixels, unsigned width,
                      unsigned height);

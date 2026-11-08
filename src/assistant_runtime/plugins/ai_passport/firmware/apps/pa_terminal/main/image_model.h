#pragma once
#include <stdbool.h>
#include <stddef.h>

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

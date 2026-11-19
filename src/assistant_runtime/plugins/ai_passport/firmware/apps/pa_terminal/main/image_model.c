#include "image_model.h"
#include <string.h>

bool pp_image_strip_tag(const void *frame, size_t bytes) {
    return bytes >= 4 && !memcmp(frame, "\xff\xff\xff\xff", 4);
}
bool pp_image_strip_parse(pp_image_strip_t *strip, const void *frame, size_t bytes,
                          unsigned width, unsigned height) {
    if (bytes < PP_IMAGE_STRIP_HEADER || bytes > PP_IMAGE_STRIP_MAX ||
        !pp_image_strip_tag(frame, bytes) || !width || width > PP_IMAGE_VIEW_WIDTH ||
        !height || height > PP_IMAGE_VIEW_HEIGHT)
        return false;
    const unsigned char *p = frame;
    unsigned row = ((unsigned)p[8] << 8) | p[9];
    unsigned rows = ((unsigned)p[10] << 8) | p[11];
    uint32_t request = ((uint32_t)p[4] << 24) | ((uint32_t)p[5] << 16) | ((uint32_t)p[6] << 8) | p[7];
    if (!request || row >= height || row % PP_IMAGE_STRIP_ROWS ||
        rows != (height - row < PP_IMAGE_STRIP_ROWS ? height - row : PP_IMAGE_STRIP_ROWS) ||
        bytes != PP_IMAGE_STRIP_HEADER + (size_t)width * rows * 2)
        return false;
    *strip = (pp_image_strip_t){request, row, rows};
    return true;
}

bool pp_image_has_headroom(size_t available, size_t bytes, bool background) {
    /* Background prefetch preserves more headroom for the next gesture. */
    const size_t headroom = background ? 20000u : 16u * 1024u;
    return available >= headroom && bytes <= available - headroom;
}

bool pp_image_request_size(size_t available, size_t largest, size_t limit, bool background,
                           unsigned *width, unsigned *height) {
    const unsigned sizes[][2] = {
        {PP_IMAGE_MAX_WIDTH, PP_IMAGE_MAX_HEIGHT}, {128, 80}, {96, 64},
        {80, 48}, {64, 48}, {PP_IMAGE_PREVIEW_WIDTH, PP_IMAGE_PREVIEW_HEIGHT},
        {48, 32}, {40, 24}, {32, 24},
    };
    const size_t slack = 1024u;
    for (unsigned i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
        unsigned w = sizes[i][0], h = sizes[i][1];
        size_t bytes = (size_t)w * h * 2;
        if (background && (w > PP_IMAGE_PREVIEW_WIDTH || h > PP_IMAGE_PREVIEW_HEIGHT))
            continue;
        if (bytes <= limit && largest >= bytes + slack &&
            pp_image_has_headroom(available, bytes, background)) {
            *width = w;
            *height = h;
            return true;
        }
    }
    return false;
}

bool pp_image_begin(pp_image_transfer_t *image, const char *format, const char *version,
                    unsigned width, unsigned height, size_t bytes, unsigned requested_width,
                    unsigned requested_height) {
    if (strcmp(format, "rgb565le") || strlen(version) != 64 || !width || !height ||
        width > requested_width || height > requested_height ||
        width > PP_IMAGE_VIEW_WIDTH || height > PP_IMAGE_VIEW_HEIGHT ||
        bytes != (size_t)width * height * 2)
        return false;
    for (unsigned i = 0; i < 64; i++)
        if (!((version[i] >= '0' && version[i] <= '9') || (version[i] >= 'a' && version[i] <= 'f')))
            return false;
    *image = (pp_image_transfer_t){.width = width, .height = height, .bytes = bytes};
    strcpy(image->version, version);
    return true;
}
bool pp_image_chunk(pp_image_transfer_t *image, const char *version, size_t offset, size_t length,
                    size_t next) {
    if (!image->bytes || strcmp(version, image->version) || offset != image->received || !length ||
        length > 2048 || length % 2 || length > image->bytes - image->received ||
        next != (offset + length < image->bytes ? offset + length : 0))
        return false;
    image->received += length;
    return true;
}
void pp_image_preview_dimensions(unsigned width, unsigned height, unsigned *w, unsigned *h) {
    *w = width;
    *h = height;
    if (width <= PP_IMAGE_PREVIEW_WIDTH && height <= PP_IMAGE_PREVIEW_HEIGHT)
        return;
    if (width * PP_IMAGE_PREVIEW_HEIGHT > height * PP_IMAGE_PREVIEW_WIDTH) {
        *w = PP_IMAGE_PREVIEW_WIDTH;
        *h = height * *w / width;
    } else {
        *h = PP_IMAGE_PREVIEW_HEIGHT;
        *w = width * *h / height;
    }
    if (!*w)
        *w = 1;
    if (!*h)
        *h = 1;
}
void pp_image_preview_rows(unsigned char *out, unsigned w, unsigned h, const unsigned char *pixels,
                           unsigned width, unsigned height, unsigned row, unsigned rows) {
    for (unsigned y = 0; y < h; y++) {
        unsigned source = y * height / h;
        if (source < row || source >= row + rows)
            continue;
        const unsigned char *line = pixels + (size_t)(source - row) * width * 2;
        for (unsigned x = 0; x < w; x++)
            memcpy(out + ((size_t)y * w + x) * 2, line + (x * width / w) * 2, 2);
    }
}
bool pp_image_preview(pp_image_transfer_t *image, unsigned char *pixels, unsigned width,
                      unsigned height) {
    if (!pixels || !width || !height || !image->width || !image->height ||
        image->width > PP_IMAGE_MAX_WIDTH || image->height > PP_IMAGE_MAX_HEIGHT ||
        image->bytes != (size_t)image->width * image->height * 2)
        return false;
    unsigned w = image->width, h = image->height;
    if (w <= width && h <= height)
        return true;
    if ((size_t)w * height > (size_t)h * width) {
        h = h * width / w;
        w = width;
    } else {
        w = w * height / h;
        h = height;
    }
    if (!w)
        w = 1;
    if (!h)
        h = 1;
    for (unsigned y = 0; y < h; y++)
        for (unsigned x = 0; x < w; x++) {
            size_t source =
                ((size_t)(y * image->height / h) * image->width + x * image->width / w) * 2;
            size_t target = ((size_t)y * w + x) * 2;
            pixels[target] = pixels[source];
            pixels[target + 1] = pixels[source + 1];
        }
    image->width = w;
    image->height = h;
    image->bytes = image->received = (size_t)w * h * 2;
    return true;
}

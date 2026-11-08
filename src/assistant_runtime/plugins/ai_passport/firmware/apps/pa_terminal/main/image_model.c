#include "image_model.h"
#include <string.h>

bool pp_image_begin(pp_image_transfer_t *image, const char *format, const char *version,
                    unsigned width, unsigned height, size_t bytes, unsigned requested_width,
                    unsigned requested_height) {
    if (strcmp(format, "rgb565le") || strlen(version) != 64 || !width || !height ||
        width > requested_width || height > requested_height || width > 160 || height > 96 ||
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
bool pp_image_preview(pp_image_transfer_t *image, unsigned char *pixels, unsigned width,
                      unsigned height) {
    if (!pixels || !width || !height || !image->width || !image->height || image->width > 160 ||
        image->height > 96 || image->bytes != (size_t)image->width * image->height * 2)
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

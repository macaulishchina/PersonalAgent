#include "image_model.h"
#include <assert.h>
#include <string.h>

static void test_strips(void) {
    unsigned char frame[PP_IMAGE_STRIP_MAX] = {0xff, 0xff, 0xff, 0xff, 0, 0, 0, 7,
                                               0, 0, 0, PP_IMAGE_STRIP_ROWS};
    pp_image_strip_t strip = {0};
    assert(pp_image_strip_parse(&strip, frame, sizeof(frame), 216, 122));
    assert(strip.request == 7 && strip.row == 0 && strip.rows == 8);
    assert(!pp_image_strip_parse(&strip, frame, sizeof(frame) - 1, 216, 122));
    assert(!pp_image_strip_parse(&strip, frame, sizeof(frame), 217, 122));
    frame[7] = 0;
    assert(!pp_image_strip_parse(&strip, frame, sizeof(frame), 216, 122));
    frame[7] = 7;
    frame[9] = 120;
    frame[11] = 2;
    assert(pp_image_strip_parse(&strip, frame, PP_IMAGE_STRIP_HEADER + 216 * 4, 216, 122));
    frame[9] = 119;
    assert(!pp_image_strip_parse(&strip, frame, PP_IMAGE_STRIP_HEADER + 216 * 4, 216, 122));

    unsigned w, h;
    pp_image_preview_dimensions(216, 122, &w, &h);
    assert(w == 56 && h == 32);
    unsigned char source[64], preview[16] = {0};
    for (unsigned i = 0; i < sizeof(source); i++)
        source[i] = i;
    pp_image_preview_rows(preview, 4, 2, source, 8, 4, 0, 1);
    pp_image_preview_rows(preview, 4, 2, source + 16, 8, 4, 1, 3);
    const unsigned char expected[] = {0, 1, 4, 5, 8, 9, 12, 13, 32, 33, 36, 37, 40, 41, 44, 45};
    assert(!memcmp(preview, expected, sizeof(expected)));
}

int main(void) {
    test_strips();
    const char *version = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef";
    pp_image_transfer_t image = {0};
    assert(!pp_image_begin(&image, "png", version, 96, 64, 12288, 96, 64));
    assert(!pp_image_begin(&image, "rgb565le", "not-a-hash", 96, 64, 12288, 96, 64));
    assert(!pp_image_begin(&image, "rgb565le", version, 97, 64, 12416, 96, 64));
    assert(pp_image_begin(&image, "rgb565le", version, 96, 64, 12288, 96, 64));
    assert(!pp_image_chunk(&image, version, 2, 2048, 2050));
    assert(!pp_image_chunk(&image, "stale", 0, 2048, 2048));
    assert(!pp_image_chunk(&image, version, 0, 2047, 2047));
    assert(image.received == 0);
    for (size_t offset = 0; offset < 12288; offset += 2048)
        assert(pp_image_chunk(&image, version, offset, 2048, offset < 10240 ? offset + 2048 : 0));
    assert(image.received == image.bytes);
    assert(!pp_image_chunk(&image, version, 12288, 2, 0));
    unsigned char pixels[64];
    for (unsigned i = 0; i < sizeof(pixels); i++)
        pixels[i] = i;
    assert(pp_image_begin(&image, "rgb565le", version, 8, 4, sizeof(pixels), 8, 4));
    assert(pp_image_preview(&image, pixels, 4, 2));
    assert(image.width == 4 && image.height == 2 && image.bytes == 16);
    const unsigned selected[] = {0, 4, 8, 12, 32, 36, 40, 44};
    for (unsigned i = 0; i < 8; i++) {
        assert(pixels[i * 2] == selected[i]);
        assert(pixels[i * 2 + 1] == selected[i] + 1);
    }
    assert(pp_image_preview(&image, pixels, 64, 32) && image.bytes == 16); /* Never upscale. */
    assert(!pp_image_preview(&image, pixels, 0, 32));
    assert(!pp_image_preview(&image, NULL, 64, 32));

    unsigned width, height;
    assert(pp_image_request_size(24168, 9728, 30720, false, &width, &height));
    assert(width == 80 && height == 48);
    assert(pp_image_request_size(24168, 9728, 30720, true, &width, &height));
    assert(width == 64 && height == 32);
    assert(pp_image_request_size(21000, 9728, 30720, false, &width, &height));
    assert(width == 64 && height == 32);
    assert(pp_image_request_size(24168, 9728, 4095, false, &width, &height));
    assert(width == 48 && height == 32);
    assert(pp_image_request_size(24168, 3490, 30720, false, &width, &height));
    assert(width == 40 && height == 24);
    assert(!pp_image_request_size(17919, 9728, 30720, false, &width, &height));
    assert(!pp_image_request_size(24168, 2559, 30720, false, &width, &height));
    assert(pp_image_has_headroom(20480, 4096, false));
    assert(!pp_image_has_headroom(20479, 4096, false));
    assert(!pp_image_has_headroom(20480, 4096, true));
    return 0;
}

#include "image_model.h"
#include <assert.h>
#include <string.h>
int main(void) {
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
    return 0;
}

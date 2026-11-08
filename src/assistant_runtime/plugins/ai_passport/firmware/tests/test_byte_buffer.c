#include "byte_buffer.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>

static bool reject_allocation;
void *pp_test_realloc(void *pointer, size_t size) {
    return reject_allocation ? NULL : realloc(pointer, size);
}

int main(void) {
    pp_buffer_t b = {0};
    unsigned char frame[4096];
    memset(frame, 0x5a, sizeof(frame));
    assert(!b.data && !b.capacity);
    assert(pp_buffer_append(&b, "a", 1, 16384));
    assert(b.capacity < 8192 && b.size == 1 && b.data[1] == 0);
    char *original = b.data;
    reject_allocation = true;
    assert(!pp_buffer_append(&b, frame, sizeof(frame), 16384));
    assert(b.data == original && b.size == 1 && !strcmp(b.data, "a"));
    reject_allocation = false;
    pp_buffer_free(&b);
    for (int i = 0; i < 4; i++)
        assert(pp_buffer_append(&b, frame, sizeof(frame), 16384));
    assert(b.size == 16384 && b.data[b.size] == 0);
    assert(!memcmp(b.data, frame, sizeof(frame)));
    assert(!pp_buffer_append(&b, "x", 1, 16384));
    assert(b.size == 16384);
    pp_buffer_free(&b);
    assert(!b.data && !b.size && !b.capacity);
    assert(pp_buffer_append(&b, frame, 4096, 8191));
    assert(pp_buffer_append(&b, frame, 4095, 8191));
    assert(!pp_buffer_append(&b, "x", 1, 8191));
    pp_buffer_free(&b);
    pp_buffer_free(&b);
    return 0;
}

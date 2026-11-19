#undef realloc
#include "byte_buffer.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>

static bool reject_allocation;
void *pp_test_realloc(void *pointer, size_t size) {
    return reject_allocation ? NULL : realloc(pointer, size);
}

static unsigned completed[4], completions;
static bool allocated[4];
static void done(unsigned ticket, pp_buffer_t buffer) {
    completed[completions] = ticket;
    allocated[completions++] = buffer.data != NULL;
    pp_buffer_free(&buffer);
}

static void wait_order(void) {
    unsigned background = pp_memory_wait(32, PP_MEMORY_BACKGROUND, PP_MEMORY_CACHE, 300, done);
    unsigned normal = pp_memory_wait(32, PP_MEMORY_NORMAL, PP_MEMORY_IMAGE, 100, done);
    unsigned critical = pp_memory_wait(32, PP_MEMORY_CRITICAL, PP_MEMORY_AUDIO, 200, done);
    assert(background && normal && critical && !pp_memory_wait(32, PP_MEMORY_NORMAL, PP_MEMORY_IMAGE, 300, done));
    pp_memory_poll(50);
    assert(completions == 1 && completed[0] == critical && allocated[0]);
    pp_memory_cancel(background);
    pp_memory_cancel(background);
    pp_memory_poll(100);
    assert(completions == 3 && completed[1] == background && completed[2] == normal);
    assert(!allocated[1] && !allocated[2] && pp_memory_stats().expired == 1);
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
    for (int i = 0; i < 3; i++)
        assert(pp_buffer_append(&b, frame, sizeof(frame), 16384));
    assert(pp_buffer_append(&b, frame, sizeof(frame) - 1, 16384));
    /* Even at the payload ceiling, another allocation for the NUL must be unnecessary. */
    reject_allocation = true;
    assert(pp_buffer_append(&b, frame, 1, 16384));
    reject_allocation = false;
    assert(b.size == 16384 && b.data[b.size] == 0);
    assert(!memcmp(b.data, frame, sizeof(frame)));
    assert(!pp_buffer_append(&b, "x", 1, 16384));
    assert(b.size == 16384);
    size_t capacity = b.capacity;
    pp_buffer_set_use(&b, PP_MEMORY_CACHE);
    assert(!pp_memory_stats().live[PP_MEMORY_WIRE]);
    assert(pp_memory_stats().live[PP_MEMORY_CACHE] == capacity);
    pp_buffer_free(&b);
    assert(!pp_memory_stats().live[PP_MEMORY_CACHE]);
    assert(!b.data && !b.size && !b.capacity);
    assert(pp_buffer_append(&b, frame, 4096, 8191));
    assert(pp_buffer_append(&b, frame, 4095, 8191));
    assert(!pp_buffer_append(&b, "x", 1, 8191));
    pp_buffer_free(&b);
    pp_buffer_free(&b);
    wait_order();
    return 0;
}

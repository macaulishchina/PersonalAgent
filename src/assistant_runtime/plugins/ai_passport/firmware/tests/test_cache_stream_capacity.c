#include "cache_stream.h"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <string.h>

/* Stop the consumer completely. This reaches a physically full backlog,
 * independently of host timing, audio sampling limits and network buffering. */
enum { SECTORS = 768, FRAME = 640, RAM_BYTES = 40 * 1024 };
static unsigned char flash[SECTORS][PP_STREAM_PAGE], ram[RAM_BYTES];
static unsigned available, attempts;
static pp_stream_t stream;

static void forget(void) {}
static pp_stream_io_t transfer(bool write, unsigned session, unsigned page, void *data) {
    assert(session && page < SECTORS);
    if (write) {
        attempts++;
        if (page >= available)
            return PP_STREAM_NO_SPACE;
        memcpy(flash[page], data, PP_STREAM_PAGE);
    } else {
        assert(page < available);
        memcpy(data, flash[page], PP_STREAM_PAGE);
    }
    return PP_STREAM_IO_OK;
}

static void service(void) {
    unsigned iterations = 0;
    while (pp_stream_service())
        assert(++iterations < SECTORS + 8);
}

static void fill_frame(uint32_t *frame, unsigned seq, unsigned generation) {
    for (unsigned i = 0; i < FRAME / sizeof(*frame); i++)
        frame[i] = generation + seq * (FRAME / sizeof(*frame)) + i;
}

static void blocked_consumer(unsigned clean_sectors, unsigned generation) {
    pp_buffer_t block = {.data = (char *)ram, .capacity = sizeof(ram)};
    uint32_t frame[FRAME / sizeof(uint32_t)], actual[FRAME / sizeof(uint32_t)];
    pp_stream_stats_t before = pp_stream_stats();
    available = clean_sectors;
    attempts = 0;
    assert(pp_stream_prepare(&stream, 0));
    pp_stream_reset(&stream, &block, sizeof(ram), sizeof(ram));
    assert(pp_stream_capacity(&stream) == SECTORS * PP_STREAM_PAGE + RAM_BYTES);
    unsigned frames = (clean_sectors * PP_STREAM_PAGE + RAM_BYTES) / FRAME;
    for (unsigned i = 0; i < frames; i++) {
        fill_frame(frame, i, generation);
        assert(pp_stream_append(&stream, frame, FRAME));
        service();
    }
    assert(pp_stream_size(&stream) == frames * FRAME);
    assert(pp_stream_stats().stored_bytes == clean_sectors * PP_STREAM_PAGE);
    assert(pp_stream_capacity(&stream) == pp_stream_size(&stream));
    assert(!pp_stream_room(&stream) && !pp_stream_append(&stream, frame, FRAME));
    assert(pp_stream_stats().full == before.full + 1);
    assert(pp_stream_stats().errors == before.errors && !pp_stream_failed(&stream));
    assert(attempts == clean_sectors + (clean_sectors < SECTORS));
    unsigned credit = pp_stream_take_released(&stream);
    for (unsigned i = 0; i < frames; i++) {
        size_t from_ram;
        assert(pp_stream_read(&stream, actual, FRAME, &from_ram) == FRAME);
        fill_frame(frame, i, generation);
        assert(!memcmp(actual, frame, FRAME));
        credit += from_ram;
        service();
    }
    assert(credit == frames * FRAME && !pp_stream_size(&stream));
    assert(pp_stream_capacity(&stream) == RAM_BYTES);
    /* Resume ingress within the same session after all clean sectors are used. */
    fill_frame(frame, frames, generation);
    assert(pp_stream_append(&stream, frame, FRAME));
    assert(pp_stream_read(&stream, actual, FRAME, NULL) == FRAME && !memcmp(actual, frame, FRAME));
    pp_stream_reset(&stream, NULL, 0, 0);
    service();
    assert(pp_stream_idle() && !pp_stream_stats().workspace_bytes);
}

int main(void) {
    pp_stream_attach((pp_stream_disk_t){.pages = SECTORS, .transfer = transfer,
                                        .forget = forget, .single_pass = true});
    blocked_consumer(SECTORS, 0);
    blocked_consumer(0, 1000000);
    blocked_consumer(3, 2000000);
    blocked_consumer(SECTORS, 3000000);
    return 0;
}

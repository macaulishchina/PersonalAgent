#include "cache_stream.h"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <string.h>

#define P PP_STREAM_PAGE
#define SECTORS 3u
static unsigned char pages[SECTORS][P], ram[2 * P], source[18 * P], result[sizeof(source)];
static pp_buffer_t block;
static pp_stream_t fifo;
static unsigned canceled, consume_while_writing;
static bool fail_write, fail_read;

static void wake(void) {}
static void forget(void) {}

static pp_stream_io_t transfer(bool write, unsigned session, unsigned seq, void *bytes) {
    assert(session);
    if (write) {
        if (consume_while_writing) {
            assert(pp_stream_read(&fifo, result, consume_while_writing, NULL) == consume_while_writing);
            consume_while_writing = 0;
        }
        if (canceled) {
            canceled = 0;
            pp_stream_reset(&fifo, &block, sizeof(ram), sizeof(ram));
            assert(pp_stream_append(&fifo, "new", 3));
        }
        memcpy(pages[seq % SECTORS], bytes, P);
        memset(bytes, 0, P); /* encryption can overwrite the worker copy */
        return !fail_write;
    }
    memcpy(bytes, pages[seq % SECTORS], P);
    return !fail_read;
}

static void pump(void) {
    unsigned spins = 0;
    while (pp_stream_service())
        assert(++spins < 20);
}

static void restart(void) {
    assert(pp_stream_prepare(&fifo, 0));
    pp_stream_reset(&fifo, &block, sizeof(ram), sizeof(ram));
    pump();
}

int main(void) {
    block = (pp_buffer_t){.data = (char *)ram, .capacity = sizeof(ram)};
    for (size_t i = 0; i < sizeof(source); i++)
        source[i] = (unsigned char)(i * 31 + i / P);
    pp_stream_attach((pp_stream_disk_t){SECTORS, wake, transfer, forget, false});
    restart();
    assert(pp_stream_stats().capacity_bytes == SECTORS * P && !pp_stream_stats().stored_bytes);
    /* The second FIFO can still work in RAM while the first holds flash. */
    unsigned char other_ram[256];
    pp_buffer_t other_block = {.data = (char *)other_ram, .capacity = sizeof(other_ram)};
    pp_stream_t other = {0};
    assert(!pp_stream_prepare(&other, 0));
    pp_stream_reset(&other, &other_block, sizeof(other_ram), sizeof(other_ram));
    assert(pp_stream_append(&other, source, sizeof(other_ram)));
    assert(pp_stream_read(&other, result, sizeof(other_ram), NULL) == sizeof(other_ram));
    assert(!memcmp(source, result, sizeof(other_ram)));
    pp_stream_reset(&other, NULL, 0, 0);

    size_t sent = 0, consumed = 0, returned = 0;
    while (consumed < sizeof(source)) {
        while (sent < sizeof(source) && pp_stream_append(&fifo, source + sent, 640)) {
            sent += 640;
            pump();
        }
        size_t from_ram;
        size_t n = pp_stream_read(&fifo, result + consumed, 640, &from_ram);
        assert(n == 640);
        consumed += n;
        returned += from_ram + pp_stream_take_released(&fifo);
        pump();
        assert(pp_stream_size(&fifo) == sent - consumed);
        assert(pp_stream_stats().stored_bytes == pp_stream_size(&fifo) - fifo.used);
    }
    assert(!memcmp(source, result, consumed) && returned == sent);
    assert(pp_stream_stats().peak_bytes == SECTORS * P && pp_stream_stats().reads > SECTORS);
    restart();
    consume_while_writing = 640;
    assert(pp_stream_append(&fifo, source, 2 * P));
    pump();
    assert(pp_stream_take_released(&fifo) == P - 640);
    assert(pp_stream_read(&fifo, result + 640, 2 * P, NULL) == 2 * P - 640);
    assert(!memcmp(result, source, 2 * P));

    restart();
    consume_while_writing = P;
    assert(pp_stream_append(&fifo, source, 2 * P));
    pump();
    assert(!pp_stream_take_released(&fifo));
    assert(pp_stream_append(&fifo, source + 2 * P, P));
    pump();
    assert(pp_stream_take_released(&fifo) == P);
    assert(pp_stream_read(&fifo, result + P, 2 * P, NULL) == 2 * P);
    assert(!memcmp(result, source, 3 * P));

    restart();
    fail_write = true;
    assert(pp_stream_append(&fifo, source, 2 * P));
    pump();
    assert(!pp_stream_failed(&fifo) && !pp_stream_take_released(&fifo));
    assert(pp_stream_read(&fifo, result, 2 * P, NULL) == 2 * P);
    assert(!memcmp(result, source, 2 * P));
    fail_write = false;
    restart();
    assert(pp_stream_append(&fifo, source, 2 * P));
    pump();
    assert(pp_stream_append(&fifo, source, P));
    fail_read = true;
    /* Program the second page then fail its anticipatory read. */
    pump();
    assert(pp_stream_read(&fifo, result, P, NULL) == P);
    pump();
    assert(pp_stream_failed(&fifo) && pp_stream_size(&fifo) == 2 * P);
    assert(!pp_stream_read(&fifo, result, 640, NULL));
    fail_read = false;

    restart();
    canceled = 1;
    assert(pp_stream_append(&fifo, source, 2 * P));
    pump();
    assert(pp_stream_read(&fifo, result, 4, NULL) == 3 && !memcmp(result, "new", 3));
    assert(!pp_stream_take_released(&fifo));
    pp_stream_reset(&fifo, NULL, 0, 0);
    pump();
    assert(!pp_memory_stats().live[PP_MEMORY_CACHE]);
    pp_stream_attach((pp_stream_disk_t){SECTORS, wake, transfer, forget, true});
    restart();
    assert(pp_stream_stats().workspace_bytes == 3 * P);
    unsigned before_errors = pp_stream_stats().errors;
    unsigned before_writes = pp_stream_stats().writes;
    sent = consumed = returned = 0;
    while (consumed < sizeof(source)) {
        while (sent < sizeof(source) && pp_stream_append(&fifo, source + sent, 640)) {
            sent += 640;
            pump();
        }
        size_t from_ram;
        size_t n = pp_stream_read(&fifo, result + consumed, consumed ? 640 : 2 * P, &from_ram);
        assert(n == (consumed ? 640 : 2 * P)); /* both read pages available without worker I/O */
        consumed += n;
        returned += from_ram + pp_stream_take_released(&fifo);
        pump();
    }
    assert(!memcmp(source, result, consumed) && returned == sent);
    assert(pp_stream_stats().writes == before_writes + SECTORS);
    assert(pp_stream_stats().errors == before_errors && pp_stream_stats().full == 1);
    pp_stream_reset(&fifo, NULL, 0, 0);
    pump();
    assert(!pp_stream_stats().workspace_bytes);
    return 0;
}

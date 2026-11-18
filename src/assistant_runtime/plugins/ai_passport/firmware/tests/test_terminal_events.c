/* Only the linked firmware sources use the instrumented allocator. */
#undef malloc
#undef free
#include "terminal_events.h"
#include "network.h"
#include "terminal_limits.h"
#include <assert.h>
#include <stdlib.h>
#include <string.h>

struct host_queue {
    pp_event_t items[12];
    size_t count, capacity;
};
static struct host_queue queues[2];
static unsigned queue_count, copies;
static bool fail_copy;
static char *watched;
static size_t watched_capacity;

QueueHandle_t xQueueCreate(size_t slots, size_t item_size) {
    assert(queue_count < 2 && slots <= 12 && item_size == sizeof(pp_event_t));
    QueueHandle_t q = &queues[queue_count++];
    q->capacity = slots;
    return q;
}
int xQueueSend(QueueHandle_t q, const void *item, unsigned wait) {
    assert(!wait);
    if (q->count == q->capacity)
        return 0;
    q->items[q->count++] = *(const pp_event_t *)item;
    return pdTRUE;
}
int xQueueReceive(QueueHandle_t q, void *item, unsigned wait) {
    assert(!wait);
    if (!q->count)
        return 0;
    *(pp_event_t *)item = q->items[0];
    memmove(q->items, q->items + 1, (--q->count) * sizeof(pp_event_t));
    return pdTRUE;
}
void xQueueReset(QueueHandle_t q) { q->count = 0; }
void vQueueDelete(QueueHandle_t q) { q->count = 0; }
void pp_audio_capture_release(void) {}
void *pp_test_realloc(void *p, size_t n) { return realloc(p, n); }
void *pp_test_malloc(size_t n) {
    copies++;
    return fail_copy ? NULL : malloc(n);
}
void pp_test_free(void *p) {
    if (watched && watched == p) {
        for (size_t i = 0; i < watched_capacity; i++)
            assert(watched[i] == 0);
        watched = NULL;
    }
    free(p);
}
static void watch_release(pp_buffer_t frame) {
    assert(frame.data && !watched);
    watched = frame.data;
    watched_capacity = frame.capacity;
}
static pp_buffer_t frame_of_size(size_t size) {
    static const char chunk[512] = "wifi-secret";
    pp_buffer_t frame = {0};
    while (frame.size < size) {
        size_t left = size - frame.size;
        assert(pp_buffer_append(&frame, chunk, left < sizeof(chunk) ? left : sizeof(chunk),
                                PP_WIRE_MESSAGE_BYTES));
    }
    /* A reused frame may have sensitive bytes in unused capacity as well. */
    if (frame.capacity > size + 1)
        memset(frame.data + size + 1, 's', frame.capacity - size - 1);
    return frame;
}
static void check_small_and_borrowed(void) {
    pp_buffer_t small = frame_of_size(PP_NET_REUSE_CAPACITY - 1);
    assert(small.capacity == PP_NET_REUSE_CAPACITY);
    char *original = small.data;
    pp_received_frame(&small, 4);
    assert(small.data == original);
    small.data[0] = 'x';
    pp_event_t e;
    assert(pp_events_next(&e) && e.text.data != small.data && e.text.data[0] == 'w');
    watch_release(e.text);
    pp_buffer_free(&e.text);
    assert(!watched);
    fail_copy = true;
    pp_received_frame(&small, 4);
    assert(pp_events_take_error() && !pp_events_next(&e) && small.data == original);
    fail_copy = false;
    pp_buffer_free(&small);

    char cached[] = "history";
    pp_received(cached, strlen(cached));
    cached[0] = 'x';
    assert(pp_events_next(&e) && e.generation == 0 && !strcmp(e.text.data, "history"));
    pp_buffer_free(&e.text);
    pp_received_frame(&(pp_buffer_t){0}, 4);
    assert(pp_events_next(&e) && e.text.size == 0 && e.text.data[0] == 0);
    pp_buffer_free(&e.text);
}
static void check_transfer(size_t size) {
    pp_buffer_t frame = frame_of_size(size);
    assert(frame.capacity > PP_NET_REUSE_CAPACITY);
    char *original = frame.data;
    unsigned before = copies;
    fail_copy = true;
    pp_received_frame(&frame, 4);
    assert(!frame.data && !frame.size && !frame.capacity && copies == before);
    pp_event_t e;
    assert(pp_events_next(&e) && e.generation == 4 && e.kind == PP_EVENT_MESSAGE && e.text.data == original);
    assert(e.text.size == size && e.text.data[size] == 0 && !memcmp(e.text.data, "wifi-secret", 10));
    watch_release(e.text);
    pp_buffer_free(&e.text);
    assert(!watched && !pp_events_take_error());
    fail_copy = false;
}
static void check_failed_delivery(void) {
    pp_buffer_t frame = frame_of_size(PP_NET_REUSE_CAPACITY);
    for (unsigned i = 0; i < 8; i++)
        pp_network_state(true, "online", 4);
    assert(!pp_events_take_error());
    unsigned before = copies;
    watch_release(frame);
    pp_received_frame(&frame, 4);
    assert(!watched && !frame.data && copies == before && pp_events_take_error());
    pp_event_t e;
    for (unsigned i = 0; i < 8; i++) {
        assert(pp_events_next(&e) && e.kind == PP_EVENT_LINK && e.online);
        pp_buffer_free(&e.text);
    }
    assert(!pp_events_next(&e) && !pp_events_take_error());

    frame = frame_of_size(PP_WIRE_MESSAGE_BYTES);
    frame.size++;
    watch_release(frame);
    pp_received_frame(&frame, 4);
    assert(!watched && !frame.data && pp_events_take_error() && !pp_events_next(&e));
}
int main(void) {
    assert(pp_events_init());
    check_small_and_borrowed();
    check_transfer(PP_NET_REUSE_CAPACITY);
    check_transfer(PP_WIRE_MESSAGE_BYTES);
    check_failed_delivery();
    return 0;
}

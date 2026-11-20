#include "terminal_limits.h"
#include "terminal_events.h"
#include "terminal_audio.h"
#include "network.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

static QueueHandle_t messages, keys;
static atomic_bool key_overflow, event_error;

bool pp_events_init(void) {
    keys = xQueueCreate(12, sizeof(pp_event_t));
    messages = xQueueCreate(8, sizeof(pp_event_t));
    if (messages && keys)
        return true;
    if (messages)
        vQueueDelete(messages);
    if (keys)
        vQueueDelete(keys);
    messages = keys = NULL;
    return false;
}
void pp_events_key(bsp_btn_t key, bsp_btn_ev_t action, void *arg) {
    (void)arg;
    if (action != BSP_BTN_PRESS && action != BSP_BTN_RELEASE)
        return;
    if (key == BSP_BTN_OK && action == BSP_BTN_RELEASE)
        pp_audio_capture_release();
    pp_event_t e = {.kind = PP_EVENT_KEY, .key = key, .button = action};
    if (xQueueSend(keys, &e, 0) != pdTRUE)
        key_overflow = true;
}
static void enqueue_owned(pp_event_t event) {
    if (!messages || xQueueSend(messages, &event, 0) != pdTRUE) {
        pp_buffer_free(&event.text);
        if (event.kind != PP_EVENT_IMAGE)
            event_error = true;
    }
}
static void push_text(pp_event_kind_t kind, bool online, uint32_t generation, const char *text, size_t bytes) {
    if (bytes > PP_WIRE_MESSAGE_BYTES) {
        event_error = true;
        return;
    }
    pp_buffer_t copy = {0};
    if (!pp_buffer_reserve(&copy, bytes + 1, PP_MEMORY_CRITICAL, PP_MEMORY_WIRE)) {
        event_error = true;
        return;
    }
    memcpy(copy.data, text, bytes);
    copy.data[bytes] = 0;
    copy.size = bytes;
    enqueue_owned((pp_event_t){.kind = kind, .online = online, .generation = generation, .text = copy});
}
void pp_received(const char *text, size_t bytes) {
    push_text(PP_EVENT_MESSAGE, false, 0, text, bytes);
}
void pp_received_frame(pp_buffer_t *frame, uint32_t generation) {
    if (frame->size > PP_WIRE_MESSAGE_BYTES ||
        (frame->data ? frame->size >= frame->capacity : frame->size || frame->capacity)) {
        pp_buffer_free(frame);
        event_error = true;
        return;
    }
    if (frame->capacity <= PP_NET_REUSE_CAPACITY) {
        push_text(PP_EVENT_MESSAGE, false, generation, frame->data ? frame->data : "", frame->size);
        return;
    }
    pp_buffer_t owned = pp_buffer_take(frame);
    owned.data[owned.size] = 0;
    enqueue_owned((pp_event_t){.kind = PP_EVENT_MESSAGE, .generation = generation, .text = owned});
}
void pp_network_state(bool online, const char *reason, uint32_t generation) {
    push_text(PP_EVENT_LINK, online, generation, reason, strlen(reason));
}
void pp_received_image(pp_buffer_t *frame, uint32_t generation) {
    pp_buffer_set_use(frame, PP_MEMORY_IMAGE);
    enqueue_owned((pp_event_t){.kind = PP_EVENT_IMAGE, .generation = generation,
                               .text = pp_buffer_take(frame)});
}
bool pp_events_next(pp_event_t *event) {
    return xQueueReceive(keys, event, 0) || xQueueReceive(messages, event, 0);
}
void pp_events_wait_for_wake(void) {
    pp_event_t event;
    /* A release from the sleep menu is not a wake gesture. */
    for (;;) {
        if (xQueueReceive(keys, &event, portMAX_DELAY) && event.button == BSP_BTN_PRESS)
            return;
    }
}
bool pp_events_take_key_overflow(void) {
    if (!atomic_exchange(&key_overflow, false))
        return false;
    xQueueReset(keys);
    return true;
}
bool pp_events_take_error(void) { return atomic_exchange(&event_error, false); }

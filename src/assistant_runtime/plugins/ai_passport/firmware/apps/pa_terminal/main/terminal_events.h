#pragma once
#include "bsp_button.h"
#include "byte_buffer.h"
#include <stdbool.h>
#include <stdint.h>

typedef enum { PP_EVENT_KEY, PP_EVENT_MESSAGE, PP_EVENT_LINK, PP_EVENT_IMAGE } pp_event_kind_t;
typedef struct {
    pp_event_kind_t kind;
    bsp_btn_t key;
    bsp_btn_ev_t button;
    bool online;
    uint32_t generation; /* Zero for local activity. */
    pp_buffer_t text; /* Consumer calls pp_buffer_free() after dispatch. */
} pp_event_t;

bool pp_events_init(void);
void pp_events_key(bsp_btn_t key, bsp_btn_ev_t action, void *arg);
bool pp_events_next(pp_event_t *event);
bool pp_events_take_key_overflow(void);
bool pp_events_take_error(void);

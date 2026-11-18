#pragma once
#include <stdbool.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stddef.h>

#define PP_WIFI_MAX 5
#define PP_WIFI_ROUNDS 3
/* One explicit connection flow, never an unbounded restart of failed rounds. */
typedef struct {
    unsigned count, attempt;
} pp_wifi_plan_t;
bool pp_wifi_next(pp_wifi_plan_t *plan, unsigned *index, unsigned *round);
bool pp_wifi_valid(const char *ssid, const char *password);
bool pp_server_url_valid(const char *url);
bool pp_server_endpoint(const char *root, const char *route, bool websocket, char *destination,
                        size_t capacity);

/* One transport generation owns its handshake, queued frames and commands.
 * Generation 0 is reserved for local events such as offline history reads. */
typedef struct { _Atomic uint32_t state; } pp_link_t;
uint32_t pp_link_begin(pp_link_t *link);
uint32_t pp_link_generation(const pp_link_t *link);
bool pp_link_open(pp_link_t *link, uint32_t generation);
bool pp_link_accept(pp_link_t *link, uint32_t generation, unsigned protocol);
void pp_link_close(pp_link_t *link, uint32_t generation);
bool pp_link_connected(const pp_link_t *link, uint32_t generation);
uint32_t pp_link_ready(const pp_link_t *link);

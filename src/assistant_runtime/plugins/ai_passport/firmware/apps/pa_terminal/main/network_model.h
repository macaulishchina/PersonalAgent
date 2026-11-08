#pragma once
#include <stdbool.h>
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

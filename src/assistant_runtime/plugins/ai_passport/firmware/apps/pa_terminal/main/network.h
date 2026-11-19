#pragma once
#include "byte_buffer.h"
#include "cJSON.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
bool pp_net_start(void);
bool pp_net_send(cJSON *command); /* Consumes JSON on all paths. */
bool pp_net_pcm(uint32_t sequence, const void *pcm, size_t bytes);
bool pp_net_has_capacity(bool pcm);
void pp_net_interrupt_input(void);
bool pp_net_connected(void); /* Application handshake, not only TCP. */
uint32_t pp_net_generation(void);
bool pp_net_current_frame(uint32_t generation);
bool pp_net_accept_protocol(unsigned protocol, uint32_t generation);
bool pp_net_rejected(void);
void pp_net_repair(void);
void pp_net_retry(void);
void pp_net_reload(void);
void pp_net_provision(void);
bool pp_net_stop(void);
bool pp_net_wifi_details(char *ssid, size_t capacity, int *rssi);
/* Application callbacks only enqueue bounded work, never block this transport. */
void pp_received(const char *text, size_t bytes);
/* Large completed frames transfer ownership on success and on failure; small
 * frames are copied so the transport can reuse its receive buffer. */
#define PP_NET_REUSE_CAPACITY 4096u
void pp_received_frame(pp_buffer_t *frame, uint32_t generation);
bool pp_play_pcm(uint32_t generation, const void *pcm, size_t bytes);
void pp_network_state(bool ready, const char *reason, uint32_t generation);

#pragma once
#include "cJSON.h"
#include "settings.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
void pp_net_start(pp_settings_t *settings);
bool pp_net_send(cJSON *command); /* Consumes JSON on all paths. */
bool pp_net_pcm(uint32_t sequence, const void *pcm, size_t bytes);
bool pp_net_connected(void);
void pp_net_stop(void);
/* Application callbacks only enqueue bounded work, never block this transport. */
void pp_received(const char *text, size_t bytes);
bool pp_play_pcm(uint32_t generation, const void *pcm, size_t bytes);
void pp_network_state(bool ready, const char *reason);

#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Initialize the shared storage worker before opening any stream. */
void pp_cache_init(void);
/* Logical usage of the available cache tiers; snapshot without disk I/O. */
typedef struct { uint32_t used, capacity; } pp_cache_usage_t;
pp_cache_usage_t pp_cache_usage(void);
bool pp_stream_flash_attach(void (*wake)(void));
bool pp_stream_flash_prepare(void);

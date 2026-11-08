#pragma once
#include <stdbool.h>
#include <stddef.h>

typedef struct {
    char *data;
    size_t size, capacity;
} pp_buffer_t;

/* Allocates only when data arrives; the limit excludes the terminating zero. */
bool pp_buffer_append(pp_buffer_t *buffer, const void *data, size_t bytes, size_t limit);
void pp_buffer_free(pp_buffer_t *buffer);

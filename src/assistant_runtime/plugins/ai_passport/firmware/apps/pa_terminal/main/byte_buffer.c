#include "byte_buffer.h"
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

bool pp_buffer_append(pp_buffer_t *b, const void *data, size_t bytes, size_t limit) {
    if (!data || !bytes || limit == SIZE_MAX || b->size > limit || bytes > limit - b->size)
        return false;
    size_t needed = b->size + bytes + 1;
    if (needed > b->capacity) {
        size_t capacity = b->capacity ? b->capacity : 128;
        while (capacity < needed) {
            if (capacity > (limit + 1) / 2) {
                capacity = limit + 1;
                break;
            }
            capacity *= 2;
        }
        if (capacity > limit + 1)
            capacity = limit + 1;
        char *next = realloc(b->data, capacity);
        if (!next)
            return false;
        b->data = next;
        b->capacity = capacity;
    }
    memcpy(b->data + b->size, data, bytes);
    b->size += bytes;
    b->data[b->size] = 0;
    return true;
}

void pp_buffer_free(pp_buffer_t *b) {
    if (b->data) {
        volatile unsigned char *p = (volatile unsigned char *)b->data;
        for (size_t i = 0; i < b->capacity; i++)
            p[i] = 0;
        free(b->data);
    }
    *b = (pp_buffer_t){0};
}

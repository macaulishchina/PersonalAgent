#pragma once
#include "byte_buffer.h"

#define PP_CACHE_VALUE_MAX 32768u
#define PP_CACHE_AUX_MAX 96u
#define PP_CACHE_RESIDENT_BYTES 4096u

typedef enum {
    PP_CACHE_HIT, PP_CACHE_MISSING, PP_CACHE_CORRUPT, PP_CACHE_BUSY,
    PP_CACHE_NO_MEMORY, PP_CACHE_IO_ERROR, PP_CACHE_FULL
} pp_cache_status_t;

typedef struct {
    uint64_t sequence;
    pp_buffer_t bytes;
    size_t aux_size;
    unsigned char aux[PP_CACHE_AUX_MAX];
} pp_cache_value_t;

typedef struct {
    unsigned ticket, lease;
    uint64_t cursor; /* Directory position survives an unreadable record body. */
    size_t charge;
    bool written;
    pp_cache_status_t status;
    pp_cache_value_t value; /* Read-only until release. */
} pp_cache_result_t;
typedef struct {
    bool ready;
    unsigned hits, misses, write_failures, busy, ram_hits, flash_hits, recoverable, in_flight, io_peak_ms;
    unsigned stack_min_free; /* ESP-IDF task stack high-water mark, in bytes. */
} pp_cache_info_t;

typedef enum { PP_SEEK_EXACT, PP_SEEK_LATEST, PP_SEEK_PREVIOUS, PP_SEEK_NEXT } pp_cache_seek_t;
typedef enum { PP_OBJECT_NORMAL, PP_OBJECT_INDEX, PP_HISTORY_HEAD, PP_HISTORY_PART } pp_object_kind_t;
typedef struct {
    pp_cache_seek_t seek;
    uint64_t cursor;
    pp_memory_priority_t priority;
    size_t heap_reserve;
} pp_object_read_t;
typedef struct {
    uint64_t import_order;
    size_t disk_reserve;
    pp_object_kind_t kind;
} pp_object_write_t;

bool pp_objects_identity(const char *const parts[], unsigned count, char identity[65]);
void pp_objects_configure(const char *scope, bool enabled);
bool pp_objects_read(const char *key, unsigned ticket, pp_object_read_t options);
/* Takes ownership whether or not the request is accepted. Aux is copied. */
bool pp_objects_write(const char *key, const char *owner, pp_object_write_t options, pp_buffer_t *buffer,
                       const void *aux, size_t aux_size, unsigned ticket);
/* Keeps the input on refused admission. Failed ticketed writes return its bytes in the result. */
bool pp_objects_try_write(const char *key, const char *owner, pp_object_write_t options,
                          pp_buffer_t *buffer, const void *aux, size_t aux_size, unsigned ticket);
bool pp_objects_next(pp_cache_result_t *result);
void pp_objects_release(pp_cache_result_t *result);
bool pp_objects_retain(const pp_cache_result_t *result, pp_cache_result_t *retained);
void pp_objects_allow_io(bool allowed);
void pp_objects_connecting(bool connecting);
pp_cache_info_t pp_objects_info(void);

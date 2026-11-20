#pragma once
#include "byte_buffer.h"

/* Bounded FIFO backed by caller-owned RAM blocks. Reset before releasing
 * blocks. Readers/writers never perform storage I/O. One FIFO at a time may
 * borrow the flash region; other live FIFOs retain their full RAM capacity. */
typedef struct {
    pp_buffer_t *blocks;
    size_t block, capacity, head, used;
    uint32_t removed, released;
} pp_stream_t;

/* Controller reserves spill, current read and lookahead pages before optional ring growth.
 * A failed reservation leaves a fully functional RAM-only FIFO. */
bool pp_stream_prepare(pp_stream_t *s, size_t free_floor);
void pp_stream_reset(pp_stream_t *s, pp_buffer_t *blocks, size_t block, size_t capacity);
bool pp_stream_append(pp_stream_t *s, const void *data, size_t n);
size_t pp_stream_read(pp_stream_t *s, void *data, size_t n, size_t *from_ram);
size_t pp_stream_size(pp_stream_t *s);
/* Reachable FIFO depth including spill space. This is not ingress credit. */
size_t pp_stream_capacity(pp_stream_t *s);
size_t pp_stream_room(pp_stream_t *s);
unsigned pp_stream_take_released(pp_stream_t *s);
bool pp_stream_failed(pp_stream_t *s);

/* The cache worker exclusively drives I/O. Each logical page occupies one
 * erase sector and contains a whole number of 640-byte capture frames. */
#define PP_STREAM_PAGE 3840u
typedef enum { PP_STREAM_IO_ERROR, PP_STREAM_IO_OK, PP_STREAM_NO_SPACE } pp_stream_io_t;
typedef struct {
    unsigned pages;
    void (*wake)(void);
    pp_stream_io_t (*transfer)(bool write, unsigned session, unsigned page, void *bytes);
    void (*forget)(void);
    bool single_pass; /* Flash can only erase between leases. */
} pp_stream_disk_t;

void pp_stream_attach(pp_stream_disk_t disk);
bool pp_stream_service(void);
bool pp_stream_idle(void);
typedef struct {
    unsigned capacity_bytes, stored_bytes, peak_bytes, writes, reads, errors;
    unsigned workspace_bytes, ram_only, full;
} pp_stream_stats_t;
pp_stream_stats_t pp_stream_stats(void);

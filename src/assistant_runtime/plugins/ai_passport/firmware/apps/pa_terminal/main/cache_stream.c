#include "cache_stream.h"
#include <string.h>
#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
static portMUX_TYPE mutex = portMUX_INITIALIZER_UNLOCKED;
#define LOCK() portENTER_CRITICAL(&mutex)
#define UNLOCK() portEXIT_CRITICAL(&mutex)
#else
#define LOCK() ((void)0)
#define UNLOCK() ((void)0)
#endif

static pp_stream_disk_t disk;
static pp_stream_t *owner, *reserved;
static unsigned epoch, first, next, offset;
static bool ready[2], failed, stop_writes;
static pp_stream_stats_t stats;
/* Two read slots indexed by logical page parity and one private I/O slot.
 * The worker alone retires the workspace, after its last transfer returns. */
static pp_buffer_t pages[3];
static unsigned worker_epoch;

static size_t stored(void) { return (next - first) * PP_STREAM_PAGE - offset; }

static void wake(void) {
    if (disk.wake)
        disk.wake();
}

void pp_stream_attach(pp_stream_disk_t backend) {
    /* Installed by the controller before starting the audio tasks. */
    disk = backend;
}

bool pp_stream_prepare(pp_stream_t *s, size_t free_floor) {
    LOCK();
    bool available = disk.pages && (!reserved || reserved == s);
    if (available)
        reserved = s;
    else
        stats.ram_only++;
    bool installed = pages[0].data != NULL;
    UNLOCK();
    if (!available || installed)
        return available;
    pp_buffer_t workspace[3] = {0};
    bool ok = pp_buffers_try_acquire(workspace, 3, PP_STREAM_PAGE, free_floor, PP_MEMORY_CACHE);
    LOCK();
    if (ok)
        memcpy(pages, workspace, sizeof(pages));
    else {
        reserved = NULL;
        stats.ram_only++;
    }
    UNLOCK();
    return ok;
}

void pp_stream_reset(pp_stream_t *s, pp_buffer_t *blocks, size_t block, size_t capacity) {
    LOCK();
    if (owner == s || reserved == s) {
        owner = capacity ? s : NULL;
        epoch++;
        first = next = offset = 0;
        ready[0] = ready[1] = failed = stop_writes = false;
        if (!capacity)
            reserved = NULL;
    }
    *s = (pp_stream_t){.blocks = blocks, .block = block, .capacity = capacity};
    UNLOCK();
    wake();
}

static void copy_ring(pp_stream_t *s, size_t pos, void *bytes, size_t n, bool put) {
    unsigned char *p = bytes;
    while (n) {
        size_t within = pos % s->block;
        size_t span = s->block - within;
        if (span > n)
            span = n;
        char *slot = s->blocks[pos / s->block].data + within;
        if (put)
            memcpy(slot, p, span);
        else
            memcpy(p, slot, span);
        n -= span;
        p += span;
        pos = (pos + span) % s->capacity;
    }
}

static void consume_ram(pp_stream_t *s, size_t n) {
    s->head = (s->head + n) % s->capacity;
    s->used -= n;
    s->removed += n;
}

/* Upper bound on additional logical pages for this lease. A write discovering
 * a dirty sector or an I/O error can reduce this to zero between polls. */
static unsigned writable_pages(pp_stream_t *s) {
    if (owner != s || stop_writes || failed)
        return 0;
    return disk.pages - (disk.single_pass ? next : next - first);
}

size_t pp_stream_capacity(pp_stream_t *s) {
    LOCK();
    size_t limit = s->capacity;
    if (owner == s)
        limit += stored() + (size_t)writable_pages(s) * PP_STREAM_PAGE;
    UNLOCK();
    return limit;
}

static bool can_spill(pp_stream_t *s) {
    return writable_pages(s) && s->used >= PP_STREAM_PAGE &&
           s->used >= s->capacity - s->capacity / 4;
}

bool pp_stream_append(pp_stream_t *s, const void *data, size_t n) {
    LOCK();
    bool ok = n && data && n <= s->capacity - s->used && !(owner == s && failed);
    if (ok) {
        copy_ring(s, (s->head + s->used) % s->capacity, (void *)data, n, true);
        s->used += n;
    }
    bool notify = ok && can_spill(s);
    UNLOCK();
    if (notify)
        wake();
    return ok;
}

size_t pp_stream_read(pp_stream_t *s, void *data, size_t n, size_t *from_ram) {
    size_t copied = 0;
    bool notify = false;
    LOCK();
    while (copied < n && owner == s && first != next && ready[first % 2]) {
        size_t part = PP_STREAM_PAGE - offset;
        if (part > n - copied)
            part = n - copied;
        memcpy((char *)data + copied, pages[first % 2].data + offset, part);
        copied += part;
        offset += part;
        if (offset == PP_STREAM_PAGE) {
            ready[first % 2] = false;
            first++;
            offset = 0;
            notify = true;
        }
    }
    size_t take = n - copied;
    if (owner == s && first != next)
        take = 0;
    if (take > s->used)
        take = s->used;
    if (take) {
        copy_ring(s, s->head, (char *)data + copied, take, false);
        consume_ram(s, take);
    }
    UNLOCK();
    if (notify)
        wake();
    if (from_ram)
        *from_ram = take;
    return copied + take;
}

size_t pp_stream_size(pp_stream_t *s) {
    LOCK();
    size_t n = s->used + (owner == s ? stored() : 0);
    UNLOCK();
    return n;
}
size_t pp_stream_room(pp_stream_t *s) {
    LOCK();
    size_t n = s->capacity - s->used;
    UNLOCK();
    return n;
}
unsigned pp_stream_take_released(pp_stream_t *s) {
    LOCK();
    unsigned n = s->released;
    s->released = 0;
    UNLOCK();
    return n;
}
bool pp_stream_failed(pp_stream_t *s) {
    LOCK();
    bool broken = owner == s && failed;
    UNLOCK();
    return broken;
}
pp_stream_stats_t pp_stream_stats(void) {
    LOCK();
    pp_stream_stats_t result = stats;
    result.capacity_bytes = disk.pages * PP_STREAM_PAGE;
    result.stored_bytes = stored();
    result.workspace_bytes = pages[0].capacity + pages[1].capacity + pages[2].capacity;
    UNLOCK();
    return result;
}

bool pp_stream_idle(void) {
    LOCK();
    bool idle = !reserved;
    UNLOCK();
    return idle;
}

bool pp_stream_service(void) {
    LOCK();
    unsigned session = epoch;
    bool reset = worker_epoch != session;
    if (reset) {
        pp_buffer_t retired[3] = {0};
        if (!reserved) {
            memcpy(retired, pages, sizeof(pages));
            memset(pages, 0, sizeof(pages));
        }
        worker_epoch = session;
        UNLOCK();
        pp_buffers_release(retired, 3);
        disk.forget();
        return true;
    }
    unsigned page = first;
    if (page < next && ready[page % 2])
        page++;
    bool read = owner && !failed && page < next && !ready[page % 2];
    if (!read && (!owner || !can_spill(owner))) {
        UNLOCK();
        return false;
    }
    if (!read)
        page = next;
    char *scratch = pages[2].data;
    unsigned removed = owner->removed;
    if (!read)
        copy_ring(owner, owner->head, scratch, PP_STREAM_PAGE, false);
    UNLOCK();

    pp_stream_io_t status = disk.transfer(!read, session, page, scratch);

    LOCK();
    if (epoch == session) {
        if (status != PP_STREAM_IO_OK) {
            if (!read && status == PP_STREAM_NO_SPACE)
                stats.full++;
            else
                stats.errors++;
            stop_writes = true;
            if (read)
                failed = true;
        } else if (read) {
            pp_buffer_t swap = pages[2];
            pages[2] = pages[page % 2];
            pages[page % 2] = swap;
            ready[page % 2] = true;
            stats.reads++;
        } else {
            stats.writes++;
            if (disk.single_pass && next + 1 == disk.pages)
                stats.full++;
            /* Reads may have consumed the snapshotted prefix during the
             * flash write. Retire/credit only bytes still in ingress RAM. */
            unsigned skipped = owner->removed - removed;
            if (skipped < PP_STREAM_PAGE) {
                size_t retired = PP_STREAM_PAGE - skipped;
                if (first == next) {
                    offset = skipped;
                    copy_ring(owner, owner->head, pages[first % 2].data + skipped, retired, false);
                    ready[first % 2] = true;
                }
                consume_ram(owner, retired);
                owner->released += retired;
                next++;
                size_t bytes = stored();
                if (bytes > stats.peak_bytes)
                    stats.peak_bytes = bytes;
            } else {
                /* The reader took the whole prefix during programming. Its
                 * sector was still used. Move past it before the next write. */
                first++;
                next++;
            }
        }
    }
    UNLOCK();
    return true;
}

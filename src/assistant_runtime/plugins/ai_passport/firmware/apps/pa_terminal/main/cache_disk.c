#include "object_cache.h"
#include "cache_store.h"
#include "cache_resident.h"
#include "image_model.h"
#include "esp_littlefs.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lfs.h"
#include "terminal_limits.h"
#include "mbedtls/sha256.h"
#include <stdatomic.h>
#include <string.h>
#include <stdio.h>

#define QUEUE_SIZE 2u
typedef struct {
    unsigned ticket, generation, revision, pin_count;
    pp_memory_priority_t priority;
    bool read, index;
    char key[65];
    unsigned char scope[16], owner[16], pins[PP_CACHE_RECENT][16];
    pp_cache_value_t value;
} job_t;
typedef struct {
    unsigned generation, revision;
    bool index;
    pp_cache_result_t result;
} reply_t;
static QueueHandle_t results;
static TaskHandle_t worker_task;
static job_t queued[QUEUE_SIZE];
static unsigned queued_count;
static portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
static pp_cache_resident_t resident;
static atomic_uint generation, index_revision, in_flight;
static atomic_bool quiet = true, reconnecting, storage_ready;
static atomic_uint hits, misses, write_failures, busy, ram_hits, flash_hits, io_peak_ms;
static unsigned char scope[16], pins[PP_CACHE_RECENT][16];
static unsigned pin_count, outstanding; /* Controller owns admission/completion. */
static bool enabled, initialized;
static bool rebuilt;

static void digest(const void *data, size_t size, unsigned char result[32]) {
    if (mbedtls_sha256(data, size, result, 0))
        memset(result, 0, 32);
}
bool pp_objects_identity(const char *url, const char *device, const char *binding, char identity[65]) {
    mbedtls_sha256_context ctx;
    unsigned char hash[32];
    const char *parts[] = {url, device, binding};
    mbedtls_sha256_init(&ctx);
    int result = mbedtls_sha256_starts(&ctx, 0);
    for (unsigned i = 0; i < 3 && result == 0; i++)
        result = mbedtls_sha256_update(&ctx, (const unsigned char *)parts[i], strlen(parts[i]) + 1);
    if (result == 0)
        result = mbedtls_sha256_finish(&ctx, hash);
    mbedtls_sha256_free(&ctx);
    if (result != 0) {
        identity[0] = 0;
        return false;
    }
    for (unsigned i = 0; i < 32; i++)
        snprintf(identity + i * 2, 3, "%02x", hash[i]);
    return true;
}
static int probe_read(const struct lfs_config *cfg, lfs_block_t block, lfs_off_t offset,
                      void *data, lfs_size_t size) {
    const esp_partition_t *partition = cfg->context;
    if (!atomic_load(&quiet) || atomic_load(&reconnecting) ||
        block >= partition->size / cfg->block_size)
        return LFS_ERR_IO;
    return esp_partition_read(partition, (size_t)block * cfg->block_size + offset, data, size) ==
           ESP_OK ? 0 : LFS_ERR_IO;
}
static int probe_program(const struct lfs_config *cfg, lfs_block_t b, lfs_off_t off,
                         const void *data, lfs_size_t size) {
    (void)cfg; (void)b; (void)off; (void)data; (void)size;
    return LFS_ERR_IO;
}
static int probe_erase(const struct lfs_config *cfg, lfs_block_t b) {
    (void)cfg; (void)b;
    return LFS_ERR_IO;
}
static int probe_sync(const struct lfs_config *cfg) {
    (void)cfg;
    return LFS_ERR_IO;
}
static bool confirmed_corrupt(const esp_partition_t *partition) {
    lfs_t lfs;
    const struct lfs_config config = {
        .context = (void *)partition, .read = probe_read, .prog = probe_program,
        .erase = probe_erase, .sync = probe_sync, .block_size = partition->erase_size,
        .read_size = CONFIG_LITTLEFS_READ_SIZE, .prog_size = CONFIG_LITTLEFS_WRITE_SIZE,
        .cache_size = CONFIG_LITTLEFS_CACHE_SIZE, .lookahead_size = CONFIG_LITTLEFS_LOOKAHEAD_SIZE,
        .block_cycles = CONFIG_LITTLEFS_BLOCK_CYCLES,
    };
    int status = lfs_mount(&lfs, &config);
    if (status == 0)
        lfs_unmount(&lfs);
    return status == LFS_ERR_CORRUPT; /* An erased superblock is also uninitialized. */
}
static bool mount_store(pp_cache_store_t *store, bool *mounted) {
    const esp_partition_t *partition = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_LITTLEFS, "pp_cache");
    if (!partition || partition->address != 0x780000 || partition->size != 512 * 1024)
        return false;
    esp_vfs_littlefs_conf_t cfg = {.base_path = "/ppcache", .partition_label = "pp_cache"};
    if (!*mounted) {
        esp_err_t status = esp_vfs_littlefs_register(&cfg);
        if (status == ESP_FAIL && !rebuilt && confirmed_corrupt(partition) &&
            atomic_load(&quiet) && !atomic_load(&reconnecting)) {
            rebuilt = true;
            if (esp_littlefs_format("pp_cache") == ESP_OK)
                status = esp_vfs_littlefs_register(&cfg);
        }
        *mounted = status == ESP_OK;
    }
    return *mounted && pp_cache_store_open(store, "/ppcache", PP_CACHE_BUDGET);
}
static bool may_run(void *context) {
    const job_t *job = context;
    return atomic_load(&quiet) && !atomic_load(&reconnecting) &&
           job->generation == atomic_load(&generation) &&
           (!job->index || job->revision == atomic_load(&index_revision));
}
static bool charge(size_t bytes) {
    unsigned prior = atomic_load(&in_flight);
    do {
        if (bytes > PP_CACHE_VALUE_MAX - prior)
            return false;
    } while (!atomic_compare_exchange_weak(&in_flight, &prior, prior + bytes));
    return true;
}
static bool reserve_read(pp_buffer_t *buffer, size_t capacity, void *context) {
    const job_t *job = context;
    size_t available = heap_caps_get_free_size(MALLOC_CAP_8BIT);
    bool picture = (job->ticket & 3u) == 3u;
    bool fits = picture
                    ? pp_image_has_headroom(available, capacity, job->priority == PP_MEMORY_BACKGROUND)
                    : available >= capacity + PP_PLAYBACK_WINDOW;
    if (!fits || !charge(capacity))
        return false;
    if (pp_buffer_reserve(buffer, capacity, job->priority, PP_MEMORY_CACHE))
        return true;
    atomic_fetch_sub(&in_flight, capacity);
    return false;
}
static void reclaim(size_t capacity, pp_memory_priority_t priority) {
    size_t released = 0;
    do {
        portENTER_CRITICAL(&lock);
        pp_buffer_t buffer = pp_resident_remove(&resident, priority, 0);
        portEXIT_CRITICAL(&lock);
        if (!buffer.data)
            break;
        released += buffer.capacity;
        pp_buffer_free(&buffer);
    } while (released < capacity);
}
static pp_buffer_t take_resident(size_t capacity, pp_memory_priority_t priority) {
    portENTER_CRITICAL(&lock);
    pp_buffer_t buffer = pp_resident_remove(&resident, priority, capacity);
    portEXIT_CRITICAL(&lock);
    return buffer;
}
static bool remember(const job_t *job, pp_cache_value_t *value, pp_cache_result_t *borrow) {
    if (!value->bytes.data || value->bytes.capacity > PP_CACHE_RESIDENT_BYTES)
        return false;
    for (unsigned attempt = 0; attempt < 3; attempt++) {
        portENTER_CRITICAL(&lock);
        bool current = may_run((void *)job);
        if (current && pp_resident_insert(&resident, job->key, value, job->priority)) {
            if (borrow)
                pp_resident_borrow(&resident, job->key, borrow);
            portEXIT_CRITICAL(&lock);
            return true;
        }
        pp_buffer_t victim = current && attempt < 2
                                 ? pp_resident_remove(&resident, job->priority, 0)
                                 : (pp_buffer_t){0};
        portEXIT_CRITICAL(&lock);
        if (!victim.data)
            return false;
        pp_buffer_free(&victim);
    }
    return false;
}
static bool dequeue(job_t *job) {
    portENTER_CRITICAL(&lock);
    if (!queued_count) {
        portEXIT_CRITICAL(&lock);
        return false;
    }
    unsigned slot = queued_count == 2 && queued[1].priority > queued[0].priority ? 1 : 0;
    *job = queued[slot];
    if (slot == 0 && queued_count == 2)
        queued[0] = queued[1];
    --queued_count;
    portEXIT_CRITICAL(&lock);
    return true;
}
static void worker(void *unused) {
    (void)unused;
    pp_cache_store_t store;
    bool mounted = false;
    TickType_t last_mount = 0;
    bool attempted = false;
    for (;;) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        job_t job;
        while (dequeue(&job)) {
            reply_t reply = {.generation = job.generation, .revision = job.revision, .index = job.index,
                             .result = {.ticket = job.ticket, .written = !job.read,
                                        .status = PP_CACHE_BUSY}};
            pp_cache_result_t *r = &reply.result;
            int64_t started = esp_timer_get_time();
            if (may_run(&job) && !atomic_load(&storage_ready) &&
                (!attempted || xTaskGetTickCount() - last_mount >= pdMS_TO_TICKS(2000))) {
                attempted = true;
                last_mount = xTaskGetTickCount();
                bool ready = mount_store(&store, &mounted);
                if (ready) {
                    store.allowed = may_run;
                    store.context = &job;
                    ready = pp_cache_store_recover(&store);
                }
                atomic_store(&storage_ready, ready);
            }
            if (may_run(&job)) {
                r->status = PP_CACHE_IO_ERROR;
                if (atomic_load(&storage_ready)) {
                    pp_cache_store_scope(&store, job.scope, job.pins, job.pin_count);
                    store.allowed = may_run;
                    store.context = &job;
                    if (job.read) {
                        r->status = pp_cache_store_read(&store, job.key, &r->value, reserve_read, &job);
                        r->charge = r->value.bytes.capacity;
                        if (r->status == PP_CACHE_HIT && may_run(&job)) {
                            pp_cache_value_t stored = r->value;
                            remember(&job, &stored, r);
                            atomic_fetch_add(&flash_hits, 1);
                            atomic_fetch_add(&hits, 1);
                        }
                    } else {
                        r->status = pp_cache_store_write(&store, job.key, job.owner, job.index,
                                                         job.value.bytes.data, job.value.bytes.size,
                                                         job.value.aux, job.value.aux_size);
                        if (job.index && r->status == PP_CACHE_HIT) {
                            portENTER_CRITICAL(&lock);
                            pp_resident_invalidate(&resident, job.key);
                            portEXIT_CRITICAL(&lock);
                        }
                    }
                }
            }
            if (job.value.bytes.data) {
                size_t charged = job.value.bytes.capacity;
                /* Only a committed object may enter the reclaimable tier. */
                if (r->status == PP_CACHE_HIT)
                    remember(&job, &job.value, NULL);
                pp_buffer_free(&job.value.bytes);
                atomic_fetch_sub(&in_flight, charged);
            }
            if (r->status == PP_CACHE_BUSY || r->status == PP_CACHE_NO_MEMORY)
                atomic_fetch_add(&busy, 1);
            else if (job.read && (r->status == PP_CACHE_CORRUPT || r->status == PP_CACHE_MISSING))
                atomic_fetch_add(&misses, 1);
            else if (!job.read && r->status != PP_CACHE_HIT)
                atomic_fetch_add(&write_failures, 1);
            unsigned elapsed = (unsigned)((esp_timer_get_time() - started + 999) / 1000);
            unsigned prior = atomic_load(&io_peak_ms);
            if (elapsed > prior)
                atomic_store(&io_peak_ms, elapsed);
            /* Each accepted ticket reserved its result slot before enqueue. */
            if (job.ticket) {
                BaseType_t sent = xQueueSend(results, &reply, 0);
                configASSERT(sent == pdTRUE);
                if (sent != pdTRUE)
                    pp_objects_release(r);
            }
            taskYIELD();
        }
    }
}
static bool start(void) {
    if (worker_task)
        return true;
    results = xQueueCreate(QUEUE_SIZE, sizeof(reply_t));
    if (results && xTaskCreate(worker, "pp_cache", 4096, NULL, 1, &worker_task) == pdPASS)
        return true;
    if (results)
        vQueueDelete(results);
    results = NULL;
    return false;
}
void pp_objects_configure(const char *binding, const char *const owners[], unsigned count,
                          bool active) {
    unsigned char hash[32], next_pins[PP_CACHE_RECENT][16] = {0};
    digest(binding, strlen(binding), hash);
    bool changed = memcmp(scope, hash, sizeof(scope)) || enabled != active;
    memcpy(scope, hash, sizeof(scope));
    enabled = active;
    count = count > PP_CACHE_RECENT ? PP_CACHE_RECENT : count;
    for (unsigned i = 0; i < count; i++) {
        digest(owners[i], strlen(owners[i]), hash);
        memcpy(next_pins[i], hash, 16);
    }
    portENTER_CRITICAL(&lock);
    if (changed)
        atomic_fetch_add(&generation, 1);
    if (changed || pin_count != count || memcmp(next_pins, pins, sizeof(pins))) {
        atomic_fetch_add(&index_revision, 1);
        pp_resident_invalidate(&resident, NULL);
    }
    portEXIT_CRITICAL(&lock);
    pin_count = count;
    memcpy(pins, next_pins, sizeof(pins));
    if (changed)
        reclaim(PP_CACHE_RESIDENT_BYTES, PP_MEMORY_CRITICAL);
    /* Binding is called once at startup before the other tasks start allocating. */
    if (!initialized) {
        pp_memory_on_pressure(take_resident, reclaim);
        initialized = true;
    }
}
static job_t job_for(const char *key, pp_memory_priority_t priority) {
    job_t job = {.generation = atomic_load(&generation), .pin_count = pin_count,
                 .priority = priority, .revision = atomic_load(&index_revision)};
    memcpy(job.scope, scope, sizeof(scope));
    memcpy(job.pins, pins, sizeof(pins));
    mbedtls_sha256_context ctx;
    unsigned char hash[32];
    mbedtls_sha256_init(&ctx);
    mbedtls_sha256_starts(&ctx, 0);
    mbedtls_sha256_update(&ctx, scope, sizeof(scope));
    mbedtls_sha256_update(&ctx, (const unsigned char *)key, strlen(key));
    mbedtls_sha256_finish(&ctx, hash);
    mbedtls_sha256_free(&ctx);
    for (unsigned i = 0; i < 32; i++)
        snprintf(job.key + i * 2, 3, "%02x", hash[i]);
    return job;
}
static bool submit(const job_t *job) {
    if (job->ticket && outstanding == QUEUE_SIZE)
        return false;
    portENTER_CRITICAL(&lock);
    bool accepted = queued_count < QUEUE_SIZE;
    if (accepted)
        queued[queued_count++] = *job;
    portEXIT_CRITICAL(&lock);
    if (accepted) {
        if (job->ticket)
            outstanding++;
        xTaskNotifyGive(worker_task);
    }
    return accepted;
}
bool pp_objects_read(const char *key, unsigned ticket, pp_memory_priority_t priority) {
    if (!enabled || !ticket || outstanding == QUEUE_SIZE)
        return false;
    job_t job = job_for(key, priority);
    job.ticket = ticket;
    job.read = true;
    job.index = !strcmp(key, "recent");
    reply_t reply = {.generation = job.generation, .revision = job.revision, .index = job.index,
                     .result = {.ticket = ticket, .status = PP_CACHE_HIT}};
    portENTER_CRITICAL(&lock);
    bool hit = pp_resident_borrow(&resident, job.key, &reply.result);
    portEXIT_CRITICAL(&lock);
    if (hit) {
        if (xQueueSend(results, &reply, 0) == pdTRUE) {
            outstanding++;
            atomic_fetch_add(&ram_hits, 1);
            atomic_fetch_add(&hits, 1);
            return true;
        }
        pp_objects_release(&reply.result);
        return false;
    }
    return may_run(&job) && start() && submit(&job);
}
bool pp_objects_write(const char *key, const char *owner, bool index, pp_buffer_t *buffer,
                       const void *aux, size_t aux_size, unsigned ticket) {
    pp_buffer_t data = pp_buffer_take(buffer);
    job_t job = job_for(key, PP_MEMORY_BACKGROUND);
    job.index = index;
    if (!enabled || !data.data || data.size > data.capacity || aux_size > PP_CACHE_AUX_MAX ||
        (aux_size && !aux) || !may_run(&job) ||
        !start() || !charge(data.capacity)) {
        pp_buffer_free(&data);
        return false;
    }
    unsigned char hash[32];
    digest(owner, strlen(owner), hash);
    memcpy(job.owner, hash, sizeof(job.owner));
    job.ticket = ticket;
    pp_buffer_set_use(&data, PP_MEMORY_CACHE);
    job.value.bytes = data;
    job.value.aux_size = aux_size;
    if (aux_size)
        memcpy(job.value.aux, aux, aux_size);
    if (submit(&job))
        return true;
    atomic_fetch_sub(&in_flight, data.capacity);
    pp_buffer_free(&data);
    return false;
}
bool pp_objects_next(pp_cache_result_t *result) {
    reply_t reply;
    while (results && xQueueReceive(results, &reply, 0) == pdTRUE) {
        outstanding--;
        if (reply.generation == atomic_load(&generation)) {
            *result = reply.result;
            if (reply.index && reply.revision != atomic_load(&index_revision))
                result->status = PP_CACHE_BUSY;
            return true;
        }
        bool image_write = reply.result.written && (reply.result.ticket & 3u) == 3u;
        unsigned ticket = reply.result.ticket;
        pp_objects_release(&reply.result);
        if (image_write) {
            *result = (pp_cache_result_t){.ticket = ticket, .written = true, .status = PP_CACHE_BUSY};
            return true;
        }
    }
    return false;
}
void pp_objects_release(pp_cache_result_t *result) {
    if (result->lease) {
        portENTER_CRITICAL(&lock);
        pp_resident_release(&resident, result->lease);
        portEXIT_CRITICAL(&lock);
    } else
        pp_buffer_free(&result->value.bytes);
    if (result->charge)
        atomic_fetch_sub(&in_flight, result->charge);
    *result = (pp_cache_result_t){0};
}
bool pp_objects_retain(const pp_cache_result_t *result, pp_cache_result_t *retained) {
    *retained = (pp_cache_result_t){0};
    portENTER_CRITICAL(&lock);
    bool ok = pp_resident_retain(&resident, result->lease, retained);
    portEXIT_CRITICAL(&lock);
    return ok;
}
void pp_objects_allow_io(bool allowed) {
    bool was_allowed = atomic_exchange(&quiet, allowed);
    if (!allowed && was_allowed)
        reclaim(PP_CACHE_RESIDENT_BYTES, PP_MEMORY_CRITICAL);
}
void pp_objects_connecting(bool connecting) {
    atomic_store(&reconnecting, connecting);
    if (connecting)
        reclaim(PP_CACHE_RESIDENT_BYTES, PP_MEMORY_CRITICAL);
}
pp_cache_info_t pp_objects_info(void) {
    portENTER_CRITICAL(&lock);
    size_t recoverable = pp_resident_recoverable(&resident);
    portEXIT_CRITICAL(&lock);
    return (pp_cache_info_t){.ready = atomic_load(&storage_ready), .hits = atomic_load(&hits),
        .misses = atomic_load(&misses), .write_failures = atomic_load(&write_failures),
        .busy = atomic_load(&busy), .ram_hits = atomic_load(&ram_hits), .flash_hits = atomic_load(&flash_hits),
        .recoverable = recoverable, .in_flight = atomic_load(&in_flight),
        .io_peak_ms = atomic_load(&io_peak_ms)};
}

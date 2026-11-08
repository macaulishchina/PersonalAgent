#include "cache_disk.h"
#include "esp_littlefs.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_partition.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

typedef struct {
    unsigned ticket, generation, pin_count;
    bool read, index;
    char key[65];
    unsigned char scope[16], owner[16], pins[PP_CACHE_RECENT][16], aux[PP_CACHE_AUX_MAX];
    void *data;
    size_t size, aux_size;
} job_t;
static QueueHandle_t jobs, results;
static atomic_uint generation;
static atomic_bool write_allowed = true;
static atomic_bool storage_ready;
static atomic_uint hits, misses, write_failures;
static unsigned char scope[16], pins[PP_CACHE_RECENT][16];
static unsigned pin_count;
static bool enabled;

static void digest(const void *data, size_t size, unsigned char result[32]) {
    if (mbedtls_sha256(data, size, result, 0))
        memset(result, 0, 32);
}
bool pp_disk_identity(const char *url, const char *device, const char *binding, char identity[65]) {
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
static bool mount_store(pp_cache_store_t *store) {
    const esp_partition_t *partition = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_LITTLEFS, "pp_cache");
    bool ready = partition && partition->address == 0x780000 && partition->size == 512 * 1024;
    esp_vfs_littlefs_conf_t cfg = {
        .base_path = "/ppcache", .partition_label = "pp_cache", .format_if_mount_failed = false};
    if (ready && esp_vfs_littlefs_register(&cfg) != ESP_OK) {
        /* This exact disposable partition only. Never erase configuration/NVS,
         * app, external media or any execution evidence to recover a cache. */
        ready =
            esp_littlefs_format("pp_cache") == ESP_OK && esp_vfs_littlefs_register(&cfg) == ESP_OK;
    }
    ready = ready && pp_cache_store_open(store, "/ppcache", PP_CACHE_BUDGET);
    ESP_LOGI("pp_cache", "storage=%s budget=%u", ready ? "ready" : "unavailable", PP_CACHE_BUDGET);
    return ready;
}
static void worker(void *unused) {
    (void)unused;
    pp_cache_store_t store;
    bool attempted = false, ready = false;
    job_t job;
    while (xQueueReceive(jobs, &job, portMAX_DELAY)) {
        pp_disk_result_t reply = {
            .ticket = job.ticket, .generation = job.generation, .written = !job.read};
        if (!attempted && job.generation == atomic_load(&generation)) {
            attempted = true;
            ready = mount_store(&store);
            atomic_store(&storage_ready, ready);
        }
        if (ready && job.generation == atomic_load(&generation)) {
            pp_cache_store_scope(&store, job.scope, job.pins, job.pin_count);
            if (job.read) {
                size_t limit = PP_CACHE_VALUE_MAX;
                if ((job.ticket & 3u) == 3u) {
                    size_t available = heap_caps_get_free_size(MALLOC_CAP_8BIT);
                    limit = available > 20000 ? available - 20000 : 0;
                }
                reply.hit = pp_cache_store_get_limit(&store, job.key, limit, &reply.value);
            } else if (atomic_load(&write_allowed))
                reply.hit = pp_cache_store_put(&store, job.key, job.owner, job.index, job.data,
                                               job.size, job.aux, job.aux_size);
        }
        free(job.data);
        if (job.read)
            atomic_fetch_add(reply.hit ? &hits : &misses, 1);
        else if (!reply.hit)
            atomic_fetch_add(&write_failures, 1);
        if (job.ticket && job.generation == atomic_load(&generation)) {
            if (xQueueSend(results, &reply, pdMS_TO_TICKS(100)) != pdTRUE)
                pp_cache_value_free(&reply.value);
        } else
            pp_cache_value_free(&reply.value);
        taskYIELD();
    }
    vTaskDelete(NULL);
}
bool pp_disk_start(void) {
    jobs = xQueueCreate(2, sizeof(job_t));
    results = xQueueCreate(2, sizeof(pp_disk_result_t));
    if (jobs && results && xTaskCreate(worker, "pp_cache", 4096, NULL, 1, NULL) == pdPASS)
        return true;
    if (jobs)
        vQueueDelete(jobs);
    if (results)
        vQueueDelete(results);
    jobs = results = NULL;
    return false;
}
void pp_disk_configure(const char *binding, const char *const owners[], unsigned count,
                       bool active) {
    unsigned char hash[32];
    digest(binding, strlen(binding), hash);
    if (memcmp(scope, hash, sizeof(scope)) || enabled != active) {
        atomic_fetch_add(&generation, 1);
        pp_disk_result_t prior;
        while (results && xQueueReceive(results, &prior, 0))
            pp_cache_value_free(&prior.value);
    }
    memcpy(scope, hash, sizeof(scope));
    enabled = active;
    pin_count = count > PP_CACHE_RECENT ? PP_CACHE_RECENT : count;
    for (unsigned i = 0; i < pin_count; i++) {
        digest(owners[i], strlen(owners[i]), hash);
        memcpy(pins[i], hash, 16);
    }
}
static job_t job_for(const char *key) {
    job_t job = {.generation = atomic_load(&generation), .pin_count = pin_count};
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
bool pp_disk_read(const char *key, unsigned ticket) {
    if (!enabled || !jobs || !ticket)
        return false;
    job_t job = job_for(key);
    job.read = true;
    job.ticket = ticket;
    return xQueueSend(jobs, &job, 0) == pdTRUE;
}
static bool write_take(const char *key, const char *owner, bool index, void *data, size_t bytes,
                       const void *aux, size_t aux_size, unsigned ticket) {
    if (!enabled || !jobs || !atomic_load(&write_allowed) || !data || bytes > PP_CACHE_VALUE_MAX ||
        aux_size > PP_CACHE_AUX_MAX) {
        free(data);
        return false;
    }
    job_t job = job_for(key);
    unsigned char hash[32];
    digest(owner, strlen(owner), hash);
    memcpy(job.owner, hash, sizeof(job.owner));
    job.index = index;
    job.ticket = ticket;
    job.data = data;
    job.size = bytes;
    job.aux_size = aux_size;
    if (aux_size)
        memcpy(job.aux, aux, aux_size);
    if (xQueueSend(jobs, &job, 0) == pdTRUE)
        return true;
    free(data);
    return false;
}
bool pp_disk_write_take(const char *key, const char *owner, bool index, void *data, size_t bytes,
                        const void *aux, size_t aux_size) {
    return write_take(key, owner, index, data, bytes, aux, aux_size, 0);
}
bool pp_disk_write(const char *key, const char *owner, bool index, const void *data, size_t bytes) {
    return pp_disk_write_confirm(key, owner, index, data, bytes, 0);
}
bool pp_disk_write_confirm(const char *key, const char *owner, bool index, const void *data,
                           size_t bytes, unsigned ticket) {
    if (!atomic_load(&write_allowed) || bytes > PP_CACHE_VALUE_MAX)
        return false;
    void *copy = malloc(bytes);
    if (!copy)
        return false;
    memcpy(copy, data, bytes);
    return write_take(key, owner, index, copy, bytes, NULL, 0, ticket);
}
bool pp_disk_result(pp_disk_result_t *result) {
    while (results && xQueueReceive(results, result, 0) == pdTRUE) {
        if (result->generation == atomic_load(&generation))
            return true;
        pp_cache_value_free(&result->value);
    }
    return false;
}
bool pp_disk_enabled(void) { return enabled && jobs; }
void pp_disk_allow_writes(bool quiet) { atomic_store(&write_allowed, quiet); }
pp_disk_info_t pp_disk_info(void) {
    return (pp_disk_info_t){.ready = atomic_load(&storage_ready),
                            .hits = atomic_load(&hits),
                            .misses = atomic_load(&misses),
                            .write_failures = atomic_load(&write_failures)};
}

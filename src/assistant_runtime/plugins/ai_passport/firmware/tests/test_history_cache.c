#include "history_cache.h"
#include "network.h"
#include "pictures.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char key[256], scope[640];
    char *value;
} record_t;
static record_t records[128];
static char scope[640];
static bool online = true, enabled;
static bool fail_next_write;
static unsigned reads, requests, incoming_count;
typedef struct {
    unsigned ticket;
    bool written, success;
    char key[256];
} pending_t;
static pending_t pending[8];
static unsigned pending_count;
static cJSON *request, *incoming[32];
int64_t esp_timer_get_time(void) { return 1000000; }
bool pp_disk_start(void) { return true; }
bool pp_disk_identity(const char *url, const char *device, const char *binding, char identity[65]) {
    unsigned hash = 1;
    const char *parts[] = {url, device, binding};
    for (unsigned i = 0; i < 3; i++)
        for (unsigned j = 0; j <= strlen(parts[i]); j++)
            hash = hash * 31 + (unsigned char)parts[i][j];
    snprintf(identity, 65, "%064x", hash);
    return true;
}
void pp_disk_configure(const char *value, const char *const owners[], unsigned count, bool active) {
    (void)owners;
    assert(count <= PP_CACHE_RECENT);
    strcpy(scope, value);
    enabled = active;
}
bool pp_disk_enabled(void) { return enabled; }
bool pp_disk_read(const char *key, unsigned ticket) {
    assert(pending_count < 8);
    pending_t *job = &pending[pending_count++];
    *job = (pending_t){.ticket = ticket};
    strcpy(job->key, key);
    reads++;
    return true;
}
bool pp_disk_write(const char *key, const char *owner, bool index, const void *data, size_t bytes) {
    (void)owner;
    (void)index;
    assert(bytes <= PP_CACHE_VALUE_MAX);
    unsigned target = 128;
    for (unsigned i = 0; i < 128; i++) {
        if (!records[i].value && target == 128)
            target = i;
        if (records[i].value && !strcmp(records[i].key, key) && !strcmp(records[i].scope, scope)) {
            target = i;
            break;
        }
    }
    assert(target < 128);
    record_t *record = &records[target];
    free(record->value);
    record->value = malloc(bytes + 1);
    assert(record->value);
    memcpy(record->value, data, bytes);
    record->value[bytes] = 0;
    strcpy(record->key, key);
    strcpy(record->scope, scope);
    return true;
}
bool pp_disk_write_confirm(const char *key, const char *owner, bool index, const void *data,
                           size_t bytes, unsigned ticket) {
    bool ok = !fail_next_write && pp_disk_write(key, owner, index, data, bytes);
    fail_next_write = false;
    assert(pending_count < 8);
    pending[pending_count++] = (pending_t){.ticket = ticket, .written = true, .success = ok};
    return true;
}
bool pp_disk_result(pp_disk_result_t *result) {
    if (!pending_count)
        return false;
    pending_t job = pending[0];
    memmove(pending, pending + 1, --pending_count * sizeof(pending[0]));
    *result = (pp_disk_result_t){.ticket = job.ticket, .written = job.written};
    if (job.written) {
        result->hit = job.success;
        return true;
    }
    for (unsigned i = 0; i < 128; i++)
        if (records[i].value && !strcmp(records[i].key, job.key) &&
            !strcmp(records[i].scope, scope)) {
            size_t bytes = strlen(records[i].value);
            result->value.data = malloc(bytes + 1);
            memcpy(result->value.data, records[i].value, bytes + 1);
            result->value.size = bytes;
            result->hit = true;
            break;
        }
    return true;
}
void pp_cache_value_free(pp_cache_value_t *value) {
    free(value->data);
    memset(value, 0, sizeof(*value));
}
bool pp_net_connected(void) { return online; }
bool pp_net_send(cJSON *command) {
    if (!online) {
        cJSON_Delete(command);
        return false;
    }
    cJSON_Delete(request);
    request = command;
    requests++;
    return true;
}
void pp_picture_cached(pp_disk_result_t *result) {
    (void)result;
    assert(false);
}
bool pp_picture_prefetch(const char *message, const char *image) {
    (void)message;
    (void)image;
    return false;
}
static void receive(const char *text, size_t bytes) {
    (void)bytes;
    assert(incoming_count < 32);
    incoming[incoming_count++] = cJSON_Parse(text);
    assert(incoming[incoming_count - 1]);
}
static const char *text(const cJSON *j, const char *key) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}
static void catalog(unsigned revision) {
    char raw[1024];
    snprintf(
        raw, sizeof(raw),
        "{\"type\":\"cache.index\",\"items\":["
        "{\"id\":\"one\",\"cursor\":\"a\",\"role\":\"user\",\"status\":\"complete\",\"updated\":1},"
        "{\"id\":\"two\",\"cursor\":\"b\",\"role\":\"assistant\",\"status\":\"complete\","
        "\"updated\":1},"
        "{\"id\":\"three\",\"cursor\":\"c\",\"role\":\"user\",\"status\":\"complete\",\"updated\":%"
        "u}]}",
        revision);
    cJSON *j = cJSON_Parse(raw);
    assert(j && pp_history_cache_receive(j));
    cJSON_Delete(j);
}
static void network_page(const char *id, unsigned revision, bool prefetch) {
    cJSON *page = cJSON_CreateObject();
    cJSON_AddStringToObject(page, "type", "message.page");
    cJSON_AddStringToObject(page, "message_id", id);
    cJSON_AddStringToObject(page, "text", "完整正文");
    cJSON_AddStringToObject(page, "role", "user");
    cJSON_AddStringToObject(page, "status", "complete");
    cJSON_AddNumberToObject(page, "updated", revision);
    cJSON_AddNumberToObject(page, "offset", 0);
    cJSON_AddNumberToObject(page, "total", 4);
    cJSON_AddNullToObject(page, "next_offset");
    if (prefetch)
        cJSON_AddBoolToObject(page, "prefetch", true);
    assert(pp_history_cache_receive(page) == prefetch);
    cJSON_Delete(page);
}
int main(void) {
    pp_history_cache_init(receive);
    pp_history_cache_bind("http://pa:8047", "device", "session", true, true);
    pp_history_cache_poll(false); /* Empty disk is not presented while online. */
    catalog(1);
    const char *ids[] = {"one", "two", "three"};
    for (unsigned i = 0; i < 3; i++) {
        pp_history_cache_poll(true);
        pp_history_cache_poll(false);
        assert(request && !strcmp(text(request, "message_id"), ids[i]));
        assert(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(request, "prefetch")));
        network_page(ids[i], 1, true);
    }
    for (unsigned i = 0; i < 5; i++)
        pp_history_cache_poll(true);
    assert(requests == 3);
    unsigned before = requests;
    /* Browsing unrelated old history does not replace the recent directory. */
    assert(pp_history_cache_page("very-old", 0));
    assert(requests == before + 1 && !strcmp(text(request, "message_id"), "very-old"));
    network_page("very-old", 1, false);
    assert(pp_history_cache_page("three", 0));
    pp_history_cache_poll(false);
    cJSON *hit = incoming[incoming_count - 1];
    assert(!strcmp(text(hit, "type"), "message.page"));
    assert(cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(hit, "cached")));
    assert(!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(hit, "prefetch")));
    assert(!pp_history_cache_receive(hit));
    assert(requests == before + 1);
    online = false;
    assert(pp_history_cache_recent());
    pp_history_cache_poll(false);
    assert(!strcmp(text(incoming[incoming_count - 1], "type"), "cache.restored"));
    for (unsigned i = 0; i < 3; i++) {
        assert(pp_history_cache_page(ids[i], 0));
        pp_history_cache_poll(false);
        hit = incoming[incoming_count - 1];
        assert(!strcmp(text(hit, "message_id"), ids[i]));
        assert(!strcmp(text(hit, "text"), "完整正文"));
        assert(!pp_history_cache_receive(hit));
    }
    assert(requests == before + 1); /* Offline navigation performs no network call. */
    online = true;
    catalog(2);
    assert(pp_history_cache_page("three", 0));
    pp_history_cache_poll(false);
    assert(requests == before + 2); /* Old revision cannot satisfy a new one. */
    network_page("three", 2, false);
    /* A new instance/session cannot expose the previous device's cached body. */
    pp_history_cache_bind("http://other:8047", "device", "another-session", true, true);
    online = false;
    pp_history_cache_poll(false);
    assert(!strcmp(text(incoming[incoming_count - 1], "type"), "cache.empty"));
    /* A failed page commit must not publish an index claiming that message is
     * cached. A network receipt alone is insufficient. */
    online = true;
    catalog(1);
    pp_history_cache_poll(true);
    pp_history_cache_poll(false);
    fail_next_write = true;
    network_page("one", 1, true);
    pp_history_cache_poll(true);
    unsigned after_failure = requests;
    for (unsigned i = 0; i < 5; i++)
        pp_history_cache_poll(true);
    assert(requests == after_failure); /* No invisible read/model retry loop. */
    online = false;
    assert(pp_history_cache_recent());
    pp_history_cache_poll(false);
    assert(!strcmp(text(incoming[incoming_count - 1], "type"), "cache.empty"));
    assert(reads > 3);
    cJSON_Delete(request);
    for (unsigned i = 0; i < incoming_count; i++)
        cJSON_Delete(incoming[i]);
    for (unsigned i = 0; i < 128; i++)
        free(records[i].value);
    return 0;
}

#define _POSIX_C_SOURCE 200809L
#include "history_cache.h"
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
static bool enabled;
static bool fail_next_write;

typedef struct {
    unsigned ticket;
    bool written, success;
    char key[256];
} pending_t;
static pending_t pending[8];
static unsigned pending_count;

int64_t esp_timer_get_time(void) {
    static int64_t t;
    return t += 200000;
}
bool pp_objects_identity(const char *const parts[], unsigned count, char identity[65]) {
    unsigned hash = 1;
    assert(count == 3);
    for (unsigned i = 0; i < 3; i++)
        for (unsigned j = 0; j <= strlen(parts[i]); j++)
            hash = hash * 31 + (unsigned char)parts[i][j];
    snprintf(identity, 65, "%064x", hash);
    return true;
}
void pp_objects_configure(const char *value, bool active) {
    strcpy(scope, value);
    enabled = active;
}
bool pp_objects_read(const char *key, unsigned ticket, pp_object_read_t options) {
    assert(options.heap_reserve);
    assert(pending_count < 8);
    pending_t *job = &pending[pending_count++];
    *job = (pending_t){.ticket = ticket};
    strcpy(job->key, key);
    return true;
}
static bool save(const char *key, const char *owner, bool index, const void *data, size_t bytes) {
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
bool pp_objects_try_write(const char *key, const char *owner, pp_object_write_t options,
                          pp_buffer_t *buffer, const void *aux, size_t aux_size, unsigned ticket) {
    (void)aux; (void)aux_size;
    if (fail_next_write) {
        fail_next_write = false;
        return false;
    }
    bool ok = save(key, owner, options.kind, buffer->data, buffer->size);
    pp_buffer_free(buffer);
    fail_next_write = false;
    assert(pending_count < 8);
    if (ticket)
        pending[pending_count++] = (pending_t){.ticket = ticket, .written = true, .success = ok};
    return true;
}
static pp_cache_status_t fail_next_read;
bool pp_objects_next(pp_cache_result_t *result) {
    if (!pending_count)
        return false;
    pending_t job = pending[0];
    memmove(pending, pending + 1, --pending_count * sizeof(*pending));
    *result = (pp_cache_result_t){.ticket = job.ticket, .written = job.written,
                                   .status = PP_CACHE_MISSING};
    if (job.written) {
        result->status = job.success ? PP_CACHE_HIT : PP_CACHE_BUSY;
        return true;
    }
    if (fail_next_read) {
        result->status = fail_next_read;
        fail_next_read = PP_CACHE_HIT;
        return true;
    }
    for (unsigned i = 0; i < 128; i++)
        if (records[i].value && !strcmp(records[i].key, job.key) &&
            !strcmp(records[i].scope, scope)) {
            size_t bytes = strlen(records[i].value);
            assert(pp_buffer_reserve(&result->value.bytes, bytes, PP_MEMORY_NORMAL, PP_MEMORY_CACHE));
            memcpy(result->value.bytes.data, records[i].value, bytes);
            result->value.bytes.size = bytes;
            result->status = PP_CACHE_HIT;
            break;
        }
    return true;
}
void pp_objects_release(pp_cache_result_t *result) { pp_buffer_free(&result->value.bytes); }
void pp_picture_cached(pp_cache_result_t *r) {
    (void)r;
    assert(false);
}
static pp_history_page_t page;
static pp_cache_status_t status;
static void receive(pp_cache_status_t result, const pp_history_page_t *record) {
    status = result;
    static char text[256];
    if (record) {
        page = *record;
        snprintf(text, sizeof(text), "%s", record->text);
        page.text = text;
    }
}
static void warning(const char *why) {
    (void)why;
    assert(false);
}
static void record(unsigned i) {
    char raw[256];
    snprintf(raw, sizeof(raw),
             "{\"message_id\":\"msg-%u\",\"group\":\"turn\",\"status\":\"complete\","
             "\"role\":\"user\",\"offset\":0,\"updated\":1,\"text\":\"正文%u\"}",
             i, i);
    pp_buffer_t frame = pp_buffer_adopt(strdup(raw), strlen(raw), strlen(raw) + 1, PP_MEMORY_CACHE);
    cJSON *j = cJSON_Parse(raw);
    pp_history_page_t parsed;
    assert(pp_timeline_decode(&parsed, j));
    pp_history_cache_record(&parsed, &frame);
    assert(!frame.data);
    cJSON_Delete(j);
}
int main(void) {
    pp_history_cache_init(receive, warning);
    pp_history_cache_bind("url", "dev", "binding", true, true);
    assert(enabled);
    const char *dir = "{\"items\":[{\"id\":\"old\",\"updated\":2}]}";
    assert(save("recent", "", true, dir, strlen(dir)));
    for (unsigned i = 0; i < 3; i++) {
        char body[200], key[40];
        snprintf(key, sizeof(key), "page/old/2/%u", i * 80);
        snprintf(body, sizeof(body),
                 "{\"message_id\":\"old\",\"updated\":2,\"status\":\"complete\","
                 "\"offset\":%u,\"text\":\"part-%u\"}",
                 i * 80, i);
        assert(save(key, "old/2", false, body, strlen(body)));
    }
    for (unsigned i = 0; i < 20; i++)
        pp_history_cache_poll(true);
    assert(pp_history_cache_read("old", 2, false, 0, PP_SEEK_EXACT));
    pp_history_cache_poll(false);
    for (unsigned i = 1; i <= 2; i++) {
        assert(status == PP_CACHE_HIT && page.legacy && !strcmp(page.group, "old/2"));
        assert(pp_history_cache_read(page.id, page.revision, page.legacy, i * 80, PP_SEEK_EXACT));
        pp_history_cache_poll(false);
    }
    assert(status == PP_CACHE_HIT && page.legacy && !strcmp(page.text, "part-2"));
    assert(pp_history_cache_read(page.id, page.revision, true, 163, PP_SEEK_EXACT));
    pp_history_cache_poll(false);
    assert(status == PP_CACHE_HIT && page.offset == 163 && !strcmp(page.text, "t-2"));
    for (unsigned i = 0; i < 7; i++)
        record(i);
    pp_history_cache_poll(false);
    assert(!pending_count);
    fail_next_write = true;
    for (int i = 0; i < 22; i++)
        pp_history_cache_poll(true);
    for (unsigned i = 0; i < 7; i++) {
        char id[32], body[32];
        snprintf(id, sizeof(id), "msg-%u", i);
        snprintf(body, sizeof(body), "正文%u", i);
        assert(pp_history_cache_read(id, 1, false, 0, PP_SEEK_EXACT));
        pp_history_cache_poll(false);
        assert(status == PP_CACHE_HIT && !strcmp(page.text, body) && page.user &&
               !strcmp(page.group, "turn"));
    }
    fail_next_read = PP_CACHE_BUSY;
    assert(pp_history_cache_read("msg-2", 1, false, 0, PP_SEEK_EXACT));
    for (unsigned i = 0; i < 5; i++)
        pp_history_cache_poll(false);
    assert(status == PP_CACHE_HIT && !strcmp(page.id, "msg-2"));
    pp_history_cache_bind("url", "dev", "another", true, true);
    assert(pp_history_cache_read("msg-1", 1, false, 0, PP_SEEK_EXACT));
    pp_history_cache_poll(false);
    assert(status == PP_CACHE_MISSING);
    pp_history_cache_bind("url", "dev", "binding", false, true);
    assert(!pp_history_cache_read("msg-1", 1, false, 0, PP_SEEK_EXACT));
    for (unsigned i = 0; i < 128; i++)
        free(records[i].value);
    return 0;
}

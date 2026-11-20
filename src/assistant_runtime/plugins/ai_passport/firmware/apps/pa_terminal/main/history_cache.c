#include "history_cache.h"
#include "pictures.h"
#include "terminal_limits.h"
#include "esp_timer.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Only a live frame awaiting its flash receipt occupies the write queue. */
typedef struct pending {
    struct pending *next;
    pp_buffer_t bytes;
    const char *group;
    pp_object_kind_t kind;
    unsigned ticket;
    size_t capacity;
    uint64_t import_order;
    size_t aux_size;
    char key[];
} pending_t;
static pending_t *first, *last;
static size_t queued_bytes;
static unsigned serial, read_ticket, read_offset;
static uint32_t retry_at;
static struct {
    char key[256];
    pp_object_read_t policy;
    unsigned attempts;
    uint32_t retry_at;
    bool waiting;
} reading;
static uint64_t unreadable_cursor;

static char scope[65];
static bool enabled, images, read_legacy;
static pp_history_page_fn page_read;
static void (*warn)(const char *);
static uint32_t now(void) { return (uint32_t)(esp_timer_get_time() / 1000); }
static unsigned ticket(unsigned tag) { return (++serial << 2) | tag; }
static void issue_read(void) {
    reading.attempts++;
    unsigned next = ticket(1);
    reading.waiting = !pp_objects_read(reading.key, next, reading.policy);
    read_ticket = reading.waiting ? 0 : next;
    reading.retry_at = now() + 120;
}
static void pop(void) {
    pending_t *old = first;
    first = old->next;
    if (!first)
        last = NULL;
    queued_bytes -= old->capacity;
    pp_buffer_free(&old->bytes);
    free(old);
}
void pp_history_cache_init(pp_history_page_fn callback, void (*warning)(const char *)) {
    page_read = callback;
    warn = warning;
}
static void body_key(char key[256], const char *id, double revision, unsigned block) {
    if (!block)
        snprintf(key, 256, "record/%s", id);
    else
        snprintf(key, 256, "page/%s/%.17g/%u", id, revision, block);
}
bool pp_history_cache_image_key(const char *id, double revision, const char *image, char key[256]) {
    if (!enabled || !images || !*id || strlen(image) != 32)
        return false;
    snprintf(key, 256, "image/%s/%.17g/%s", id, revision, image);
    return true;
}
static bool enqueue(const char *key, const char *group, pp_object_kind_t kind, pp_buffer_t *frame,
                    const void *aux, size_t aux_size) {
    size_t key_size = strlen(key) + 1;
    size_t metadata = sizeof(pending_t) + key_size + strlen(group) + 1 + aux_size;
    size_t needed = metadata + frame->capacity;
    if (needed > PP_CACHE_VALUE_MAX - queued_bytes)
        return false;
    pending_t *p = calloc(1, metadata);
    if (!p)
        return false;
    strcpy(p->key, key);
    p->group = strcpy(p->key + key_size, group);
    p->aux_size = aux_size;
    if (aux_size)
        memcpy((char *)p->group + strlen(group) + 1, aux, aux_size);
    p->capacity = needed;
    p->bytes = pp_buffer_take(frame);
    p->kind = kind;
    if (last)
        last->next = p;
    else
        first = p;
    last = p;
    queued_bytes += p->capacity;
    return true;
}
bool pp_history_cache_preview(const char *key, const char *group, pp_buffer_t *pixels,
                              const pp_image_transfer_t *meta) {
    return enqueue(key, group, PP_HISTORY_PART, pixels, meta, sizeof(*meta));
}
void pp_history_cache_record(const pp_history_page_t *page, pp_buffer_t *frame) {
    if (!enabled || !page->complete || !frame->data)
        return;
    char key[256];
    body_key(key, page->id, page->revision, page->offset);
    if (!enqueue(key, page->group, page->offset ? PP_HISTORY_PART : PP_HISTORY_HEAD, frame, NULL, 0))
        warn("本地历史写入忙，本段未保存");
}
bool pp_history_cache_read(const char *id, double revision, bool legacy, unsigned offset,
                           pp_cache_seek_t seek) {
    if (!enabled)
        return false;
    unsigned stride = legacy ? PP_LEGACY_HISTORY_BLOCK_CHARS : PP_HISTORY_BLOCK_CHARS;
    unsigned block = seek == PP_SEEK_EXACT ? offset / stride * stride : 0;
    if (seek == PP_SEEK_LATEST || seek == PP_SEEK_EXACT)
        unreadable_cursor = 0;
    body_key(reading.key, id, revision, block);
    reading.policy = (pp_object_read_t){.priority = PP_MEMORY_NORMAL, .heap_reserve = PP_PLAYBACK_WINDOW,
                                        .seek = seek, .cursor = unreadable_cursor};
    reading.attempts = 0;
    issue_read();
    read_offset = offset;
    read_legacy = legacy && seek == PP_SEEK_EXACT;
    return true;
}

/* Local, one-time adoption of the last directory written by 0.2.1. Page
 * tails and previews keep their existing revision keys. No remote dependency. */
static cJSON *legacy_items;
static unsigned legacy_pos, legacy_ticket;
static bool import_needed;
static void import_poll(void) {
    if (!import_needed || legacy_ticket || first)
        return;
    char key[256] = "recent";
    if (legacy_items) {
        const cJSON *entry = cJSON_GetArrayItem(legacy_items, legacy_pos);
        if (!entry) {
            char *raw = strdup("{}");
            if (!raw)
                return;
            pp_buffer_t marker = pp_buffer_adopt(raw, 2, 3, PP_MEMORY_CACHE);
            if (enqueue("recent", "", PP_HISTORY_PART, &marker, NULL, 0))
                import_needed = false;
            pp_buffer_free(&marker);
            cJSON_Delete(legacy_items);
            legacy_items = NULL;
            return;
        }
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(entry, "id");
        const cJSON *rev = cJSON_GetObjectItemCaseSensitive(entry, "updated");
        if (!cJSON_IsString(id) || strlen(id->valuestring) >= PP_MESSAGE_ID || !cJSON_IsNumber(rev)) {
            legacy_pos++;
            return;
        }
        snprintf(key, sizeof(key), "page/%s/%.17g/0", id->valuestring, rev->valuedouble);
    }
    unsigned t = ticket(2);
    if (pp_objects_read(
            key, t, (pp_object_read_t){.priority = PP_MEMORY_NORMAL, .heap_reserve = PP_PLAYBACK_WINDOW}))
        legacy_ticket = t;
}
static void import_result(pp_cache_result_t *r) {
    legacy_ticket = 0;
    if (r->status == PP_CACHE_MISSING || r->status == PP_CACHE_CORRUPT) {
        if (legacy_items)
            legacy_pos++;
        else
            import_needed = false;
        return;
    }
    if (r->status != PP_CACHE_HIT) {
        retry_at = now() + 500;
        return;
    }
    cJSON *j = cJSON_ParseWithLength(r->value.bytes.data, r->value.bytes.size);
    if (!legacy_items) {
        legacy_items = cJSON_DetachItemFromObjectCaseSensitive(j, "items");
        if (!cJSON_IsArray(legacy_items) || cJSON_GetArraySize(legacy_items) > 3) {
            cJSON_Delete(legacy_items);
            legacy_items = NULL;
            import_needed = false;
        }
    } else {
        pp_history_page_t page;
        cJSON_AddBoolToObject(j, "legacy", true);
        if (pp_timeline_decode(&page, j) && page.complete && !page.offset) {
            char key[256];
            body_key(key, page.id, page.revision, 0);
            char *raw = cJSON_PrintUnformatted(j);
            if (!raw) {
                cJSON_Delete(j);
                return;
            }
            size_t n = strlen(raw);
            pp_buffer_t data = pp_buffer_adopt(raw, n, n + 1, PP_MEMORY_CACHE);
            bool ok = enqueue(key, page.group, PP_HISTORY_HEAD, &data, NULL, 0);
            if (ok)
                last->import_order = r->value.sequence;
            pp_buffer_free(&data);
            if (!ok) {
                cJSON_Delete(j);
                return;
            }
        }
        legacy_pos++;
    }
    cJSON_Delete(j);
}
void pp_history_cache_bind(const char *url, const char *device, const char *binding, bool active,
                           bool show_images) {
    char next[65];
    const char *parts[] = {url, device, binding};
    active = pp_objects_identity(parts, 3, next) && active && *binding;
    images = show_images;
    if (enabled == active && !strcmp(scope, next))
        return;
    while (first)
        pop();
    reading.waiting = false;
    unreadable_cursor = 0;
    read_ticket = legacy_ticket = legacy_pos = retry_at = 0;
    cJSON_Delete(legacy_items);
    legacy_items = NULL;
    import_needed = enabled = active;
    strcpy(scope, next);
    pp_objects_configure(scope, enabled);
}
void pp_history_cache_poll(bool writable) {
    pp_cache_result_t r;
    while (pp_objects_next(&r)) {
        if ((r.ticket & 3u) == 3u)
            pp_picture_cached(&r);
        else if (r.ticket == legacy_ticket && legacy_ticket)
            import_result(&r);
        else if (first && first->ticket && first->ticket == r.ticket && r.written) {
            if ((r.status == PP_CACHE_BUSY || r.status == PP_CACHE_NO_MEMORY) && r.value.bytes.data) {
                first->bytes = pp_buffer_take(&r.value.bytes);
                first->ticket = 0;
                retry_at = now() + 100;
            } else {
                if (r.status != PP_CACHE_HIT)
                    warn(first->aux_size ? "此图预览未保存" : "本地历史保存失败");
                pop();
            }
        } else if (r.ticket == read_ticket && read_ticket) {
            read_ticket = 0;
            pp_history_page_t page;
            cJSON *j = r.status == PP_CACHE_HIT
                           ? cJSON_ParseWithLength(r.value.bytes.data, r.value.bytes.size)
                           : NULL;
            if (j && read_legacy && !cJSON_HasObjectItem(j, "legacy"))
                cJSON_AddBoolToObject(j, "legacy", true);
            pp_history_page_t *decoded = j ? pp_timeline_window(&page, j, read_offset) : NULL;
            pp_cache_status_t result = r.status;
            if (result == PP_CACHE_HIT && !decoded) {
                printf("pp_history: unreadable %s window offset=%u stored_bytes=%zu legacy=%u\n",
                       j ? "record" : "json", read_offset, r.value.bytes.size, (unsigned)read_legacy);
                result = PP_CACHE_CORRUPT;
            }
            if ((result == PP_CACHE_BUSY || result == PP_CACHE_NO_MEMORY) && reading.attempts < 4) {
                reading.waiting = true;
                reading.retry_at = now() + 120;
            } else {
                if (result == PP_CACHE_HIT)
                    unreadable_cursor = 0;
                else if ((result == PP_CACHE_CORRUPT || result == PP_CACHE_IO_ERROR) && r.cursor)
                    unreadable_cursor = r.cursor;
                page_read(result, decoded);
            }
            cJSON_Delete(j);
        }
        pp_objects_release(&r);
    }
    if (reading.waiting && (int32_t)(now() - reading.retry_at) >= 0) {
        if (reading.attempts < 4)
            issue_read();
        else {
            reading.waiting = false;
            page_read(PP_CACHE_BUSY, NULL);
        }
    }
    if (!enabled || !writable || (retry_at && (int32_t)(now() - retry_at) < 0))
        return;
    retry_at = 0;
    if (first && !first->ticket) {
        unsigned t = ticket(0);
        if (pp_objects_try_write(
                first->key, first->group,
                (pp_object_write_t){.kind = first->kind, .import_order = first->import_order},
                &first->bytes, first->group + strlen(first->group) + 1, first->aux_size, t))
            first->ticket = t;
        else
            retry_at = now() + 100;
    } else
        import_poll();
}

#include "terminal_limits.h"
#include "history_cache.h"
#include "network.h"
#include "pictures.h"
#include "timeline.h"
#include "esp_timer.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    char id[160];
    double revision;
} entry_t;
typedef struct {
    char id[160];
    double revision;
    unsigned offset, ticket;
    uint32_t started;
    bool active;
} read_t;
static const pp_object_write_t page_policy = {.disk_reserve = 32u * 1024u};
static entry_t recent[PP_CACHE_PIN_LIMIT];
static unsigned count, serial, warm_index, warm_offset, warm_pages;
static unsigned warm_offsets[PP_CACHE_PIN_LIMIT];
static bool warm_done[PP_CACHE_PIN_LIMIT];
static char *pending_catalog;
static unsigned pending_next;
static char pending_image[33];
static uint32_t retry_after;
static unsigned index_write_ticket, index_write_failures;
static unsigned recent_ticket;
static bool enabled, warming, images_enabled;
static bool catalog_changed;
static char warm_images[PP_CACHE_PIN_LIMIT][33];
static unsigned warm_image;
static char scope[65];
static read_t foreground, background;
static void (*receive)(const char *, size_t);
static uint32_t now(void) { return (uint32_t)(esp_timer_get_time() / 1000); }
static const char *text(const cJSON *j, const char *key) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}
static void deliver(cJSON *j) {
    char *raw = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    if (raw) {
        receive(raw, strlen(raw));
        free(raw);
    }
}
static void configure(void) {
    const char *owners[PP_CACHE_PIN_LIMIT];
    char values[PP_CACHE_PIN_LIMIT][192];
    for (unsigned i = 0; i < count; i++) {
        snprintf(values[i], sizeof(values[i]), "%s/%.17g", recent[i].id, recent[i].revision);
        owners[i] = values[i];
    }
    pp_objects_configure(scope, owners, count, enabled);
}
void pp_history_cache_init(void (*callback)(const char *, size_t)) {
    receive = callback;
}
void pp_history_cache_bind(const char *url, const char *device, const char *binding, bool active,
                           bool images) {
    char next[sizeof(scope)];
    if (images && !images_enabled && count) {
        warm_image = 0;
        warming = true;
    }
    images_enabled = images;
    const char *parts[] = {url, device, binding};
    bool valid = pp_objects_identity(parts, 3, next);
    active = active && binding[0] && valid;
    if (strcmp(scope, next) || enabled != active) {
        count = 0;
        memset(recent, 0, sizeof(recent));
        foreground.active = background.active = false;
        warming = false;
        free(pending_catalog);
        pending_catalog = NULL;
        index_write_ticket = 0;
    }
    strcpy(scope, next);
    enabled = active;
    configure();
    if (enabled && !count)
        (void)pp_history_cache_recent();
}
bool pp_history_cache_revision(const char *id, double *revision) {
    for (unsigned i = 0; i < count; i++)
        if (!strcmp(id, recent[i].id)) {
            *revision = recent[i].revision;
            return true;
        }
    return false;
}
static void page_key(char key[256], const char *id, double revision, unsigned offset) {
    snprintf(key, 256, "page/%s/%.17g/%u", id, revision, offset);
}
bool pp_history_cache_image_key(const char *message, const char *image, char key[256]) {
    double revision;
    if (!enabled || !images_enabled || !pp_history_cache_revision(message, &revision) ||
        strlen(image) != 32)
        return false;
    snprintf(key, 256, "image/%s/%.17g/%s", message, revision, image);
    return true;
}
bool pp_history_cache_owner(const char *message, char owner[192]) {
    double revision;
    if (!enabled || !pp_history_cache_revision(message, &revision))
        return false;
    snprintf(owner, 192, "%s/%.17g", message, revision);
    return true;
}
static bool remote(const read_t *read, bool prefetch) {
    if (!pp_net_connected())
        return false;
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "type", "message.page");
    cJSON_AddStringToObject(j, "message_id", read->id);
    cJSON_AddNumberToObject(j, "offset", read->offset);
    if (prefetch)
        cJSON_AddBoolToObject(j, "prefetch", true);
    return pp_net_send(j);
}
static void unavailable(const read_t *read, bool busy) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "type", busy ? "cache.busy" : "cache.unavailable");
    cJSON_AddStringToObject(j, "message_id", read->id);
    cJSON_AddNumberToObject(j, "offset", read->offset);
    deliver(j);
}
static bool begin(read_t *read, const char *id, unsigned offset, bool prefetch) {
    if (!id || strlen(id) >= sizeof(read->id))
        return false;
    *read = (read_t){
        .offset = offset, .ticket = (++serial << 2) | 1u, .started = now(), .active = true};
    strcpy(read->id, id);
    char key[256];
    if (enabled && pp_history_cache_revision(id, &read->revision)) {
        page_key(key, id, read->revision, offset);
        pp_object_read_t policy = {.priority = prefetch ? PP_MEMORY_BACKGROUND : PP_MEMORY_NORMAL,
                                   .heap_reserve = PP_PLAYBACK_WINDOW};
        if (pp_objects_read(key, read->ticket, policy))
            return true;
        read->active = false;
        return false; /* Keep foreground data and retry the same background cursor. */
    }
    if (remote(read, prefetch))
        return true;
    read->active = false;
    if (!prefetch)
        unavailable(read, false);
    return true;
}
bool pp_history_cache_page(const char *id, unsigned offset) {
    return begin(&foreground, id, offset, false);
}
bool pp_history_cache_recent(void) {
    unsigned ticket = (++serial << 2) | 2u;
    pp_object_read_t policy = {.priority = PP_MEMORY_NORMAL, .heap_reserve = PP_PLAYBACK_WINDOW, .index = true};
    if (!enabled || !pp_objects_read("recent", ticket, policy))
        return false;
    recent_ticket = ticket;
    return true;
}
static bool catalog(const cJSON *j) {
    const cJSON *items = cJSON_GetObjectItemCaseSensitive(j, "items"), *item;
    if (!cJSON_IsArray(items) || cJSON_GetArraySize(items) > (int)PP_CACHE_PIN_LIMIT)
        return false;
    entry_t next[PP_CACHE_PIN_LIMIT] = {0};
    unsigned size = 0;
    cJSON_ArrayForEach(item, items) {
        const char *id = text(item, "id"), *role = text(item, "role"),
                   *status = text(item, "status");
        const cJSON *revision = cJSON_GetObjectItemCaseSensitive(item, "updated");
        if (!id[0] || strlen(id) >= sizeof(next[0].id) || !text(item, "cursor")[0] ||
            strlen(text(item, "cursor")) >= PP_HISTORY_CURSOR ||
            (strcmp(role, "user") && strcmp(role, "assistant")) || !cJSON_IsNumber(revision) ||
            !isfinite(revision->valuedouble) || revision->valuedouble < 0 || !status[0] ||
            !strcmp(status, "streaming"))
            return false;
        for (unsigned i = 0; i < size; i++)
            if (!strcmp(next[i].id, id))
                return false;
        strcpy(next[size].id, id);
        next[size++].revision = revision->valuedouble;
    }
    bool changed = size != count || memcmp(next, recent, sizeof(next));
    catalog_changed = changed;
    memcpy(recent, next, sizeof(next));
    count = size;
    configure();
    if (changed) {
        background.active = false;
        index_write_ticket = index_write_failures = 0;
        warm_index = warm_offset = warm_pages = 0;
        warming = true;
        memset(warm_offsets, 0, sizeof(warm_offsets));
        memset(warm_done, 0, sizeof(warm_done));
        warm_image = 0;
        memset(warm_images, 0, sizeof(warm_images));
    }
    return true;
}
static void warm_metadata(const cJSON *j) {
    const cJSON *next = cJSON_GetObjectItemCaseSensitive(j, "next_offset");
    pending_next = cJSON_IsNumber(next) && next->valuedouble > warm_offset &&
                   next->valuedouble <= UINT32_MAX ? (unsigned)next->valuedouble : 0;
    pending_image[0] = 0;
    if (!warm_offset) {
        const char *image = text(cJSON_GetArrayItem(cJSON_GetObjectItemCaseSensitive(j, "images"), 0), "id");
        if (strlen(image) == 32)
            strcpy(pending_image, image);
    }
}
static void warm_complete(void) {
    background.active = false;
    if (pending_image[0])
        strcpy(warm_images[warm_index], pending_image);
    warm_offsets[warm_index] = pending_next;
    warm_done[warm_index] = !pending_next;
    warm_pages++;
    warm_index = (warm_index + 1) % count;
}
static void warm_retry(void) {
    background.active = false;
    retry_after = now() + 500;
}
static pp_buffer_t payload(cJSON *j, pp_buffer_t *frame) {
    if (frame && frame->data)
        return pp_buffer_take(frame);
    char *raw = cJSON_PrintUnformatted(j);
    size_t size = raw ? strlen(raw) : 0;
    return pp_buffer_adopt(raw, size, size + 1, PP_MEMORY_CACHE);
}
bool pp_history_cache_receive(cJSON *j, pp_buffer_t *frame) {
    const char *type = text(j, "type");
    if (!strcmp(type, "cache.index")) {
        if (!enabled || !catalog(j))
            return true;
        if (!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "cached"))) {
            recent_ticket = 0; /* A delayed disk index cannot replace newer server metadata. */
            if (catalog_changed) {
                free(pending_catalog);
                pending_catalog = cJSON_PrintUnformatted(j);
            }
        }
        return true;
    }
    if (!strcmp(type, "cache.prefetch_unavailable")) {
        if (!strcmp(text(j, "message_id"), background.id)) {
            background.active = false;
            warming = false;
        }
        return true;
    }
    if (strcmp(type, "message.page") && strcmp(type, "message"))
        return false;
    bool prefetch = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "prefetch"));
    if (prefetch) {
        pp_history_page_t page;
        if (!background.active || strcmp(text(j, "message_id"), background.id) ||
            !pp_timeline_decode(&page, j) || !page.complete || page.offset != background.offset ||
            page.revision != background.revision)
            return true;
        warm_metadata(j);
        if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "cached")))
            warm_complete();
        else {
            char key[256], owner[192];
            page_key(key, page.id, page.revision, page.offset);
            if (pp_history_cache_owner(page.id, owner)) {
                pp_buffer_t data = payload(j, frame);
                if (pp_objects_write(key, owner, page_policy, &data, NULL, 0, background.ticket)) {
                    background.started = now();
                    return true;
                }
            }
            warm_retry();
        }
        return true;
    }
    if (!strcmp(type, "message.page") && foreground.active &&
        !strcmp(text(j, "message_id"), foreground.id) &&
        cJSON_GetNumberValue(cJSON_GetObjectItemCaseSensitive(j, "offset")) == foreground.offset)
        foreground.active = false;
    double revision;
    const cJSON *updated = cJSON_GetObjectItemCaseSensitive(j, "updated");
    const char *id = text(j, "message_id");
    if (enabled && !cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "cached")) &&
        strcmp(text(j, "status"), "streaming") && cJSON_IsNumber(updated) &&
        pp_history_cache_revision(id, &revision) && revision == updated->valuedouble) {
        const cJSON *offset = cJSON_GetObjectItemCaseSensitive(j, "offset");
        if (cJSON_IsNumber(offset) && offset->valuedouble >= 0 &&
            offset->valuedouble <= UINT32_MAX) {
            char key[256], owner[192];
            page_key(key, id, revision, (unsigned)offset->valuedouble);
            if (pp_history_cache_owner(id, owner)) {
                pp_buffer_t data = payload(j, frame);
                pp_objects_write(key, owner, page_policy, &data, NULL, 0, 0);
            }
        }
    }
    return prefetch;
}
void pp_history_cache_picture_done(const char *message, const char *image, double revision,
                                   bool completed) {
    if (warm_image >= count || strcmp(recent[warm_image].id, message) ||
        recent[warm_image].revision != revision || strcmp(warm_images[warm_image], image))
        return;
    if (completed)
        warm_image++;
    else
        retry_after = now() + 500;
}
void pp_history_cache_poll(bool idle) {
    pp_cache_result_t result;
    while (pp_objects_next(&result)) {
        bool hit = result.status == PP_CACHE_HIT;
        bool absent = result.status == PP_CACHE_MISSING || result.status == PP_CACHE_CORRUPT;
        if ((result.ticket & 3u) == 3u) {
            pp_picture_cached(&result);
        } else if (result.written) {
            if (result.ticket == index_write_ticket) {
                index_write_ticket = 0;
                if (hit || (result.status != PP_CACHE_BUSY && result.status != PP_CACHE_NO_MEMORY &&
                            ++index_write_failures >= 3)) {
                    free(pending_catalog);
                    pending_catalog = NULL;
                } else
                    retry_after = now() + 500;
            } else if (background.active && result.ticket == background.ticket) {
                if (hit)
                    warm_complete();
                else if (result.status == PP_CACHE_FULL) {
                    background.active = false;
                    warming = warm_pages >= count;
                    warm_pages = 96; /* Keep the reserved preview budget usable. */
                } else
                    warm_retry();
            }
        } else if ((result.ticket & 3u) == 2u) {
            if (result.ticket == recent_ticket && enabled && !pp_net_connected()) {
                cJSON *j = hit ? cJSON_ParseWithLength(result.value.bytes.data, result.value.bytes.size) : NULL;
                if (j && catalog(j)) {
                    free(pending_catalog);
                    pending_catalog = NULL;
                    background.active = false;
                    index_write_ticket = 0;
                    cJSON_SetValuestring(cJSON_GetObjectItemCaseSensitive(j, "type"), "cache.restored");
                    deliver(j);
                } else {
                    bool empty = absent || j;
                    cJSON_Delete(j);
                    cJSON *state = cJSON_CreateObject();
                    cJSON_AddStringToObject(state, "type", empty ? "cache.empty" : "cache.busy");
                    deliver(state);
                }
            }
        } else {
            read_t *read = foreground.active && foreground.ticket == result.ticket ? &foreground
                           : background.active && background.ticket == result.ticket ? &background
                           : NULL;
            if (read) {
                bool prefetch = read == &background;
                cJSON *j = hit ? cJSON_ParseWithLength(result.value.bytes.data, result.value.bytes.size) : NULL;
                pp_history_page_t page;
                double current;
                if (pp_history_cache_revision(read->id, &current) && current != read->revision) {
                    read->revision = current;
                    cJSON_Delete(j);
                    j = NULL;
                    absent = true;
                }
                if (j && (!pp_timeline_decode(&page, j) || !page.complete || strcmp(page.id, read->id) ||
                          page.offset != read->offset || page.revision != read->revision)) {
                    cJSON_Delete(j);
                    j = NULL;
                    absent = true;
                }
                if (j && prefetch) {
                    warm_metadata(j);
                    warm_complete();
                    cJSON_Delete(j);
                } else if (j) {
                    cJSON_SetValuestring(cJSON_GetObjectItemCaseSensitive(j, "type"), "message.page");
                    cJSON_DeleteItemFromObject(j, "prefetch");
                    cJSON_AddBoolToObject(j, "cached", true);
                    deliver(j);
                } else if (!absent || !remote(read, prefetch)) {
                    read->active = false;
                    if (prefetch)
                        warm_retry();
                    else
                        unavailable(read, !absent);
                }
            }
        }
        pp_objects_release(&result);
    }
    if (foreground.active && now() - foreground.started > 15000) {
        foreground.active = false;
        unavailable(&foreground, true);
    }
    if (background.active && now() - background.started > 15000)
        warm_retry();
    if (retry_after && (int32_t)(now() - retry_after) >= 0)
        retry_after = 0;
    if (idle && !retry_after && enabled && pp_net_connected() &&
        !background.active && !foreground.active) {
        /* Publish only after every head was a verified hit or a confirmed
         * commit. This is an I/O receipt, not a timer or a network send receipt. */
        if (pending_catalog && !index_write_ticket && (warm_pages >= count || !count)) {
            unsigned ticket = ++serial << 2;
            pp_buffer_t data = {0};
            size_t length = strlen(pending_catalog);
            if (pp_buffer_reserve(&data, length, PP_MEMORY_BACKGROUND, PP_MEMORY_CACHE)) {
                memcpy(data.data, pending_catalog, length);
                data.size = length;
                if (pp_objects_write("recent", "", (pp_object_write_t){.index = true}, &data, NULL, 0, ticket))
                    index_write_ticket = ticket;
                else
                    retry_after = now() + 500;
            }
        }
        if (!warming)
            return;
        unsigned available = 0;
        for (unsigned n = 0; n < count; n++) {
            if (!warm_done[warm_index]) {
                available = 1;
                break;
            }
            warm_index = (warm_index + 1) % count;
        }
        if (available && warm_pages < 96) {
            warm_offset = warm_offsets[warm_index];
            if (!begin(&background, recent[warm_index].id, warm_offset, true))
                warm_retry();
        } else if (images_enabled && warm_image < count) {
            if (!warm_images[warm_image][0])
                warm_image++;
            else if (!pp_picture_prefetch(recent[warm_image].id, warm_images[warm_image]))
                retry_after = now() + 500;
        } else
            warming = false;
    }
}

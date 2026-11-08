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
static entry_t recent[PP_CACHE_RECENT];
static unsigned count, serial, warm_index, warm_offset, warm_pages;
static unsigned warm_offsets[PP_CACHE_RECENT];
static bool warm_done[PP_CACHE_RECENT];
static char *pending_catalog;
static char *pending_page;
static unsigned index_write_ticket, index_write_failures;
static unsigned recent_ticket;
static bool enabled, warming, images_enabled;
static bool catalog_changed;
static char warm_images[PP_CACHE_RECENT][33];
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
    const char *owners[PP_CACHE_RECENT];
    char values[PP_CACHE_RECENT][192];
    for (unsigned i = 0; i < count; i++) {
        snprintf(values[i], sizeof(values[i]), "%s/%.17g", recent[i].id, recent[i].revision);
        owners[i] = values[i];
    }
    pp_disk_configure(scope, owners, count, enabled);
}
void pp_history_cache_init(void (*callback)(const char *, size_t)) {
    receive = callback;
    (void)pp_disk_start();
}
void pp_history_cache_bind(const char *url, const char *device, const char *binding, bool active,
                           bool images) {
    char next[sizeof(scope)];
    if (images && !images_enabled && count) {
        warm_image = 0;
        warming = true;
    }
    images_enabled = images;
    bool valid = pp_disk_identity(url, device, binding, next);
    active = active && binding[0] && valid;
    if (strcmp(scope, next) || enabled != active) {
        count = 0;
        memset(recent, 0, sizeof(recent));
        foreground.active = background.active = false;
        warming = false;
        free(pending_catalog);
        pending_catalog = NULL;
        free(pending_page);
        pending_page = NULL;
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
static void unavailable(const read_t *read) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "type", "cache.unavailable");
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
        if (pp_disk_read(key, read->ticket))
            return true;
        if (prefetch) {
            read->active = false; /* Retry this local lookup when the bounded queue drains. */
            return false;
        }
    }
    if (remote(read, prefetch))
        return true;
    read->active = false;
    if (!prefetch)
        unavailable(read);
    return true;
}
bool pp_history_cache_page(const char *id, unsigned offset) {
    return begin(&foreground, id, offset, false);
}
bool pp_history_cache_recent(void) {
    unsigned ticket = (++serial << 2) | 2u;
    if (!enabled || !pp_disk_read("recent", ticket))
        return false;
    recent_ticket = ticket;
    return true;
}
static bool catalog(const cJSON *j) {
    const cJSON *items = cJSON_GetObjectItemCaseSensitive(j, "items"), *item;
    if (!cJSON_IsArray(items) || cJSON_GetArraySize(items) > (int)PP_CACHE_RECENT)
        return false;
    entry_t next[PP_CACHE_RECENT] = {0};
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
        free(pending_page);
        pending_page = NULL;
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
static void completed(const cJSON *j, read_t *read, bool prefetch) {
    const cJSON *revision = cJSON_GetObjectItemCaseSensitive(j, "updated");
    if (!read->active || strcmp(text(j, "message_id"), read->id) ||
        !cJSON_IsNumber(cJSON_GetObjectItemCaseSensitive(j, "offset")) ||
        cJSON_GetObjectItemCaseSensitive(j, "offset")->valuedouble != read->offset)
        return;
    read->active = false;
    if (!prefetch)
        return;
    if (!cJSON_IsNumber(revision) || revision->valuedouble != read->revision) {
        warming = false; /* Wait for a current authoritative directory. */
        return;
    }
    const cJSON *images = cJSON_GetObjectItemCaseSensitive(j, "images");
    if (read->offset == 0 && warm_index < count && cJSON_IsArray(images)) {
        const char *image = text(cJSON_GetArrayItem(images, 0), "id");
        if (strlen(image) == 32)
            strcpy(warm_images[warm_index], image);
    }
    const cJSON *next = cJSON_GetObjectItemCaseSensitive(j, "next_offset");
    if (cJSON_IsNumber(next) && next->valuedouble > warm_offset && next->valuedouble <= UINT32_MAX)
        warm_offsets[warm_index] = (unsigned)next->valuedouble;
    else
        warm_done[warm_index] = true;
    warm_pages++;
    warm_index = (warm_index + 1) % count;
}
bool pp_history_cache_receive(cJSON *j) {
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
        if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "cached")))
            completed(j, &background, true);
        else {
            char key[256], owner[192];
            free(pending_page);
            pending_page = cJSON_PrintUnformatted(j);
            page_key(key, page.id, page.revision, page.offset);
            if (!pending_page || !pp_history_cache_owner(page.id, owner) ||
                !pp_disk_write_confirm(key, owner, false, pending_page, strlen(pending_page),
                                       background.ticket)) {
                free(pending_page);
                pending_page = NULL;
                background.active = false;
                warming = warm_pages >= count;
                if (warming)
                    warm_pages = 96; /* Keep the reserved preview budget usable. */
            } else
                background.started = now();
        }
        return true;
    }
    if (!strcmp(type, "message.page"))
        completed(j, &foreground, false);
    double revision;
    const cJSON *updated = cJSON_GetObjectItemCaseSensitive(j, "updated");
    const char *id = text(j, "message_id");
    if (enabled && !cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "cached")) &&
        strcmp(text(j, "status"), "streaming") && cJSON_IsNumber(updated) &&
        pp_history_cache_revision(id, &revision) && revision == updated->valuedouble) {
        const cJSON *offset = cJSON_GetObjectItemCaseSensitive(j, "offset");
        if (cJSON_IsNumber(offset) && offset->valuedouble >= 0 &&
            offset->valuedouble <= UINT32_MAX) {
            char key[256], owner[192], *raw = cJSON_PrintUnformatted(j);
            page_key(key, id, revision, (unsigned)offset->valuedouble);
            if (raw) {
                if (pp_history_cache_owner(id, owner))
                    pp_disk_write(key, owner, false, raw, strlen(raw));
                free(raw);
            }
        }
    }
    return prefetch;
}
void pp_history_cache_poll(bool idle) {
    pp_disk_result_t result;
    while (pp_disk_result(&result)) {
        if (result.written) {
            if (result.ticket == index_write_ticket) {
                index_write_ticket = 0;
                if (result.hit || ++index_write_failures >= 3) {
                    free(pending_catalog);
                    pending_catalog = NULL;
                }
            } else if (background.active && result.ticket == background.ticket && pending_page) {
                cJSON *j = result.hit ? cJSON_Parse(pending_page) : NULL;
                if (j)
                    completed(j, &background, true);
                else {
                    background.active = false;
                    warming = warm_pages >= count;
                    if (warming)
                        warm_pages = 96;
                }
                cJSON_Delete(j);
                free(pending_page);
                pending_page = NULL;
            }
        } else if ((result.ticket & 3u) == 3u) {
            pp_picture_cached(&result);
        } else if ((result.ticket & 3u) == 2u) {
            cJSON *j = result.hit ? cJSON_Parse(result.value.data) : NULL;
            if (result.ticket == recent_ticket && enabled && !pp_net_connected() && j &&
                catalog(j)) {
                free(pending_catalog);
                pending_catalog = NULL;
                free(pending_page);
                pending_page = NULL;
                background.active = false;
                index_write_ticket = 0;
                cJSON_SetValuestring(cJSON_GetObjectItemCaseSensitive(j, "type"), "cache.restored");
                deliver(j);
            } else {
                cJSON_Delete(j);
                if (result.ticket == recent_ticket && !pp_net_connected()) {
                    free(pending_catalog);
                    pending_catalog = NULL;
                    free(pending_page);
                    pending_page = NULL;
                    count = 0;
                    memset(recent, 0, sizeof(recent));
                    foreground.active = background.active = warming = false;
                    index_write_ticket = 0;
                    cJSON *empty = cJSON_CreateObject();
                    cJSON_AddStringToObject(empty, "type", "cache.empty");
                    deliver(empty);
                }
            }
        } else {
            read_t *read = foreground.active && foreground.ticket == result.ticket   ? &foreground
                           : background.active && background.ticket == result.ticket ? &background
                                                                                     : NULL;
            if (read) {
                bool prefetch = read == &background;
                cJSON *j = result.hit ? cJSON_Parse(result.value.data) : NULL;
                pp_history_page_t page;
                double current;
                if (pp_history_cache_revision(read->id, &current) && current != read->revision) {
                    read->revision = current;
                    cJSON_Delete(j);
                    j = NULL;
                }
                if (j &&
                    (!pp_timeline_decode(&page, j) || !page.complete || strcmp(page.id, read->id) ||
                     page.offset != read->offset || page.revision != read->revision)) {
                    cJSON_Delete(j);
                    j = NULL;
                }
                if (j) {
                    cJSON_SetValuestring(cJSON_GetObjectItemCaseSensitive(j, "type"),
                                         "message.page");
                    cJSON_DeleteItemFromObject(j, "prefetch");
                    cJSON_AddBoolToObject(j, "cached", true);
                    if (prefetch)
                        cJSON_AddBoolToObject(j, "prefetch", true);
                    deliver(j);
                } else if (!remote(read, prefetch)) {
                    read->active = false;
                    if (prefetch)
                        warming = false;
                    else
                        unavailable(read);
                }
            }
        }
        pp_cache_value_free(&result.value);
    }
    if (foreground.active && now() - foreground.started > 15000) {
        foreground.active = false;
        unavailable(&foreground);
    }
    if (background.active && now() - background.started > 15000) {
        background.active = false;
        warming = false;
        free(pending_page);
        pending_page = NULL;
    }
    if (idle && enabled && pp_net_connected() && !background.active && !foreground.active) {
        /* Publish only after every head was a verified hit or a confirmed
         * commit. This is an I/O receipt, not a timer or a network send receipt. */
        if (pending_catalog && !index_write_ticket && (warm_pages >= count || !count)) {
            unsigned ticket = ++serial << 2;
            if (pp_disk_write_confirm("recent", "", true, pending_catalog, strlen(pending_catalog),
                                      ticket))
                index_write_ticket = ticket;
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
            begin(&background, recent[warm_index].id, warm_offset, true);
        } else if (images_enabled && warm_image < count) {
            unsigned index = warm_image++;
            if (warm_images[index][0])
                pp_picture_prefetch(recent[index].id, warm_images[index]);
        } else
            warming = false;
    }
}

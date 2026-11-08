#include "timeline.h"
#include <limits.h>
#include <string.h>

static bool copy(char *target, size_t size, const cJSON *object, const char *key, bool required) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!v && !required) {
        target[0] = 0;
        return true;
    }
    if (!cJSON_IsString(v) || strlen(v->valuestring) >= size || (required && !v->valuestring[0]))
        return false;
    strcpy(target, v->valuestring);
    return true;
}
static bool integer(const cJSON *j, const char *key, unsigned *target, bool nullable) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    if (nullable && (!v || cJSON_IsNull(v))) {
        *target = 0;
        return true;
    }
    if (!cJSON_IsNumber(v) || v->valuedouble < 0 || v->valuedouble > UINT_MAX ||
        v->valuedouble != (unsigned)v->valuedouble)
        return false;
    *target = (unsigned)v->valuedouble;
    return true;
}
bool pp_timeline_index(pp_timeline_t *timeline, const cJSON *response) {
    const cJSON *items = cJSON_GetObjectItemCaseSensitive(response, "items");
    if (!cJSON_IsArray(items) || cJSON_GetArraySize(items) > PP_TIMELINE_ENTRIES)
        return false;
    /* Validate the whole response before replacing the previous useful window. */
    const cJSON *item;
    cJSON_ArrayForEach(item, items) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(item, "id");
        const cJSON *cursor = cJSON_GetObjectItemCaseSensitive(item, "cursor");
        const cJSON *role = cJSON_GetObjectItemCaseSensitive(item, "role");
        if (!cJSON_IsString(id) || !id->valuestring[0] ||
            strlen(id->valuestring) >= PP_MESSAGE_ID || !cJSON_IsString(cursor) ||
            !cursor->valuestring[0] || strlen(cursor->valuestring) >= PP_HISTORY_CURSOR ||
            !cJSON_IsString(role) ||
            (strcmp(role->valuestring, "user") && strcmp(role->valuestring, "assistant")))
            return false;
        for (const cJSON *prior = items->child; prior != item; prior = prior->next)
            if (!strcmp(cJSON_GetObjectItemCaseSensitive(prior, "id")->valuestring,
                        id->valuestring))
                return false;
    }
    timeline->count = 0;
    cJSON_ArrayForEach(item, items) {
        pp_history_entry_t *entry = &timeline->entries[timeline->count++];
        copy(entry->id, sizeof(entry->id), item, "id", true);
        copy(entry->cursor, sizeof(entry->cursor), item, "cursor", true);
        entry->user = !strcmp(cJSON_GetObjectItemCaseSensitive(item, "role")->valuestring, "user");
    }
    return true;
}
const pp_history_entry_t *pp_timeline_entry(const pp_timeline_t *timeline, const char *id) {
    for (unsigned i = 0; i < timeline->count; i++)
        if (!strcmp(timeline->entries[i].id, id))
            return &timeline->entries[i];
    return NULL;
}
const pp_history_entry_t *pp_timeline_neighbor(const pp_timeline_t *timeline, const char *id,
                                               int direction) {
    for (unsigned i = 0; i < timeline->count; i++) {
        if (strcmp(timeline->entries[i].id, id))
            continue;
        int next = (int)i + direction;
        return next >= 0 && next < (int)timeline->count ? &timeline->entries[next] : NULL;
    }
    return NULL;
}
pp_history_page_t *pp_timeline_decode(pp_history_page_t *page, const cJSON *j) {
    memset(page, 0, sizeof(*page));
    char role[16], status[24], kind[24];
    if (!copy(page->id, sizeof(page->id), j, "message_id", true) ||
        !copy(page->text, sizeof(page->text), j, "text", false) ||
        !copy(role, sizeof(role), j, "role", false) ||
        !copy(status, sizeof(status), j, "status", true) ||
        !copy(kind, sizeof(kind), j, "kind", false) ||
        !integer(j, "offset", &page->offset, false) ||
        !integer(j, "next_offset", &page->next, true) || !integer(j, "total", &page->total, true))
        return NULL;
    const cJSON *updated = cJSON_GetObjectItemCaseSensitive(j, "updated");
    if (updated && (!cJSON_IsNumber(updated) || updated->valuedouble < 0))
        return NULL;
    page->revision = updated ? updated->valuedouble : 0;
    page->user = !strcmp(role, "user");
    page->complete = strcmp(status, "streaming") != 0;
    page->image_only = !strcmp(kind, "image");
    if (page->next && page->next <= page->offset)
        return NULL;
    const cJSON *images = cJSON_GetObjectItemCaseSensitive(j, "images"), *image;
    if (images && !cJSON_IsArray(images))
        return NULL;
    cJSON_ArrayForEach(image, images) {
        if (page->image_count >= 4 ||
            !copy(page->images[page->image_count++], 33, image, "id", true))
            return NULL;
    }
    return page;
}

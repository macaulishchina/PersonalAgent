#include "timeline.h"
#include <limits.h>
#include <stdio.h>
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
pp_history_page_t *pp_timeline_window(pp_history_page_t *page, const cJSON *j, unsigned offset) {
    memset(page, 0, sizeof(*page));
    char role[16], status[24], kind[24];
    if (!copy(page->id, sizeof(page->id), j, "message_id", true) ||
        !copy(page->group, sizeof(page->group), j, "group", false) ||
        !copy(role, sizeof(role), j, "role", false) || !copy(status, sizeof(status), j, "status", true) ||
        !copy(kind, sizeof(kind), j, "kind", false) || !integer(j, "offset", &page->offset, false) ||
        !integer(j, "next_offset", &page->next, true) || !integer(j, "total", &page->total, true))
        return NULL;
    const cJSON *body = cJSON_GetObjectItemCaseSensitive(j, "text");
    if (body && !cJSON_IsString(body))
        return NULL;
    const char *p = body ? body->valuestring : "";
    if (offset == UINT_MAX)
        offset = page->offset;
    if (offset < page->offset)
        return NULL;
    unsigned pos = page->offset;
    while (*p && pos < offset) {
        do {
            p++;
        } while (((unsigned char)*p & 0xc0) == 0x80);
        pos++;
    }
    if (pos != offset)
        return NULL;
    page->text = p;
    page->offset = offset;
    const cJSON *updated = cJSON_GetObjectItemCaseSensitive(j, "updated");
    if (updated && (!cJSON_IsNumber(updated) || updated->valuedouble < 0))
        return NULL;
    page->revision = updated ? updated->valuedouble : 0;
    page->legacy = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "legacy"));
    if (!page->group[0]) {
        if (page->legacy)
            snprintf(page->group, sizeof(page->group), "%s/%.17g", page->id, page->revision);
        else
            strcpy(page->group, page->id);
    }
    page->user = !strcmp(role, "user");
    page->complete = strcmp(status, "streaming") != 0;
    page->image_only = !strcmp(kind, "image");
    /* An older live message only contains the first server page. A remote
     * continuation inside this block cannot address another local object.
     * Imported local pages have their own stride and keep their links. */
    if (!page->legacy && page->complete && page->next % PP_HISTORY_BLOCK_CHARS) {
        page->next = 0;
        page->local_excerpt = true;
    }
    if (page->next && page->next <= page->offset)
        return NULL;
    const cJSON *images = cJSON_GetObjectItemCaseSensitive(j, "images"), *image;
    if (images && !cJSON_IsArray(images))
        return NULL;
    cJSON_ArrayForEach(image, images) {
        if (page->image_count >= PP_HISTORY_IMAGES ||
            !copy(page->images[page->image_count++], 33, image, "id", true))
            return NULL;
    }
    return page;
}

pp_history_page_t *pp_timeline_decode(pp_history_page_t *page, const cJSON *j) {
    return pp_timeline_window(page, j, UINT_MAX);
}

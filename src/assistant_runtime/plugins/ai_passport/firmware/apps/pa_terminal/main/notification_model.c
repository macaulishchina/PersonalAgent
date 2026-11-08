#include "notification_model.h"
#include <string.h>

static bool equal_number(const cJSON *value, unsigned expected) {
    return cJSON_IsNumber(value) && value->valuedouble == expected;
}
bool pp_notice_begin(pp_notice_read_t *read, const char *id, unsigned revision, unsigned offset,
                     uint32_t now) {
    if (read->pending || !id || !id[0] || strlen(id) >= sizeof(read->id) || !revision)
        return false;
    memmove(read->id, id, strlen(id) + 1);
    read->revision = revision;
    read->offset = offset;
    read->started = now;
    read->pending = true;
    return true;
}
int pp_notice_page(pp_notice_read_t *read, const cJSON *page) {
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(page, "id");
    if (!read->pending || !cJSON_IsString(id) || strcmp(read->id, id->valuestring) ||
        !equal_number(cJSON_GetObjectItemCaseSensitive(page, "revision"), read->revision) ||
        !equal_number(cJSON_GetObjectItemCaseSensitive(page, "offset"), read->offset))
        return 0;
    read->pending = false;
    const cJSON *next = cJSON_GetObjectItemCaseSensitive(page, "next_offset");
    if (!cJSON_IsString(cJSON_GetObjectItemCaseSensitive(page, "text")) ||
        !(cJSON_IsNull(next) ||
          (cJSON_IsNumber(next) && next->valuedouble > read->offset &&
           next->valuedouble <= UINT32_MAX && next->valuedouble == (unsigned)next->valuedouble)))
        return -1;
    return 1;
}
bool pp_notice_expire(pp_notice_read_t *read, uint32_t now) {
    /* A directory/page read does not wait for model execution or change a task. */
    if (!read->pending || now - read->started < 15000)
        return false;
    read->pending = false;
    return true;
}

#include "timeline.h"
#include <assert.h>
#include <string.h>

int main(void) {
    pp_timeline_t timeline = {0};
    cJSON *index = cJSON_Parse("{\"items\":[{\"id\":\"one\",\"cursor\":\"a\",\"role\":\"user\"},"
                               "{\"id\":\"two\",\"cursor\":\"b\",\"role\":\"assistant\"}]}");
    assert(pp_timeline_index(&timeline, index));
    assert(timeline.count == 2 && pp_timeline_entry(&timeline, "one")->user);
    assert(!strcmp(pp_timeline_neighbor(&timeline, "two", -1)->id, "one"));
    assert(!pp_timeline_neighbor(&timeline, "two", 1));
    cJSON_DeleteItemFromObject(cJSON_GetArrayItem(cJSON_GetObjectItem(index, "items"), 0),
                               "cursor");
    assert(!pp_timeline_index(&timeline, index) && timeline.count == 2);
    cJSON_Delete(index);
    pp_history_page_t page;
    cJSON *j = cJSON_Parse("{\"message_id\":\"two\",\"text\":\"正文\",\"role\":\"assistant\","
                           "\"status\":\"complete\",\"offset\":0,\"total\":2,\"next_offset\":null,"
                           "\"updated\":2,\"images\":[{\"id\":\"photo\"}]}");
    assert(pp_timeline_decode(&page, j));
    assert(page.complete && page.total == 2 && !strcmp(page.text, "正文"));
    assert(page.image_count == 1 && !strcmp(page.images[0], "photo"));
    cJSON_SetValuestring(cJSON_GetObjectItem(j, "status"), "streaming");
    assert(pp_timeline_decode(&page, j) && !page.complete);
    cJSON_SetNumberValue(cJSON_GetObjectItem(j, "offset"), -1);
    assert(!pp_timeline_decode(&page, j));
    cJSON_Delete(j);
    /* No RAM payload cache duplicates the bounded Flash data store. */
    assert(sizeof(timeline) < 2100);
    return 0;
}

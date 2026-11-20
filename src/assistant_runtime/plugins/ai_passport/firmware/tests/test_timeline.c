#include "timeline.h"
#include <assert.h>
#include <string.h>

int main(void) {
    pp_history_page_t page;
    cJSON *j = cJSON_Parse("{\"message_id\":\"two\",\"text\":\"正文\",\"role\":\"assistant\","
                           "\"status\":\"complete\",\"offset\":0,\"total\":2,\"next_offset\":null,"
                           "\"updated\":2,\"images\":[{\"id\":\"photo\"}]}");
    assert(pp_timeline_decode(&page, j));
    assert(page.complete && page.total == 2 && !strcmp(page.text, "正文"));
    assert(page.image_count == 1 && !strcmp(page.images[0], "photo"));
    /* Device page offsets count Unicode scalars, including four-byte glyphs. */
    char long_text[4 * 82 + 1] = "";
    for (unsigned i = 0; i < 82; i++)
        strcat(long_text, "🙂");
    cJSON_SetValuestring(cJSON_GetObjectItem(j, "text"), long_text);
    cJSON_SetNumberValue(cJSON_GetObjectItem(j, "total"), 1682);
    cJSON_SetNumberValue(cJSON_GetObjectItem(j, "offset"), 1600);
    assert(pp_timeline_window(&page, j, 1600) && !page.next && strlen(page.text) == 328);
    assert(pp_timeline_window(&page, j, 1680) && !page.next && !strcmp(page.text, "🙂🙂"));
    assert(!pp_timeline_window(&page, j, 1599));
    assert(!pp_timeline_window(&page, j, 1683));
    cJSON_SetValuestring(cJSON_GetObjectItem(j, "status"), "streaming");
    assert(pp_timeline_decode(&page, j) && !page.complete);
    cJSON_SetNumberValue(cJSON_GetObjectItem(j, "offset"), -1);
    assert(!pp_timeline_decode(&page, j));
    cJSON_Delete(j);

    /* Older PA sent only a live prefix, with a pointer into SERVER history.
     * The next 160 characters were never delivered to the local journal. */
    char prefix[161];
    memset(prefix, 'x', 160);
    prefix[160] = 0;
    j = cJSON_Parse("{\"type\":\"message\",\"message_id\":\"old-live\",\"offset\":0,"
                    "\"status\":\"complete\",\"next_offset\":160,\"total\":204,\"more\":true}");
    cJSON_AddStringToObject(j, "text", prefix);
    assert(pp_timeline_window(&page, j, 80) && strlen(page.text) == 80 && page.next == 0);
    assert(page.local_excerpt && page.total == 204);
    /* Imported legacy local pages use independent 80-character cache keys. */
    cJSON_AddBoolToObject(j, "legacy", true);
    assert(pp_timeline_window(&page, j, 0) && page.next == 160);
    /* Current PA actually delivers the continuation at the 1600 boundary. */
    cJSON_DeleteItemFromObject(j, "legacy");
    char full_block[PP_HISTORY_BLOCK_CHARS + 1];
    memset(full_block, 'x', PP_HISTORY_BLOCK_CHARS);
    full_block[PP_HISTORY_BLOCK_CHARS] = 0;
    cJSON_SetValuestring(cJSON_GetObjectItem(j, "text"), full_block);
    cJSON_SetNumberValue(cJSON_GetObjectItem(j, "total"), PP_HISTORY_BLOCK_CHARS + 20);
    cJSON_SetNumberValue(cJSON_GetObjectItem(j, "next_offset"), PP_HISTORY_BLOCK_CHARS);
    assert(pp_timeline_window(&page, j, 0) && page.next == PP_HISTORY_BLOCK_CHARS);
    cJSON_Delete(j);
    return 0;
}

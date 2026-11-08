#include "notification_model.h"
#include <assert.h>
#include <string.h>

int main(void) {
    pp_notice_read_t read = {0};
    cJSON *page = cJSON_Parse("{\"type\":\"notification.page\",\"id\":\"notice1\",\"revision\":2,"
                              "\"offset\":0,\"text\":\"任务结果\",\"next_offset\":80}");
    assert(pp_notice_begin(&read, "notice1", 2, 0, 100));
    assert(!pp_notice_begin(&read, "notice2", 1, 0, 101));
    assert(pp_notice_page(&read, page) == 1 && !read.pending);
    assert(pp_notice_page(&read, page) == 0); /* duplicate/late response */

    assert(pp_notice_begin(&read, read.id, read.revision, 80, 200));
    assert(pp_notice_page(&read, page) == 0 && read.pending); /* previous page */
    cJSON_SetNumberValue(cJSON_GetObjectItemCaseSensitive(page, "offset"), 80);
    cJSON_ReplaceItemInObject(page, "next_offset", cJSON_CreateNull());
    assert(pp_notice_page(&read, page) == 1);

    assert(pp_notice_begin(&read, "notice2", 1, 0, 300));
    assert(pp_notice_page(&read, page) == 0 && read.pending); /* other task */
    read.pending = false; /* selection/back/home cancels the local wait */
    assert(pp_notice_page(&read, page) == 0);
    assert(!pp_notice_begin(&read, "", 1, 0, 0));
    assert(!pp_notice_begin(&read, "notice1", 0, 0, 0));

    assert(pp_notice_begin(&read, "notice1", 2, 80, 500));
    cJSON_ReplaceItemInObject(page, "text", cJSON_CreateObject());
    assert(pp_notice_page(&read, page) == -1 && !read.pending);
    assert(pp_notice_begin(&read, "notice1", 2, 80, 600));
    cJSON_ReplaceItemInObject(page, "text", cJSON_CreateString("正文"));
    cJSON_ReplaceItemInObject(page, "next_offset", cJSON_CreateNumber(79));
    assert(pp_notice_page(&read, page) == -1 && !read.pending);

    assert(pp_notice_begin(&read, "notice1", 2, 0, UINT32_MAX - 100));
    assert(!pp_notice_expire(&read, 14898));
    assert(pp_notice_expire(&read, 14899) && !read.pending);
    assert(!pp_notice_expire(&read, 20000));
    assert(pp_notice_begin(&read, "notice1", 2, 0, 20001));
    cJSON_Delete(page);
    /* The former synthetic message identity must not be mistaken for a page. */
    page = cJSON_Parse(
        "{\"id\":\"notification:notice1\",\"notification_id\":\"notice1\",\"notification_"
        "revision\":2,\"offset\":0,\"text\":\"旧形状\",\"next_offset\":null}");
    assert(pp_notice_page(&read, page) == 0 && read.pending);
    assert(pp_notice_expire(&read, 35001));
    cJSON_Delete(page);
    return 0;
}

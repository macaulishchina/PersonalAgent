#include "lab_language.h"
#include <assert.h>
#include <string.h>
int main(void)
{
#ifdef PPDEMO_ZH
    assert(!strcmp(LAB_APP_NAME, "PPDemo-Zh"));
    assert(!strcmp(lab_ui_name(LAB_DISPLAY), "屏幕"));
    assert(!strcmp(lab_ui_name(LAB_AUDIO), "声音与录音"));
    assert(!strcmp(lab_ui_result(LAB_PASS), "通过"));
    assert(!strcmp(lab_ui_result(LAB_FAIL), "不通过"));
    assert(!strcmp(lab_ui_result(LAB_UNTESTED), "未测试"));
#else
    assert(!strcmp(LAB_APP_NAME, "PPDemo"));
    assert(!strcmp(lab_ui_name(LAB_DISPLAY), "DISPLAY"));
    assert(!strcmp(lab_ui_result(LAB_PASS), "PASS"));
#endif
    /* Language is presentation only: preserve exported report keys and records. */
    assert(!strcmp(lab_name(LAB_DISPLAY), "DISPLAY"));
    assert(!strcmp(lab_result_name(LAB_PASS), "PASS"));
    for (int i=0; i<LAB_COUNT; i++) assert(lab_ui_name(i)[0]);
    return 0;
}

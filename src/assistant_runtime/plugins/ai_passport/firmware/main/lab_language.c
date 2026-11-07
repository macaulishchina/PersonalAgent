#include "lab_language.h"
const char *lab_ui_name(lab_case_t item)
{
#ifdef PPDEMO_ZH
    static const char *names[LAB_COUNT] = {
        "屏幕", "按键", "声音与录音", "电池", "Wi-Fi", "蓝牙",
        "存储", "休眠与唤醒", "USB 串口", "NFC 标签"
    };
    return (unsigned)item < LAB_COUNT ? names[item] : "未知";
#else
    return lab_name(item);
#endif
}
const char *lab_ui_result(unsigned result)
{
#ifdef PPDEMO_ZH
    return result == LAB_PASS ? "通过" : result == LAB_FAIL ? "不通过" : "未测试";
#else
    return lab_result_name(result);
#endif
}

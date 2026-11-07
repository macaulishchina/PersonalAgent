#pragma once
#include "lab_model.h"
#ifdef PPDEMO_ZH
#define LAB_TEXT(english, chinese) chinese
#define LAB_APP_NAME "PPDemo-Zh"
#else
#define LAB_TEXT(english, chinese) english
#define LAB_APP_NAME "PPDemo"
#endif
const char *lab_ui_name(lab_case_t item);
const char *lab_ui_result(unsigned result);

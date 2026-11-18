#pragma once
#include <stdbool.h>
#include <stdint.h>
typedef int esp_err_t;
/* Deliberately synthetic GPIO/ADC values catch hardcoded board settings. */
enum { ESP_OK = 0, ESP_ERR_INVALID_STATE = 1, ESP_ERR_TIMEOUT = 2, ESP_GPIO_WAKEUP_GPIO_LOW = 3,
       BSP_BTN_GPIO = 6, BSP_BTN_MV_RELEASED_MIN = 1913 };
int bsp_button_read_mv(void);
int bsp_audio_sleep(void);
int bsp_button_prepare_deep_sleep(int *);
int esp_deep_sleep_enable_gpio_wakeup(uint64_t, int);
int bsp_battery_sleep(void);
int bsp_audio_prepare_deep_sleep(void);
int bsp_i2c_prepare_deep_sleep(void);
bool bsp_lvgl_lock(int);
int bsp_display_prepare_deep_sleep(void);
_Noreturn void esp_restart(void);
_Noreturn void esp_deep_sleep_start(void);
const char *esp_err_to_name(int);
void test_log(const char *, const char *, ...);
#define ESP_LOGE test_log

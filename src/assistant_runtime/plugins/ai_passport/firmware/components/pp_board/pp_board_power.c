#include "pp_board_power.h"

#include "bsp_audio.h"
#include "bsp_battery.h"
#include "bsp_button.h"
#include "bsp_display.h"
#include "bsp_i2c.h"
#include "bsp_pins.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "esp_system.h"

bool pp_board_keys_released(void) {
    return bsp_button_read_mv() >= BSP_BTN_MV_RELEASED_MIN;
}

static void require_handoff(const char *stage, esp_err_t error) {
    if (error != ESP_OK) {
        ESP_LOGE("pp_board", "deep sleep failed at %s: %s; restarting", stage, esp_err_to_name(error));
        esp_restart();
    }
}

_Noreturn void pp_board_enter_deep_sleep(void) {
    require_handoff("audio suspend", bsp_audio_sleep());
    int released = 0;
    require_handoff("ADC handoff", bsp_button_prepare_deep_sleep(&released));
    require_handoff("key release", released ? ESP_OK : ESP_ERR_INVALID_STATE);
    require_handoff("GPIO wake", esp_deep_sleep_enable_gpio_wakeup(1ULL << BSP_BTN_GPIO,
                                                                   ESP_GPIO_WAKEUP_GPIO_LOW));
    /* Both I2C clients must finish before releasing the shared SDA/SCL pins. */
    require_handoff("battery suspend", bsp_battery_sleep());
    require_handoff("I2S pins", bsp_audio_prepare_deep_sleep());
    require_handoff("I2C pins", bsp_i2c_prepare_deep_sleep());
    require_handoff("display lock", bsp_lvgl_lock(1000) ? ESP_OK : ESP_ERR_TIMEOUT);
    /* No further flush or unlock is valid once the panel pins are held. */
    require_handoff("display pins", bsp_display_prepare_deep_sleep());
    esp_deep_sleep_start();
}

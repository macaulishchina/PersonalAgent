#include "board_power_fixture.h"
#include "pp_board_power.h"
#ifdef NDEBUG
#undef NDEBUG
#endif
#include <assert.h>
#include <setjmp.h>

typedef enum {
    AUDIO, ADC, GPIO_WAKE, BATTERY, I2S, I2C, LOCK, DISPLAY, STAGES
} stage_t;
enum { SLEPT = 1, RESTARTED };
static jmp_buf terminal_exit;
static int adc_mv, released;
static stage_t next, fail_at;

static int step(stage_t stage) {
    assert(next == stage);
    next++;
    return stage == fail_at ? ESP_ERR_INVALID_STATE : ESP_OK;
}
int bsp_button_read_mv(void) { return adc_mv; }
int bsp_audio_sleep(void) { return step(AUDIO); }
int bsp_button_prepare_deep_sleep(int *level) {
    *level = released;
    return step(ADC);
}
int esp_deep_sleep_enable_gpio_wakeup(uint64_t mask, int trigger) {
    assert(mask == (1ULL << BSP_BTN_GPIO) && trigger == ESP_GPIO_WAKEUP_GPIO_LOW);
    return step(GPIO_WAKE);
}
int bsp_battery_sleep(void) { return step(BATTERY); }
int bsp_audio_prepare_deep_sleep(void) { return step(I2S); }
int bsp_i2c_prepare_deep_sleep(void) { return step(I2C); }
bool bsp_lvgl_lock(int timeout_ms) {
    assert(timeout_ms > 0);
    return step(LOCK) == ESP_OK;
}
int bsp_display_prepare_deep_sleep(void) { return step(DISPLAY); }
const char *esp_err_to_name(int err) { (void)err; return "injected"; }
void test_log(const char *tag, const char *format, ...) { (void)tag; (void)format; }
_Noreturn void esp_restart(void) { longjmp(terminal_exit, RESTARTED); }
_Noreturn void esp_deep_sleep_start(void) { longjmp(terminal_exit, SLEPT); }

static int run(void) {
    next = AUDIO;
    switch (setjmp(terminal_exit)) {
    case 0:
        pp_board_enter_deep_sleep();
    case SLEPT:
        return SLEPT;
    default:
        return RESTARTED;
    }
}

int main(void) {
    adc_mv = -1;
    assert(!pp_board_keys_released());
    adc_mv = BSP_BTN_MV_RELEASED_MIN - 1;
    assert(!pp_board_keys_released());
    adc_mv++;
    assert(pp_board_keys_released());

    released = 1;
    for (fail_at = AUDIO; fail_at < STAGES; fail_at++) {
        assert(run() == RESTARTED);
        assert(next == fail_at + 1);
    }
    assert(run() == SLEPT && next == STAGES);
    released = 0; /* Key pressed between the ADC preflight and digital handoff. */
    assert(run() == RESTARTED && next == GPIO_WAKE);
}

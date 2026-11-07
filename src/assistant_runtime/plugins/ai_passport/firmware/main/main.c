#include "lab_language.h"
#include "lab_model.h"
#include "lab_runtime.h"
#include "lab_ui.h"
#include "bsp_audio.h"
#include "bsp_battery.h"
#include "bsp_button.h"
#include "bsp_display.h"
#include "bsp_i2c.h"
#include "bsp_pins.h"
#include "esp_app_desc.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"
#include <math.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef enum { EV_KEY, EV_NOTICE, EV_DONE, EV_REPORT } event_kind_t;
typedef struct {
    event_kind_t kind;
    bsp_btn_t key;
    bsp_btn_ev_t action;
    esp_err_t error;
    char text[512];
} event_t;
typedef struct { lab_case_t test; unsigned action; } job_t;
typedef enum { HOME, TEST, REVIEW, REPORT } view_t;
static QueueHandle_t events, jobs;
static atomic_bool cancel_job, terminal_sleep, restart_required, wake_guard;
static int64_t wake_guard_deadline;
static atomic_uint dropped;
static nvs_handle_t storage;
static bool storage_open, busy;
static esp_err_t audio_state, battery_state, network_state;
static lab_record_t record;
static uint32_t boots;
static int16_t *clip;
static size_t clip_samples;
static RTC_DATA_ATTR uint32_t sleep_marker;
static unsigned selected, action, key_counts[3][5];
static view_t view = HOME;
static int64_t ignore_click_until;
static bool review_pending;
static char detail[512] = LAB_TEXT("Driver evidence is separate\nfrom your PASS / FAIL grade.", "操作结果供参考\n是否通过由你判断");

void lab_require_restart(void) { atomic_store(&restart_required, true); }
bool lab_cancelled(void) { return atomic_load(&cancel_job); }
void lab_notice(const char *format, ...)
{
    event_t event = {.kind=EV_NOTICE};
    va_list args; va_start(args, format);
    vsnprintf(event.text, sizeof(event.text), format, args); va_end(args);
    if (xQueueSend(events, &event, 0) != pdTRUE) atomic_fetch_add(&dropped, 1);
}
static void button(bsp_btn_t key, bsp_btn_ev_t action_value, void *arg)
{
    (void)arg;
    if (atomic_load(&wake_guard)) {
        if (action_value == BSP_BTN_RELEASE || esp_timer_get_time() >= wake_guard_deadline)
            atomic_store(&wake_guard, false);
        return;
    }
    event_t event = {.kind=EV_KEY, .key=key, .action=action_value};
    if (xQueueSend(events, &event, 0) != pdTRUE) atomic_fetch_add(&dropped, 1);
}
static esp_err_t save_record(void)
{
    if (!storage_open) return ESP_ERR_INVALID_STATE;
    esp_err_t e = nvs_set_blob(storage, "record", &record, sizeof(record));
    return e == ESP_OK ? nvs_commit(storage) : e;
}
static void init_storage(void)
{
    lab_record_init(&record);
    esp_err_t e = nvs_flash_init();
    // Never erase a user's NVS to recover an incompatible or full partition.
    if (e != ESP_OK) { snprintf(detail, sizeof(detail), LAB_TEXT("NVS unavailable: %s\nNo erase attempted.", "NVS 不可用: %s\n未擦除存储"), esp_err_to_name(e)); return; }
    e = nvs_open("ppdemo", NVS_READWRITE, &storage);
    if (e != ESP_OK) return;
    storage_open = true;
    size_t n = sizeof(record);
    lab_record_t saved;
    e = nvs_get_blob(storage, "record", &saved, &n);
    if (e == ESP_OK && n == sizeof(saved) && lab_record_valid(&saved)) record = saved;
    else if (e != ESP_ERR_NVS_NOT_FOUND)
        snprintf(detail, sizeof(detail), LAB_TEXT("Saved report invalid.\nRAM report starts empty.\nNo erase attempted.", "原报告无效\n本次从空报告开始\n未擦除存储"));
    (void)nvs_get_u32(storage, "boots", &boots);
    if (boots < UINT32_MAX) boots++;
    if (nvs_set_u32(storage, "boots", boots) == ESP_OK) (void)nvs_commit(storage);
}
static void export_report(void)
{
    char json[1100];
    int n = snprintf(json, sizeof(json), "{\"app\":\"" LAB_APP_NAME "\",\"version\":\"%s\",\"boots\":%lu,\"manual_grades\":{",
                     esp_app_get_description()->version, (unsigned long)boots);
    for (unsigned i=0; i<LAB_COUNT; i++)
        n += snprintf(json+n, sizeof(json)-(size_t)n, "%s\"%s\":\"%s\"", i ? "," : "", lab_name(i), lab_result_name(record.results[i]));
    snprintf(json+n, sizeof(json)-(size_t)n, "},\"dropped_ui_events\":%u,\"free_heap\":%lu,\"hardware_acceptance\":\"operator_only\"}\n",
             atomic_load(&dropped), (unsigned long)esp_get_free_heap_size());
    printf("%s", json); fflush(stdout);
}
static esp_err_t audio_test(unsigned operation)
{
    if (audio_state != ESP_OK) return audio_state;
    esp_err_t e = bsp_audio_set_format(16000, 16, 1);
    if (e != ESP_OK) return e;
    bsp_audio_set_volume(45);
    int16_t samples[256];
    if (operation == 0) {
        const unsigned tones[] = {440, 660, 880};
        for (unsigned tone=0; tone<3 && !lab_cancelled(); tone++) {
            lab_notice(LAB_TEXT("Tone %u Hz\nVolume 45%%\n\nListen for clean sound.", "音调 %u Hz\n音量 45%%\n\n请听声音是否清晰"), tones[tone]);
            for (unsigned block=0; block<16 && !lab_cancelled(); block++) {
                for (unsigned i=0; i<256; i++) samples[i]=(int16_t)(7000*sinf(6.2831853f*tones[tone]*(block*256+i)/16000));
                e = bsp_audio_write(samples, sizeof(samples)); if (e != ESP_OK) return e;
            }
        }
    } else if (operation == 1) {
        free(clip); clip=NULL; clip_samples=0;
        int16_t *recording=malloc(48000*sizeof(int16_t));
        if (!recording) return ESP_ERR_NO_MEM;
        unsigned peak=0, clipped=0;
        lab_notice(LAB_TEXT("Recording 3 seconds...\nSpeak normally.\n\nRAM only. Never uploaded.", "正在录音 3 秒\n请正常说话\n\n仅存内存，不上传"));
        for (unsigned offset=0; offset<48000 && !lab_cancelled();) {
            unsigned count=48000-offset < 256 ? 48000-offset : 256;
            e=bsp_audio_read(recording+offset, count*sizeof(int16_t)); if (e != ESP_OK) break;
            for (unsigned i=0; i<count; i++) {
                unsigned value=abs((int)recording[offset+i]); if (value>peak) peak=value;
                if (value>=32760) clipped++;
            }
            offset+=count;
        }
        if (e == ESP_OK && !lab_cancelled()) { clip=recording; clip_samples=48000; }
        else free(recording);
        lab_notice(LAB_TEXT("Recording %s\nPeak %u / 32768\nClipped samples %u\n\nSelect playback to listen.", "录音%s\n峰值 %u / 32768\n削波采样 %u\n\n选择回放试听"), clip ? LAB_TEXT("complete", "完成") : LAB_TEXT("discarded", "已丢弃"), peak, clipped);
    } else {
        if (!clip_samples) return ESP_ERR_INVALID_STATE;
        for (size_t offset=0; offset<clip_samples && !lab_cancelled();) {
            size_t count=clip_samples-offset < 256 ? clip_samples-offset : 256;
            e=bsp_audio_write(clip+offset, count*sizeof(int16_t)); if (e != ESP_OK) return e;
            offset+=count;
        }
    }
    return e;
}
static esp_err_t storage_test(void)
{
    if (!storage_open) return ESP_ERR_INVALID_STATE;
    uint32_t before=0, after=0;
    esp_err_t e=nvs_get_u32(storage, "probe", &before);
    if (e != ESP_OK && e != ESP_ERR_NVS_NOT_FOUND) return e;
    e=nvs_set_u32(storage, "probe", before+1);
    if (e == ESP_OK) e=nvs_commit(storage);
    if (e == ESP_OK) e=nvs_get_u32(storage, "probe", &after);
    if (e == ESP_OK && after!=before+1) e=ESP_FAIL;
    lab_notice(LAB_TEXT("NVS commit/read: %s\nCounter %lu -> %lu\nBoot counter %lu\n\nPower-cycle to verify\nreport persistence.", "NVS 写入/读取: %s\n计数 %lu -> %lu\n开机次数 %lu\n\n重启后核对报告保留"), esp_err_to_name(e), (unsigned long)before, (unsigned long)after, (unsigned long)boots);
    return e;
}
static esp_err_t sleep_test(unsigned mode)
{
    lab_notice(LAB_TEXT("Sleep starts in 1 second.\nRelease all buttons.\nHold OK to cancel.", "1 秒后进入休眠\n请松开所有按键\n长按 OK 取消"));
    vTaskDelay(pdMS_TO_TICKS(1000));
    if (lab_cancelled()) return ESP_OK;
    if (mode == 0) {
        esp_err_t e=esp_sleep_enable_timer_wakeup(2000000);
        if (e != ESP_OK) return e;
        if (audio_state == ESP_OK && (e=bsp_audio_sleep()) != ESP_OK) return e;
        bsp_display_backlight(0);
        int64_t start=esp_timer_get_time(); e=esp_light_sleep_start();
        (void)esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_TIMER);
        esp_err_t wake=audio_state == ESP_OK ? bsp_audio_wake() : ESP_OK;
        bsp_display_backlight(75);
        lab_notice(LAB_TEXT("Light sleep: %s\nElapsed %lld ms\nAudio wake: %s\n\nNo current measurement.", "浅睡: %s\n耗时 %lld ms\n音频唤醒: %s\n\n不测量电流"), esp_err_to_name(e), (long long)((esp_timer_get_time()-start)/1000), esp_err_to_name(wake));
        return e == ESP_OK ? wake : e;
    }
    int mv=bsp_button_read_mv();
    if (mv < BSP_BTN_MV_RELEASED_MIN) return ESP_ERR_INVALID_STATE;
    esp_err_t e=esp_sleep_enable_timer_wakeup(10000000);
    if (e != ESP_OK) return e;
    if (audio_state == ESP_OK && (e=bsp_audio_sleep()) != ESP_OK) return e;
    // Terminal handoff: no further UI, ADC or codec access until reset/wakeup.
    atomic_store(&terminal_sleep, true);
    int level=0;
    e=bsp_button_prepare_deep_sleep(&level);
    if (e == ESP_OK && level) e=esp_deep_sleep_enable_gpio_wakeup(1ULL<<BSP_BTN_GPIO, ESP_GPIO_WAKEUP_GPIO_LOW);
    else if (e == ESP_OK) e=ESP_ERR_INVALID_STATE;
    if (e == ESP_OK && battery_state == ESP_OK) e=bsp_battery_sleep();
    if (e == ESP_OK && audio_state == ESP_OK) e=bsp_audio_prepare_deep_sleep();
    if (e == ESP_OK) e=bsp_i2c_prepare_deep_sleep();
    if (e == ESP_OK && !bsp_lvgl_lock(1000)) e=ESP_ERR_TIMEOUT;
    if (e == ESP_OK) e=bsp_display_prepare_deep_sleep();
    if (e != ESP_OK) {
        ESP_LOGE("ppdemo", "Terminal sleep handoff failed: %s; reboot required", esp_err_to_name(e));
        esp_restart();
    }
    sleep_marker=0x50505731;
    esp_deep_sleep_start();
    return ESP_FAIL;
}
static void io_worker(void *arg)
{
    (void)arg;
    job_t job;
    for (;;) {
        if (xQueueReceive(jobs, &job, portMAX_DELAY) != pdTRUE) continue;
        esp_err_t e=ESP_OK;
        ESP_LOGI("ppdemo", "Test %s action %u start; heap=%lu", lab_name(job.test), job.action, (unsigned long)esp_get_free_heap_size());
        switch (job.test) {
        case LAB_AUDIO: e=audio_test(job.action); break;
        case LAB_WIFI:
        case LAB_BLE:
            free(clip); clip=NULL; clip_samples=0;
            e=job.test == LAB_WIFI ? network_state : ESP_OK;
            if (e == ESP_OK) e=job.test == LAB_WIFI ? lab_wifi_test() : lab_ble_test();
            break;
        case LAB_STORAGE: e=storage_test(); break;
        case LAB_SLEEP: e=sleep_test(job.action); break;
        case LAB_BATTERY: {
            e=battery_state;
            int soc=e == ESP_OK ? bsp_battery_soc() : -1;
            int mv=e == ESP_OK ? bsp_battery_mv() : -1;
            if (soc<0 || mv<0) e=ESP_FAIL;
            lab_notice(LAB_TEXT("Fuel gauge: %s\nSOC %d %%\nVoltage %d mV\n\nGauge may be absent.\nNo calibrated current\nor runtime claim.", "电量计: %s\n电量 %d %%\n电压 %d mV\n\n部分设备无电量计\n不测电流与续航"), esp_err_to_name(e), soc, mv);
            break;
        }
        default: e=ESP_ERR_NOT_SUPPORTED; break;
        }
        ESP_LOGI("ppdemo", "Test %s action %u return=%s cancelled=%d heap=%lu", lab_name(job.test), job.action, esp_err_to_name(e), lab_cancelled(), (unsigned long)esp_get_free_heap_size());
        event_t done={.kind=EV_DONE, .error=e};
        xQueueSend(events, &done, portMAX_DELAY);
    }
}
static void console(void *arg)
{
    (void)arg;
    char line[32]; size_t n=0; bool overflow=false;
    for (;;) {
        int ch=getchar();
        if (ch == EOF) { vTaskDelay(pdMS_TO_TICKS(50)); clearerr(stdin); continue; }
        if (ch=='\r' || ch=='\n') {
            line[n]=0;
            if (!overflow && (!strcmp(line,"report") || !strcmp(line,"help"))) {
                event_t event={.kind=EV_REPORT}; xQueueSend(events,&event,portMAX_DELAY);
            }
            n=0; overflow=false;
        } else if (n+1<sizeof(line)) line[n++]=(char)ch;
        else overflow=true;
    }
}
static unsigned action_count(void)
{
    if (selected == LAB_DISPLAY) return 10;
    if (selected == LAB_AUDIO) return 3;
    if (selected == LAB_SLEEP) return 2;
    return 1;
}
static const char *action_name(void)
{
    static const char *display[]={LAB_TEXT("Alignment", "边缘对齐"), LAB_TEXT("Red", "红色"), LAB_TEXT("Green", "绿色"), LAB_TEXT("Blue", "蓝色"), LAB_TEXT("White", "白色"), LAB_TEXT("Black", "黑色"), LAB_TEXT("Gray", "灰色"), LAB_TEXT("Color bars", "彩色条"), LAB_TEXT("Gray steps", "灰阶"), LAB_TEXT("Backlight 25%", "背光 25%")};
    static const char *audio[]={LAB_TEXT("Play test tones", "播放测试音"), LAB_TEXT("Record 3 seconds", "录音 3 秒"), LAB_TEXT("Play recording", "回放录音")};
    if (selected==LAB_DISPLAY) return display[action];
    if (selected==LAB_AUDIO) return audio[action];
    if (selected==LAB_SLEEP) return action ? LAB_TEXT("Deep sleep 10s", "深睡 10 秒") : LAB_TEXT("Light sleep 2s", "浅睡 2 秒");
    if (selected==LAB_WIFI) return LAB_TEXT("Start Wi-Fi test", "开始 Wi-Fi 测试");
    if (selected==LAB_BLE) return LAB_TEXT("Start BLE echo", "开始蓝牙回读");
    return LAB_TEXT("Run check", "开始检查");
}
static void render(void)
{
    if (atomic_load(&terminal_sleep)) return;
    char meta[128], body[768];
    snprintf(meta,sizeof(meta),"%02u / %02u   %s",selected+1,LAB_COUNT,lab_ui_result(record.results[selected]));
    if (view==HOME) {
        snprintf(body,sizeof(body),LAB_TEXT("%s\n\n%u / %u marked PASS\nBoot %lu\n\nOK: open test\nHold OK: report", "%s\n\n通过 %u / %u 项\n开机 %lu 次\n\n单击 OK 开始\n长按 OK 查看报告"),lab_ui_name(selected),lab_record_passed(&record),LAB_COUNT,(unsigned long)boots);
        lab_ui_show(LAB_TEXT("Hardware checks", "硬件体验"),meta,body,LAB_TEXT("UP / DOWN: select", "上/下选择项目"),0);
    } else if (view==REVIEW) {
        lab_ui_show(lab_ui_name(selected),LAB_TEXT("YOUR OBSERVATION", "请按实际体验评价"),LAB_TEXT("UP: mark PASS\nDOWN: mark FAIL\nOK: leave untested\n\nDriver return codes\nnever auto-grade.\nHold OK: go back", "上键: 通过\n下键: 不通过\nOK: 未测试\n\n不会自动判定结果\n长按 OK 返回"),LAB_TEXT("Saved in local NVS", "结果保存在本机"),0);
    } else if (view==REPORT) {
        size_t n=0;
        for (unsigned i=0;i<LAB_COUNT;i++) n+=(size_t)snprintf(body+n,sizeof(body)-n,"%s: %s\n",lab_ui_name(i),lab_ui_result(record.results[i]));
        lab_ui_show(LAB_TEXT("Manual report", "测试报告"),LAB_TEXT("OK: USB JSON EXPORT", "OK 导出 USB JSON"),body,LAB_TEXT("Hold OK: home", "长按 OK 返回首页"),0);
    } else if (selected==LAB_DISPLAY) {
        lab_ui_show(LAB_TEXT("Display", "屏幕"),action_name(),action==0 ? LAB_TEXT("Check edge alignment,\ntext and rounded corners.\n\nUP / DOWN: pattern\nOK: next pattern", "检查边缘、文字和圆角\n\n上/下切换图案\nOK 选择下一个图案") : "", LAB_TEXT("Hold OK: finish test", "长按 OK 结束测试"),action<9 ? (int)action : 0);
    } else {
        if (selected==LAB_KEYS) {
            snprintf(body,sizeof(body),LAB_TEXT("ADC: %d mV\nPRESS CLICK DBL LONG UP\nU %u %u %u %u %u\nD %u %u %u %u %u\nK %u %u %u %u %u\n\nTry each button / hold.", "ADC: %d mV\n按下 单击 双击 长按 松开\n上 %u %u %u %u %u\n下 %u %u %u %u %u\nOK %u %u %u %u %u\n\n请逐个按键和长按"),bsp_button_read_mv(),key_counts[0][0],key_counts[0][1],key_counts[0][2],key_counts[0][3],key_counts[0][4],key_counts[1][0],key_counts[1][1],key_counts[1][2],key_counts[1][3],key_counts[1][4],key_counts[2][0],key_counts[2][1],key_counts[2][2],key_counts[2][3],key_counts[2][4]);
        } else snprintf(body,sizeof(body),"%s",detail);
        lab_ui_show(lab_ui_name(selected),busy ? LAB_TEXT("RUNNING / HOLD OK TO STOP", "运行中 / 长按 OK 停止") : action_name(),body,LAB_TEXT("Hold OK: finish test", "长按 OK 结束测试"),0);
    }
}
static void enter_test(void)
{
    view=TEST; action=0;
    snprintf(detail,sizeof(detail),LAB_TEXT("UP / DOWN: select action\nOK: start\n\nHold OK: review result", "上/下选择动作\nOK 开始\n\n长按 OK 评价结果"));
    if (selected==LAB_KEYS) memset(key_counts,0,sizeof(key_counts));
    if (selected==LAB_USB) snprintf(detail,sizeof(detail),LAB_TEXT("USB Serial / JTAG\n115200 8N1\n\nType report + Enter\nto export JSON.\n\nNot a USB sound card.", "USB 串口 / JTAG\n115200 8N1\n\n输入 report 并回车\n导出 JSON 报告\n\n不是 USB 声卡"));
    if (selected==LAB_NFC) snprintf(detail,sizeof(detail),LAB_TEXT("Passive NTAG213\n\nRead tag with a phone.\nDo not overwrite tag.\n\nNo MCU NFC reader.\nManual result only.", "被动 NTAG213 标签\n\n请用手机读取\n不要改写标签\n\n芯片不能读写 NFC\n由你手动评价"));
}
static void handle_key(const event_t *event)
{
    if ((unsigned)event->key<3 && (unsigned)event->action<5) key_counts[event->key][event->action]++;
    if (event->key==BSP_BTN_OK && event->action==BSP_BTN_LONG) {
        ignore_click_until=esp_timer_get_time()+400000;
        if (view==TEST) {
            if (busy) { atomic_store(&cancel_job,true); review_pending=true; }
            else { bsp_display_backlight(75); view=REVIEW; }
        } else view=view==HOME ? REPORT : HOME;
        return;
    }
    if (event->action!=BSP_BTN_CLICK || esp_timer_get_time()<ignore_click_until || busy) return;
    if (view==HOME) {
        if (event->key==BSP_BTN_OK) enter_test();
        else selected=(unsigned)lab_wrap((int)selected,event->key==BSP_BTN_UP ? -1 : 1,LAB_COUNT);
    } else if (view==REVIEW) {
        lab_record_t before=record;
        lab_record_set(&record,selected,event->key==BSP_BTN_UP ? LAB_PASS : event->key==BSP_BTN_DOWN ? LAB_FAIL : LAB_UNTESTED);
        esp_err_t e=save_record();
        if (e!=ESP_OK) { record=before; view=TEST; snprintf(detail,sizeof(detail),LAB_TEXT("Report NOT saved: %s\nNo grade changed.", "报告未保存: %s\n原评价保持不变"),esp_err_to_name(e)); }
        else view=HOME;
    } else if (view==REPORT) {
        if (event->key==BSP_BTN_OK) export_report();
    } else if (selected!=LAB_KEYS && selected!=LAB_USB && selected!=LAB_NFC) {
        if (event->key!=BSP_BTN_OK) action=(unsigned)lab_wrap((int)action,event->key==BSP_BTN_UP ? -1 : 1,(int)action_count());
        else if (selected==LAB_DISPLAY) action=(action+1)%action_count();
        else {
            if (atomic_load(&restart_required)) {
                snprintf(detail,sizeof(detail),LAB_TEXT("Resource cleanup unconfirmed.\nRestart device before\nstarting another test.", "资源清理未确认\n请先重启设备\n再开始其他测试")); return;
            }
            atomic_store(&cancel_job,false);
            job_t job={.test=selected,.action=action};
            if (xQueueSend(jobs,&job,0)==pdTRUE) busy=true;
        }
        if (selected==LAB_DISPLAY) bsp_display_backlight(action==9 ? 25 : 75);
    }
}
void app_main(void)
{
    events=xQueueCreate(20,sizeof(event_t)); jobs=xQueueCreate(1,sizeof(job_t));
    if (!events || !jobs) { ESP_LOGE("ppdemo","Queue allocation failed"); return; }
    init_storage();
    network_state=esp_netif_init();
    if (network_state==ESP_OK) network_state=esp_event_loop_create_default();
    esp_err_t e=bsp_display_init();
    if (e!=ESP_OK || !bsp_lvgl_init() || !lab_ui_init()) { ESP_LOGE("ppdemo","Display initialization failed: %s",esp_err_to_name(e)); return; }
    bsp_display_backlight(75);
    audio_state=bsp_audio_init(); battery_state=bsp_battery_init();
    if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_GPIO) {
        wake_guard_deadline=esp_timer_get_time()+10000000;
        atomic_store(&wake_guard,true);
    }
    e=bsp_button_init(button,NULL);
    if (e==ESP_OK && bsp_button_read_mv() >= BSP_BTN_MV_RELEASED_MIN)
        atomic_store(&wake_guard,false);
    if (e!=ESP_OK) snprintf(detail,sizeof(detail),LAB_TEXT("Buttons unavailable: %s", "按键不可用: %s"),esp_err_to_name(e));
    if (xTaskCreate(io_worker,"pp_io",6144,NULL,3,NULL)!=pdPASS || xTaskCreate(console,"pp_console",3072,NULL,2,NULL)!=pdPASS) {
        lab_ui_show(LAB_TEXT("Startup failed", "启动失败"),LAB_TEXT("OUT OF MEMORY", "内存不足"),LAB_TEXT("Restart device.\nNo tests started.", "请重启设备\n尚未开始测试"),LAB_TEXT("USB log has details", "详情见 USB 日志"),0); return;
    }
    if (sleep_marker==0x50505731) {
        sleep_marker=0; selected=LAB_SLEEP; view=TEST;
        snprintf(detail,sizeof(detail),LAB_TEXT("Deep sleep wake cause: %d\n\nVerify display, keys\nand audio after wake.\nNo automatic PASS.", "深睡唤醒原因: %d\n\n请检查屏幕、按键\n和声音是否正常\n不会自动判定通过"),(int)esp_sleep_get_wakeup_cause());
    }
    ESP_LOGI("ppdemo","Firmware %s; audio=%s battery=%s. USB: type report",esp_app_get_description()->version,esp_err_to_name(audio_state),esp_err_to_name(battery_state));
    render();
    for (;;) {
        event_t event;
        if (atomic_load(&terminal_sleep)) { vTaskDelay(pdMS_TO_TICKS(100)); continue; }
        if (xQueueReceive(events,&event,pdMS_TO_TICKS(100))!=pdTRUE) continue;
        if (event.kind==EV_KEY) handle_key(&event);
        else if (event.kind==EV_NOTICE) snprintf(detail,sizeof(detail),"%s",event.text);
        else if (event.kind==EV_REPORT) export_report();
        else if (event.kind==EV_DONE) {
            busy=false;
            if (atomic_load(&restart_required)) snprintf(detail,sizeof(detail),LAB_TEXT("Cleanup unconfirmed: %s\nRestart device.\nNo new tests accepted.", "清理未确认: %s\n请重启设备\n暂停接受新测试"),esp_err_to_name(event.error));
            else if (event.error!=ESP_OK) snprintf(detail,sizeof(detail),LAB_TEXT("Driver returned: %s\n\nNo automatic FAIL.\nCheck USB logs / wiring.", "驱动返回: %s\n\n不会自动判定失败\n请检查日志与接线"),esp_err_to_name(event.error));
            else if (lab_cancelled()) snprintf(detail,sizeof(detail),LAB_TEXT("Test stopped.\nManual grade unchanged.", "测试已停止\n原评价保持不变"));
            if (review_pending) { view=REVIEW; review_pending=false; }
        }
        render();
    }
}

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
    char text[224];
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
static char detail[224] = "Driver evidence is separate\nfrom your PASS / FAIL grade.";

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
    if (e != ESP_OK) { snprintf(detail, sizeof(detail), "NVS unavailable: %s\nNo erase attempted.", esp_err_to_name(e)); return; }
    e = nvs_open("ppdemo", NVS_READWRITE, &storage);
    if (e != ESP_OK) return;
    storage_open = true;
    size_t n = sizeof(record);
    lab_record_t saved;
    e = nvs_get_blob(storage, "record", &saved, &n);
    if (e == ESP_OK && n == sizeof(saved) && lab_record_valid(&saved)) record = saved;
    else if (e != ESP_ERR_NVS_NOT_FOUND)
        snprintf(detail, sizeof(detail), "Saved report invalid.\nRAM report starts empty.\nNo erase attempted.");
    (void)nvs_get_u32(storage, "boots", &boots);
    if (boots < UINT32_MAX) boots++;
    if (nvs_set_u32(storage, "boots", boots) == ESP_OK) (void)nvs_commit(storage);
}
static void export_report(void)
{
    char json[1100];
    int n = snprintf(json, sizeof(json), "{\"app\":\"PPDemo\",\"version\":\"%s\",\"boots\":%lu,\"manual_grades\":{",
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
            lab_notice("Tone %u Hz\nVolume 45%%\n\nListen for clean sound.", tones[tone]);
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
        lab_notice("Recording 3 seconds...\nSpeak normally.\n\nRAM only. Never uploaded.");
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
        lab_notice("Recording %s\nPeak %u / 32768\nClipped samples %u\n\nSelect playback to listen.", clip ? "complete" : "discarded", peak, clipped);
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
    lab_notice("NVS commit/read: %s\nCounter %lu -> %lu\nBoot counter %lu\n\nPower-cycle to verify\nreport persistence.", esp_err_to_name(e), (unsigned long)before, (unsigned long)after, (unsigned long)boots);
    return e;
}
static esp_err_t sleep_test(unsigned mode)
{
    lab_notice("Sleep starts in 1 second.\nRelease all buttons.\nHold OK to cancel.");
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
        lab_notice("Light sleep: %s\nElapsed %lld ms\nAudio wake: %s\n\nNo current measurement.", esp_err_to_name(e), (long long)((esp_timer_get_time()-start)/1000), esp_err_to_name(wake));
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
            lab_notice("Fuel gauge: %s\nSOC %d %%\nVoltage %d mV\n\nGauge may be absent.\nNo calibrated current\nor runtime claim.", esp_err_to_name(e), soc, mv);
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
    static const char *display[]={"Alignment", "Red", "Green", "Blue", "White", "Black", "Gray", "Color bars", "Gray steps", "Backlight 25%"};
    static const char *audio[]={"Play test tones", "Record 3 seconds", "Play recording"};
    if (selected==LAB_DISPLAY) return display[action];
    if (selected==LAB_AUDIO) return audio[action];
    if (selected==LAB_SLEEP) return action ? "Deep sleep 10s" : "Light sleep 2s";
    if (selected==LAB_WIFI) return "Start Wi-Fi test";
    if (selected==LAB_BLE) return "Start BLE echo";
    return "Run check";
}
static void render(void)
{
    if (atomic_load(&terminal_sleep)) return;
    char meta[80], body[256];
    snprintf(meta,sizeof(meta),"%02u / %02u   %s",selected+1,LAB_COUNT,lab_result_name(record.results[selected]));
    if (view==HOME) {
        snprintf(body,sizeof(body),"%s\n\n%u / %u marked PASS\nBoot %lu\n\nOK: open test\nHold OK: report",lab_name(selected),lab_record_passed(&record),LAB_COUNT,(unsigned long)boots);
        lab_ui_show("Hardware checks",meta,body,"UP / DOWN: select",0);
    } else if (view==REVIEW) {
        lab_ui_show(lab_name(selected),"YOUR OBSERVATION","UP: mark PASS\nDOWN: mark FAIL\nOK: leave untested\n\nDriver return codes\nnever auto-grade.\nHold OK: go back","Saved in local NVS",0);
    } else if (view==REPORT) {
        size_t n=0;
        for (unsigned i=0;i<LAB_COUNT;i++) n+=(size_t)snprintf(body+n,sizeof(body)-n,"%s: %s\n",lab_name(i),lab_result_name(record.results[i]));
        lab_ui_show("Manual report","OK: USB JSON EXPORT",body,"Hold OK: home",0);
    } else if (selected==LAB_DISPLAY) {
        lab_ui_show("Display",action_name(),action==0 ? "Check edge alignment,\ntext and rounded corners.\n\nUP / DOWN: pattern\nOK: next pattern" : "", "Hold OK: finish test",action<9 ? (int)action : 0);
    } else {
        if (selected==LAB_KEYS) {
            snprintf(body,sizeof(body),"ADC: %d mV\nPRESS CLICK DBL LONG UP\nU %u %u %u %u %u\nD %u %u %u %u %u\nK %u %u %u %u %u\n\nTry each button / hold.",bsp_button_read_mv(),key_counts[0][0],key_counts[0][1],key_counts[0][2],key_counts[0][3],key_counts[0][4],key_counts[1][0],key_counts[1][1],key_counts[1][2],key_counts[1][3],key_counts[1][4],key_counts[2][0],key_counts[2][1],key_counts[2][2],key_counts[2][3],key_counts[2][4]);
        } else snprintf(body,sizeof(body),"%s",detail);
        lab_ui_show(lab_name(selected),busy ? "RUNNING / HOLD OK TO STOP" : action_name(),body,"Hold OK: finish test",0);
    }
}
static void enter_test(void)
{
    view=TEST; action=0;
    snprintf(detail,sizeof(detail),"UP / DOWN: select action\nOK: start\n\nHold OK: review result");
    if (selected==LAB_KEYS) memset(key_counts,0,sizeof(key_counts));
    if (selected==LAB_USB) snprintf(detail,sizeof(detail),"USB Serial / JTAG\n115200 8N1\n\nType report + Enter\nto export JSON.\n\nNot a USB sound card.");
    if (selected==LAB_NFC) snprintf(detail,sizeof(detail),"Passive NTAG213\n\nRead tag with a phone.\nDo not overwrite tag.\n\nNo MCU NFC reader.\nManual result only.");
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
        if (e!=ESP_OK) { record=before; view=TEST; snprintf(detail,sizeof(detail),"Report NOT saved: %s\nNo grade changed.",esp_err_to_name(e)); }
        else view=HOME;
    } else if (view==REPORT) {
        if (event->key==BSP_BTN_OK) export_report();
    } else if (selected!=LAB_KEYS && selected!=LAB_USB && selected!=LAB_NFC) {
        if (event->key!=BSP_BTN_OK) action=(unsigned)lab_wrap((int)action,event->key==BSP_BTN_UP ? -1 : 1,(int)action_count());
        else if (selected==LAB_DISPLAY) action=(action+1)%action_count();
        else {
            if (atomic_load(&restart_required)) {
                snprintf(detail,sizeof(detail),"Resource cleanup unconfirmed.\nRestart device before\nstarting another test."); return;
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
    if (e!=ESP_OK) snprintf(detail,sizeof(detail),"Buttons unavailable: %s",esp_err_to_name(e));
    if (xTaskCreate(io_worker,"pp_io",6144,NULL,3,NULL)!=pdPASS || xTaskCreate(console,"pp_console",3072,NULL,2,NULL)!=pdPASS) {
        lab_ui_show("Startup failed","OUT OF MEMORY","Restart device.\nNo tests started.","USB log has details",0); return;
    }
    if (sleep_marker==0x50505731) {
        sleep_marker=0; selected=LAB_SLEEP; view=TEST;
        snprintf(detail,sizeof(detail),"Deep sleep wake cause: %d\n\nVerify display, keys\nand audio after wake.\nNo automatic PASS.",(int)esp_sleep_get_wakeup_cause());
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
            if (atomic_load(&restart_required)) snprintf(detail,sizeof(detail),"Cleanup unconfirmed: %s\nRestart device.\nNo new tests accepted.",esp_err_to_name(event.error));
            else if (event.error!=ESP_OK) snprintf(detail,sizeof(detail),"Driver returned: %s\n\nNo automatic FAIL.\nCheck USB logs / wiring.",esp_err_to_name(event.error));
            else if (lab_cancelled()) snprintf(detail,sizeof(detail),"Test stopped.\nManual grade unchanged.");
            if (review_pending) { view=REVIEW; review_pending=false; }
        }
        render();
    }
}

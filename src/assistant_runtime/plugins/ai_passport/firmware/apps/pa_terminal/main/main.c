#include "bsp_audio.h"
#include "bsp_battery.h"
#include "bsp_button.h"
#include "bsp_display.h"
#include "bsp_i2c.h"
#include "bsp_pins.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "network.h"
#include "nvs_flash.h"
#include "settings.h"
#include "terminal_model.h"
#include "ui.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    unsigned generation;
    size_t size;
    unsigned char pcm[1024];
} audio_t;
typedef struct {
    int kind, key, event;
    char *text;
} event_t;
static pp_settings_t settings;
static pp_button_t button_state;
static QueueHandle_t events, recorded, playback;
static atomic_bool capture, audio_busy, playing, play_end, audio_stop, failed;
static atomic_uint capture_generation, play_generation, credited;
static bool recording, ready, finishing, waiting, menu, configured, sleep_failed, notice_list,
    notice_view;
static unsigned sequence, menu_index, next_offset, segment_end, page_offset, page_next,
    notice_index;
static uint32_t activity;
static char request_id[37], active_id[64], message_id[160], pending[37], body[1024],
    status[160] = "连接中";
static const char *choices[] = {"继续听",   "查看任务结果", "取消当前执行",           "音量 +10",
                                "音量 -10", "网络状态",     "切换自动休眠／保持在线", "休眠",
                                "返回"};
static char notice_cursor[160];
static bool notice_older;
static char notices[8][160], notice_ids[8][65];
static unsigned notice_revisions[8];
static int notice_count;

static uint32_t now_ms(void) { return (uint32_t)(esp_timer_get_time() / 1000); }
static cJSON *command(const char *type) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "type", type);
    return j;
}
static bool send_id(const char *type, const char *id) {
    cJSON *j = command(type);
    cJSON_AddStringToObject(j, "request_id", id);
    return pp_net_send(j);
}
static void set_status(const char *text) { pp_text(status, sizeof(status), text); }
static void stop_play(void) {
    playing = false;
    play_end = false;
    play_generation++;
    xQueueReset(playback);
    pp_net_send(command("playback.stop"));
}
static void abort_record(void) {
    capture = false;
    capture_generation++;
    recording = false;
    finishing = false;
    ready = false;
    xQueueReset(recorded);
    if (request_id[0])
        send_id("input.cancel", request_id);
    set_status("收音中断，请重新说");
}
static void key_event(bsp_btn_t key, bsp_btn_ev_t action, void *arg) {
    (void)arg;
    if (action != BSP_BTN_PRESS && action != BSP_BTN_RELEASE)
        return;
    event_t e = {.kind = 1, .key = key, .event = action};
    if (xQueueSend(events, &e, 0) != pdTRUE)
        failed = true;
}
void pp_received(const char *text, size_t bytes) {
    if (bytes > 16384) {
        failed = true;
        return;
    }
    char *copy = malloc(bytes + 1);
    if (!copy) {
        failed = true;
        return;
    }
    memcpy(copy, text, bytes);
    copy[bytes] = 0;
    event_t e = {.kind = 2, .text = copy};
    if (xQueueSend(events, &e, 0) != pdTRUE) {
        free(copy);
        failed = true;
    }
}
void pp_network_state(bool online, const char *reason) {
    event_t e = {.kind = 3, .event = online, .text = strdup(reason)};
    if (xQueueSend(events, &e, 0) != pdTRUE) {
        free(e.text);
        failed = true;
    }
}
bool pp_play_pcm(uint32_t generation, const void *pcm, size_t bytes) {
    if (generation != play_generation || !playing)
        return true;
    const unsigned char *p = pcm;
    while (bytes) {
        audio_t a = {.generation = generation, .size = bytes > 1024 ? 1024 : bytes};
        memcpy(a.pcm, p, a.size);
        if (xQueueSend(playback, &a, 0) != pdTRUE)
            return false;
        p += a.size;
        bytes -= a.size;
    }
    return true;
}
static void audio_worker(void *arg) {
    (void)arg;
    unsigned rate = 0;
    for (;;) {
        if (audio_stop) {
            audio_busy = false;
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        if (capture) {
            audio_busy = true;
            unsigned generation = capture_generation;
            if (rate != 16000) {
                if (bsp_audio_set_format(16000, 16, 1) != ESP_OK) {
                    failed = true;
                    capture = false;
                    continue;
                }
                rate = 16000;
            }
            audio_t audio = {.generation = generation, .size = 640};
            if (bsp_audio_read(audio.pcm, audio.size) != ESP_OK) {
                failed = true;
                capture = false;
            } else if (generation == capture_generation &&
                       xQueueSend(recorded, &audio, 0) != pdTRUE) {
                failed = true;
                capture = false;
            }
        } else {
            audio_t a;
            if (xQueueReceive(playback, &a, pdMS_TO_TICKS(10))) {
                audio_busy = true;
                if (playing && a.generation == play_generation) {
                    if (rate != 24000) {
                        if (bsp_audio_set_format(24000, 16, 1) != ESP_OK) {
                            failed = true;
                            playing = false;
                            continue;
                        }
                        rate = 24000;
                    }
                    bsp_audio_set_volume(settings.volume);
                    if (bsp_audio_write(a.pcm, a.size) != ESP_OK) {
                        failed = true;
                        playing = false;
                    } else if (a.generation == play_generation)
                        atomic_fetch_add(&credited, a.size);
                }
            } else
                audio_busy = false;
        }
    }
}
static void make_id(char *out) {
    unsigned char b[16];
    esp_fill_random(b, sizeof(b));
    b[6] = (b[6] & 15) | 64;
    b[8] = (b[8] & 63) | 128;
    snprintf(out, 37, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x", b[0],
             b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13],
             b[14], b[15]);
}
static void actions(unsigned mask) {
    if (mask & PP_STOP)
        stop_play();
    if (mask & PP_CAPTURE) {
        if (recording || finishing || !pp_net_connected()) {
            button_state.down = false;
            return;
        }
        capture_generation++;
        xQueueReset(recorded);
        capture = true;
        set_status("正在听");
    }
    if (mask & PP_DISCARD) {
        capture = false;
        capture_generation++;
        xQueueReset(recorded);
        set_status("就绪");
    }
    if (mask & PP_BEGIN) {
        if (!capture || recording)
            return;
        make_id(request_id);
        if (pp_pending(request_id) != ESP_OK) {
            abort_record();
            return;
        }
        cJSON *j = command("input.begin");
        cJSON_AddStringToObject(j, "request_id", request_id);
        cJSON_AddStringToObject(j, "mode", settings.queue ? "queue" : "steer");
        if (!pp_net_send(j)) {
            abort_record();
            return;
        }
        sequence = 0;
        recording = true;
        ready = true;
    }
    if (mask & PP_FINISH) {
        capture = false;
        finishing = true;
        set_status("识别中");
    }
    if (mask & PP_TOGGLE) {
        settings.queue = !settings.queue;
        if (pp_preference("mode", settings.queue) != ESP_OK)
            set_status("模式保存失败");
        else
            set_status(settings.queue ? "已切换排队" : "已切换引导");
    }
    if (mask & PP_MENU) {
        menu = true;
        menu_index = 0;
    }
}
static const char *string(cJSON *j, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}
static unsigned number(cJSON *j, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsNumber(v) && v->valuedouble >= 0 ? (unsigned)v->valuedouble : 0;
}
static void credit(unsigned count) {
    if (!count)
        return;
    cJSON *j = command("playback.credit");
    cJSON_AddNumberToObject(j, "generation", play_generation);
    cJSON_AddNumberToObject(j, "bytes", count);
    if (!pp_net_send(j))
        stop_play();
}
static void notice_command(const char *type, const char *action, unsigned offset) {
    if (!notice_count || notice_index >= (unsigned)notice_count)
        return;
    cJSON *j = command(type);
    cJSON_AddStringToObject(j, "id", notice_ids[notice_index]);
    cJSON_AddNumberToObject(j, "revision", notice_revisions[notice_index]);
    if (action)
        cJSON_AddStringToObject(j, "action", action);
    else
        cJSON_AddNumberToObject(j, "offset", offset);
    pp_net_send(j);
}
static void incoming(char *raw) {
    cJSON *j = cJSON_Parse(raw);
    if (!j) {
        set_status("消息格式错误");
        return;
    }
    const char *type = string(j, "type");
    if (!strcmp(type, "hello")) {
        set_status("就绪");
        pp_text(settings.nickname, sizeof(settings.nickname), string(j, "nickname"));
        if (pp_pending_read(pending, sizeof(pending)) == ESP_OK && pending[0])
            send_id("input.receipt", pending);
    } else if (!strcmp(type, "input.ready") && !strcmp(string(j, "request_id"), request_id))
        ready = true;
    else if (!strcmp(type, "transcript.partial")) {
        if (recording)
            pp_text(body, sizeof(body), string(j, "text"));
    } else if (!strcmp(type, "transcript")) {
        pp_text(body, sizeof(body), string(j, "text"));
        set_status("处理中");
    } else if (!strcmp(type, "input.receipt")) {
        const char *state = string(j, "state");
        if (!strcmp(state, "accepted")) {
            pp_pending("");
            waiting = true;
            set_status("处理中");
        } else if (!strcmp(state, "prepared") || !strcmp(state, "cancelled")) {
            pp_pending("");
            set_status("输入未执行，请重新说");
        } else if (!strcmp(state, "unknown")) {
            set_status("提交结果待确认");
            waiting = false;
        } else {
            set_status("输入目标已变化，请重新说");
            waiting = false;
        }
    } else if (!strcmp(type, "state")) {
        pp_text(active_id, sizeof(active_id), string(j, "active_request_id"));
        waiting = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "task_running"));
        if (string(j, "question_id")[0]) {
            set_status("等待回答");
            pp_text(body, sizeof(body), string(j, "questions"));
            waiting = false;
        } else if (waiting && !recording && !playing)
            set_status("处理中");
    } else if (!strcmp(type, "message") || !strcmp(type, "message.page")) {
        notice_view = notice_list = false;
        pp_text(message_id, sizeof(message_id), string(j, "message_id"));
        pp_text(body, sizeof(body), string(j, "text"));
        page_offset = number(j, "offset");
        page_next = number(j, "next_offset");
        waiting = false;
        set_status("回复");
    } else if (!strcmp(type, "playback.stop")) {
        playing = false;
        play_end = false;
        play_generation = number(j, "generation");
        credited = 0;
        xQueueReset(playback);
    } else if (!strcmp(type, "playback.start")) {
        if (capture) {
            pp_net_send(command("playback.stop"));
        } else {
            play_generation = number(j, "generation");
            credited = 0;
            playing = true;
            play_end = false;
            next_offset = number(j, "offset");
            segment_end = number(j, "next_offset");
            credit(8192);
            set_status("播放中");
        }
    } else if (!strcmp(type, "playback.end") && number(j, "generation") == play_generation)
        play_end = true;
    else if (!strcmp(type, "notifications")) {
        cJSON *items = cJSON_GetObjectItemCaseSensitive(j, "items"), *i;
        notice_count = 0;
        cJSON *before = cJSON_GetObjectItemCaseSensitive(j, "before");
        notice_older = cJSON_IsArray(before);
        notice_cursor[0] = 0;
        if (notice_older) {
            char *raw = cJSON_PrintUnformatted(before);
            if (raw) {
                pp_text(notice_cursor, sizeof(notice_cursor), raw);
                free(raw);
            }
        }
        cJSON_ArrayForEach(i, items) {
            if (notice_count >= 8)
                break;
            pp_text(notices[notice_count], 160, string(i, "title"));
            pp_text(notice_ids[notice_count], 65, string(i, "id"));
            notice_revisions[notice_count] = number(i, "revision");
            notice_count++;
        }
        if (notice_index >= (unsigned)notice_count)
            notice_index = 0;
        for (unsigned k = 0; k < (unsigned)notice_count; k++) {
            cJSON *r = command("notification.receipt");
            cJSON_AddStringToObject(r, "id", notice_ids[k]);
            cJSON_AddNumberToObject(r, "revision", notice_revisions[k]);
            cJSON_AddStringToObject(r, "action", "received");
            pp_net_send(r);
        }
    } else if (!strcmp(type, "notification.page")) {
        notice_list = false;
        notice_view = true;
        pp_text(body, sizeof(body), string(j, "text"));
        page_offset = number(j, "offset");
        page_next = number(j, "next_offset");
        set_status("任务结果");
    } else if (!strcmp(type, "execution.cancelled")) {
        set_status("已收到取消结果");
        waiting = false;
    } else if (!strcmp(type, "error") || !strcmp(type, "playback.unavailable")) {
        pp_text(body, sizeof(body), string(j, "code"));
        set_status("请查看错误说明");
        waiting = false;
        if (recording)
            abort_record();
    }
    cJSON_Delete(j);
}
static void sleep_now(void) {
    if (sleep_failed || capture || recording || waiting || playing ||
        bsp_button_read_mv() < BSP_BTN_MV_RELEASED_MIN)
        return;
    audio_stop = true;
    for (unsigned i = 0; i < 100 && audio_busy; i++)
        vTaskDelay(pdMS_TO_TICKS(10));
    if (audio_busy) {
        audio_stop = false;
        set_status("音频尚未停止");
        return;
    }
    sleep_failed = true;
    pp_net_stop();
    int level = 0;
    if (bsp_audio_sleep() != ESP_OK || bsp_button_prepare_deep_sleep(&level) != ESP_OK || !level ||
        esp_deep_sleep_enable_gpio_wakeup(1ULL << BSP_BTN_GPIO, ESP_GPIO_WAKEUP_GPIO_LOW) !=
            ESP_OK ||
        bsp_battery_sleep() != ESP_OK || bsp_audio_prepare_deep_sleep() != ESP_OK ||
        bsp_i2c_prepare_deep_sleep() != ESP_OK || !bsp_lvgl_lock(1000) ||
        bsp_display_prepare_deep_sleep() != ESP_OK) {
        set_status("休眠交接失败，请重启");
        return;
    }
    esp_deep_sleep_start();
}
static void choose(void) {
    menu = false;
    if (menu_index == 0 && notice_view) {
        notice_command("notification.listen", NULL, next_offset);
    } else if (menu_index == 0 && message_id[0]) {
        cJSON *j = command("playback.continue");
        cJSON_AddStringToObject(j, "message_id", message_id);
        cJSON_AddNumberToObject(j, "offset", next_offset);
        pp_net_send(j);
    } else if (menu_index == 1) {
        notice_list = true;
        notice_view = false;
        notice_index = 0;
        set_status("任务结果");
        pp_net_send(command("notifications.list"));
    } else if (menu_index == 2) {
        if (active_id[0])
            send_id("execution.cancel", active_id);
        else
            set_status("没有当前执行");
    } else if (menu_index == 3 || menu_index == 4) {
        settings.volume = menu_index == 3 ? (settings.volume > 90 ? 100 : settings.volume + 10)
                                          : (settings.volume < 10 ? 0 : settings.volume - 10);
        pp_preference("volume", settings.volume);
        snprintf(body, sizeof(body), "音量 %u", settings.volume);
    } else if (menu_index == 5) {
        snprintf(body, sizeof(body), "%s\n空闲内存 %u\n最大连续内存 %u",
                 pp_net_connected() ? "连接正常" : "离线", (unsigned)esp_get_free_heap_size(),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    } else if (menu_index == 6) {
        settings.keep_online = !settings.keep_online;
        if (pp_preference("online", settings.keep_online) != ESP_OK)
            set_status("设置保存失败");
        else
            set_status(settings.keep_online ? "保持在线" : "空闲后自动休眠");
    } else if (menu_index == 7)
        sleep_now();
    else if (menu_index == 8) {
        notice_list = notice_view = false;
    }
}
void app_main(void) {
    esp_err_t e = nvs_flash_init();
    if (e != ESP_OK) {
        ESP_LOGE("pa_terminal", "NVS unavailable; no automatic erase");
        return;
    }
    events = xQueueCreate(8, sizeof(event_t));
    recorded = xQueueCreate(32, sizeof(audio_t));
    playback = xQueueCreate(8, sizeof(audio_t));
    if (!events || !recorded || !playback || !pp_ui_init())
        return;
    pp_usb_start();
    configured = pp_settings_load(&settings) == ESP_OK;
    if (!configured) {
        strcpy(settings.nickname, "PA 终端");
        set_status("等待 USB 配置");
        strcpy(body, "在 Windows 浏览器打开固件网站，连接设备并写入配对信息。");
    }
    if (bsp_audio_init() != ESP_OK) {
        set_status("音频初始化失败");
        configured = false;
    }
    (void)bsp_battery_init();
    button_state.wake_guard = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_GPIO;
    ESP_ERROR_CHECK(bsp_button_init(key_event, NULL));
    if (bsp_button_read_mv() >= BSP_BTN_MV_RELEASED_MIN)
        button_state.wake_guard = false;
    if (xTaskCreate(audio_worker, "pp_audio", 4096, NULL, 4, NULL) != pdPASS)
        return;
    if (configured)
        pp_net_start(&settings);
    activity = now_ms();
    uint32_t last_render = 0, last_memory = 0;
    for (;;) {
        if (sleep_failed) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        event_t event;
        if (xQueueReceive(events, &event, pdMS_TO_TICKS(10)) == pdTRUE) {
            if (event.kind == 1) {
                activity = now_ms();
                if (event.key == BSP_BTN_OK) {
                    if (notice_list && !menu) {
                        if (event.event == BSP_BTN_RELEASE) {
                            if (notice_index == (unsigned)notice_count && notice_older) {
                                cJSON *j = command("notifications.list");
                                cJSON_AddItemToObject(j, "before", cJSON_Parse(notice_cursor));
                                pp_net_send(j);
                                notice_index = 0;
                            } else if (notice_count) {
                                next_offset = 0;
                                notice_command("notification.page", NULL, 0);
                                notice_command("notification.receipt", "read", 0);
                            } else
                                notice_list = false;
                        }
                    } else if (menu) {
                        if (event.event == BSP_BTN_RELEASE)
                            choose();
                    } else
                        actions(event.event == BSP_BTN_PRESS
                                    ? pp_press(&button_state, now_ms(), playing)
                                    : pp_release(&button_state, now_ms()));
                } else if (event.event == BSP_BTN_RELEASE) {
                    if (notice_list && !menu) {
                        unsigned count = notice_count + (notice_older ? 1 : 0);
                        if (count)
                            notice_index =
                                (notice_index + (event.key == BSP_BTN_UP ? count - 1 : 1)) % count;
                    } else if (menu)
                        menu_index = (menu_index + (event.key == BSP_BTN_UP ? 8 : 1)) % 9;
                    else if (message_id[0] || notice_view) {
                        unsigned offset = event.key == BSP_BTN_UP
                                              ? (page_offset < 80 ? 0 : page_offset - 80)
                                              : page_next;
                        if (event.key != BSP_BTN_UP && !offset)
                            continue;
                        if (notice_view)
                            notice_command("notification.page", NULL, offset);
                        else {
                            cJSON *j = command("message.page");
                            cJSON_AddStringToObject(j, "message_id", message_id);
                            cJSON_AddNumberToObject(j, "offset", offset);
                            pp_net_send(j);
                        }
                    }
                }
            } else if (event.kind == 2) {
                incoming(event.text);
                free(event.text);
            } else {
                if (!event.event) {
                    abort_record();
                    stop_play();
                    waiting = false;
                }
                set_status(event.text ? event.text : "网络错误");
                free(event.text);
            }
        }
        if (!menu && !notice_list)
            actions(pp_tick(&button_state, now_ms()));
        if (failed) {
            failed = false;
            abort_record();
            stop_play();
            set_status("缓冲或音频错误，未重试");
        }
        if (recording && ready) {
            audio_t audio;
            unsigned n = 0;
            while (n++ < 4 && xQueueReceive(recorded, &audio, 0)) {
                if (audio.generation != capture_generation)
                    continue;
                if (!pp_net_pcm(sequence++, audio.pcm, audio.size)) {
                    abort_record();
                    break;
                }
            }
            if (finishing && !audio_busy && uxQueueMessagesWaiting(recorded) == 0) {
                cJSON *j = command("input.finish");
                cJSON_AddStringToObject(j, "request_id", request_id);
                cJSON_AddNumberToObject(j, "frames", sequence);
                if (!pp_net_send(j))
                    set_status("提交待核对");
                else
                    waiting = true;
                recording = finishing = ready = false;
            }
        }
        unsigned consumed = atomic_exchange(&credited, 0);
        if (playing && consumed)
            credit(consumed);
        if (playing && play_end && !audio_busy && uxQueueMessagesWaiting(playback) == 0) {
            cJSON *j = command("playback.completed");
            cJSON_AddNumberToObject(j, "generation", play_generation);
            pp_net_send(j);
            next_offset = segment_end;
            playing = false;
            activity = now_ms();
            set_status("回复");
        }
        if (now_ms() - last_render >= 100) {
            last_render = now_ms();
            pp_ui_show(settings.nickname, status,
                       menu ? choices[menu_index]
                            : (notice_list
                                   ? (notice_older && notice_index == (unsigned)notice_count
                                          ? "更早的任务结果"
                                          : (notice_count ? notices[notice_index] : "没有任务结果"))
                                   : body),
                       (menu || notice_list) ? "上下选择 · OK 确定" : "按住 OK 说话",
                       settings.queue);
        }
        if (configured && !settings.keep_online &&
            now_ms() - activity > settings.sleep_seconds * 1000)
            sleep_now();
        if (now_ms() - last_memory > 60000) {
            last_memory = now_ms();
            ESP_LOGI("pa_terminal", "heap=%u largest=%u low=%u", (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
                     (unsigned)esp_get_minimum_free_heap_size());
        }
    }
}

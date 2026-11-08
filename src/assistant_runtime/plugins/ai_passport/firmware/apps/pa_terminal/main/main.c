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
#include "provision.h"
#include "esp_system.h"
#include <sys/time.h>
#include "nvs_flash.h"
#include "settings.h"
#include "navigation.h"
#include "notification_model.h"
#include "terminal_model.h"
#include "text_layout.h"
#include "timeline.h"
#include "history_cache.h"
#include "pictures.h"
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
    unsigned generation;
    size_t size;
    unsigned char pcm[640];
} recording_t;
typedef struct {
    int kind, key, event;
    char *text;
} event_t;
static pp_settings_t settings;
static pp_button_t button_state;
static pp_input_t input_state;
static pp_navigation_t navigation;
#define menu pp_navigation_menu(&navigation)
#define browsing (navigation.screen == PP_SCREEN_HISTORY)
#define notice_list (navigation.screen == PP_SCREEN_TASKS)
#define notice_view (navigation.screen == PP_SCREEN_TASK)
static QueueHandle_t events, keys, recorded, playback;
static atomic_bool capture, audio_busy, playing, play_end, audio_stop, failed;
static atomic_bool key_overflow;
static atomic_uint capture_generation, play_generation, credited;
static atomic_uint target_volume;
static atomic_uint input_level;
static pp_ui_view_t view = PP_UI_INFO;
static size_t view_page, view_next;
static bool message_streaming;
static const char *message_origin = "回复";
static pp_timeline_t timeline;
static unsigned offline_skips;
static int offline_direction = -1;
static bool new_message, page_pending, page_tail, index_boundary;
static int index_direction;
static char latest_message[PP_MESSAGE_ID], requested_page[PP_MESSAGE_ID],
    history_anchor[PP_MESSAGE_ID];
static unsigned requested_offset, message_total;
static char image_ids[4][33];
static unsigned image_count;
static unsigned image_index;
static unsigned review_choice, review_offset, review_next, review_requested;
static bool review_reading, review_pending;
static uint32_t preference_changed, volume_feedback_at;
static bool waiting, configured, sleep_failed, confirm_send, speech_session, audio_available,
    dangerous_confirm;
static unsigned dangerous_action;
static cJSON *pending_configuration;
static unsigned sequence, next_offset, segment_end, page_offset, page_next, notice_index;
static uint32_t activity;
static char request_id[37], active_id[64], message_id[160], pending[37], body[1024],
    status[160] = "连接中";
static const char *review_choices[] = {"丢弃本次输入", "发送已识别文字", "查看完整转写"};
static char notice_cursor[160];
static bool notice_older;
static char notices[8][160], notice_ids[8][65];
static unsigned notice_revisions[8];
static int notice_count;
static pp_notice_read_t notice_read;
static char active_notice_title[64], dismissed_input[37];
static char question_text[1024];

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
static void request_review_page(unsigned offset) {
    if (review_pending)
        return;
    cJSON *j = command("input.transcript");
    cJSON_AddStringToObject(j, "request_id", request_id);
    cJSON_AddNumberToObject(j, "offset", offset);
    if (pp_net_send(j)) {
        review_pending = true;
        review_requested = offset;
        set_status("正在读取转写");
    } else {
        set_status("离线，连网后可查看");
    }
}
static bool close_image(void) {
    if (!pp_picture_close())
        return false;
    if (view == PP_UI_IMAGE)
        view = PP_UI_REPLY;
    return true;
}
static void open_image(unsigned index) {
    if (settings.show_images && index < image_count &&
        pp_picture_open(message_id, image_ids[index])) {
        image_index = index;
        view = PP_UI_IMAGE;
    }
}
static void request_page(const char *id, unsigned offset, bool tail);
static void show_history_page(const pp_history_page_t *page) {
    if (page_tail && !page->image_only && page->next && pp_net_connected()) {
        request_page(page->id, (page->total - 1) / 80 * 80, false);
        return;
    }
    if (!close_image())
        return;
    pp_text(message_id, sizeof(message_id), page->id);
    pp_text(body, sizeof(body), page->text);
    memcpy(image_ids, page->images, sizeof(image_ids));
    image_count = page->image_count;
    page_offset = page->offset;
    page_next = page->next;
    message_total = page->total;
    view_page = page_tail && !page->image_only ? pp_ui_previous(body, strlen(body)) : 0;
    view_next = 0;
    message_origin = page->user ? "你说的" : "回复";
    message_streaming = false;
    view = PP_UI_REPLY;
    set_status(!pp_net_connected() ? "本地缓存" : browsing ? "历史记录" : "回复");
    if (page->image_only && page->image_count)
        open_image(0);
}
static void request_page(const char *id, unsigned offset, bool tail) {
    page_tail = tail;
    pp_text(requested_page, sizeof(requested_page), id);
    requested_offset = offset;
    page_pending = true;
    if (pp_history_cache_page(id, offset))
        set_status(pp_net_connected() ? "正在读取" : "正在读取本地记录");
    else {
        page_pending = false;
        set_status("读取忙，请稍后重试");
    }
}
static void request_index(const char *cursor, int direction) {
    cJSON *j = command("history.index");
    cJSON_AddNumberToObject(j, "limit", PP_TIMELINE_ENTRIES);
    if (cursor)
        cJSON_AddStringToObject(j, direction < 0 ? "before" : "after", cursor);
    if (!pp_net_send(j)) {
        index_direction = 0;
        set_status("无法读取历史，请联网后重试");
    }
}
static void history_step(int direction) {
    if (page_pending || index_direction) {
        set_status("正在读取，请稍候");
        return;
    }
    if (!pp_net_connected()) {
        const pp_history_entry_t *entry = pp_timeline_neighbor(&timeline, message_id, direction);
        if (settings.offline_cache && entry) {
            offline_skips = 0;
            offline_direction = direction;
            request_page(entry->id, 0, false);
        } else
            set_status("已到最近缓存边界，联网后查看更多");
        return;
    }
    const pp_history_entry_t *next = pp_timeline_neighbor(&timeline, message_id, direction);
    pp_navigation_open(&navigation, PP_SCREEN_HISTORY);
    if (next) {
        request_page(next->id, 0, direction < 0);
        return;
    }
    const pp_history_entry_t *anchor = pp_timeline_entry(&timeline, message_id);
    if (!pp_net_connected()) {
        set_status("更早记录需联网读取");
        return;
    }
    pp_text(history_anchor, sizeof(history_anchor), message_id);
    index_direction = direction;
    index_boundary = anchor != NULL;
    request_index(anchor ? anchor->cursor : NULL, direction);
}
static void history_response(cJSON *j) {
    if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "pushed")) &&
        (browsing || index_direction))
        return; /* A live directory refresh cannot steal an explicit history read. */
    if (!pp_timeline_index(&timeline, j)) {
        index_direction = 0;
        set_status("历史目录格式不匹配");
        return;
    }
    if (!index_direction)
        return;
    int direction = index_direction;
    const pp_history_entry_t *next =
        index_boundary ? NULL : pp_timeline_neighbor(&timeline, history_anchor, direction);
    const pp_history_entry_t *anchor = pp_timeline_entry(&timeline, history_anchor);
    if (!next && anchor && !index_boundary) {
        index_boundary = true;
        request_index(anchor->cursor, direction);
        return;
    }
    if (!next && timeline.count)
        next = &timeline.entries[direction < 0 ? timeline.count - 1 : 0];
    index_direction = 0;
    if (next)
        request_page(next->id, 0, direction < 0);
    else
        set_status(direction < 0 ? "已到最早记录" : "已到最新记录");
}
static void stop_play(void) {
    playing = false;
    speech_session = false;
    play_end = false;
    play_generation++;
    xQueueReset(playback);
    pp_net_send(command("playback.stop"));
}
static void abort_record(void) {
    capture = false;
    capture_generation++;
    bool pending_input = input_state.phase != PP_INPUT_IDLE;
    pp_input_abort(&input_state);
    review_reading = review_pending = false;
    button_state = (pp_button_t){.wake_guard = button_state.wake_guard || button_state.down};
    xQueueReset(recorded);
    if (pending_input && request_id[0])
        send_id("input.cancel", request_id);
    set_status("收音中断，请重新说");
}
static void input_limit_reached(bool discard_excess) {
    capture = false;
    button_state = (pp_button_t){.wake_guard = button_state.down || button_state.wake_guard};
    if (discard_excess) {
        capture_generation++;
        xQueueReset(recorded);
    }
    set_status("达到上限，识别中");
    ESP_LOGI("pa_terminal", "input_limit audio_bytes=%u max_audio_bytes=%u elapsed_ms=%u",
             (unsigned)input_state.audio_bytes, (unsigned)input_state.max_audio_bytes,
             (unsigned)(now_ms() - input_state.started));
}
static const char *input_failure_hint(const char *code) {
    if (!strcmp(code, "speech_input_gap"))
        return "音频不连续，本次未发送，请重新说。";
    if (!strcmp(code, "speech_invalid_pcm"))
        return "音频格式异常，本次未发送。";
    if (!strcmp(code, "speech_input_limit"))
        return "音频超出上限，本次未发送。";
    if (!strcmp(code, "speech_input_deadline"))
        return "收音超时，本次未发送，请重新说。";
    return NULL;
}
static void home(const char *hint) {
    if (!settings.paired || pp_provision_active())
        return;
    /* Only unfinished local input is discarded. Accepted/unknown dispatches keep
     * their durable request receipt; navigation is not task cancellation. */
    if (input_state.phase != PP_INPUT_IDLE && input_state.phase != PP_INPUT_SUBMITTING &&
        input_state.phase != PP_INPUT_UNKNOWN) {
        pp_text(dismissed_input, sizeof(dismissed_input), request_id);
        abort_record();
    }
    stop_play();
    if (!close_image())
        return;
    pp_navigation_open(&navigation, PP_SCREEN_HOME);
    button_state = (pp_button_t){.wake_guard = button_state.down || button_state.wake_guard};
    dangerous_confirm = confirm_send = notice_read.pending = page_pending = false;
    index_direction = 0;
    review_reading = review_pending = false;
    message_streaming = false;
    view_page = view_next = 0;
    body[0] = 0;
    view = PP_UI_IDLE;
    if (hint)
        new_message = false;
    set_status(hint                                       ? hint
               : input_state.phase == PP_INPUT_UNKNOWN    ? "上次输入待核对"
               : input_state.phase == PP_INPUT_SUBMITTING ? "上次输入提交中"
               : waiting                                  ? "后台处理中"
                                                          : "就绪");
}
static unsigned notice_choices(void) {
    return (unsigned)notice_count + (notice_older ? 1u : 0u) + 1u;
}
static void show_notices(void) {
    stop_play();
    close_image();
    pp_navigation_open(&navigation, PP_SCREEN_TASKS);
    notice_index = 0;
    notice_read.pending = false;
    set_status(notice_count ? "选择任务查看" : "暂无任务消息");
    pp_net_send(command("notifications.list"));
}
static void key_event(bsp_btn_t key, bsp_btn_ev_t action, void *arg) {
    (void)arg;
    if (action != BSP_BTN_PRESS && action != BSP_BTN_RELEASE)
        return;
    /* Independent from network/UI backlog. No release can leave the producer
       running; the controller still drains the final in-flight PCM block. */
    if (key == BSP_BTN_OK && action == BSP_BTN_RELEASE)
        capture = false;
    event_t e = {.kind = 1, .key = key, .event = action};
    if (xQueueSend(keys, &e, 0) != pdTRUE)
        key_overflow = true;
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
    unsigned applied_volume = UINT32_MAX;
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
                applied_volume = UINT32_MAX;
            }
            if (applied_volume != 0) {
                if (bsp_audio_set_volume(0) != ESP_OK)
                    failed = true;
                applied_volume = 0;
            }
            recording_t audio = {.generation = generation, .size = 640};
            if (bsp_audio_read(audio.pcm, audio.size) != ESP_OK) {
                failed = true;
                capture = false;
            } else if (generation == capture_generation &&
                       xQueueSend(recorded, &audio, 0) != pdTRUE) {
                failed = true;
                capture = false;
            }
            /* Forty integer samples per 20ms block; no FFT or extra audio copy. */
            unsigned amplitude = 0;
            for (unsigned i = 0; i < audio.size / 2; i += 8) {
                int sample =
                    (int16_t)((unsigned)audio.pcm[i * 2] | (unsigned)audio.pcm[i * 2 + 1] << 8);
                amplitude += sample < 0 ? -sample : sample;
            }
            atomic_store(&input_level, amplitude / 40 / 12);
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
                        applied_volume = UINT32_MAX;
                    }
                    unsigned volume = atomic_load(&target_volume);
                    if (applied_volume != volume) {
                        if (bsp_audio_set_volume(volume) != ESP_OK) {
                            failed = true;
                            playing = false;
                            continue;
                        }
                        applied_volume = volume;
                    }
                    if (bsp_audio_write(a.pcm, a.size) != ESP_OK) {
                        failed = true;
                        playing = false;
                    } else if (a.generation == play_generation)
                        atomic_fetch_add(&credited, a.size);
                }
            } else {
                if (!playing && applied_volume != 0 && rate) {
                    if (bsp_audio_set_volume(0) != ESP_OK)
                        failed = true;
                    applied_volume = 0;
                }
                audio_busy = false;
            }
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
        pp_disk_allow_writes(false);
        if (!audio_available) {
            set_status("音频不可用");
            return;
        }
        if (!pp_net_connected()) {
            set_status("未连接 PA");
            return;
        }
        if (!pp_input_start(&input_state, now_ms())) {
            set_status(input_state.phase == PP_INPUT_UNKNOWN ? "上次输入待核对"
                                                             : "请先结束当前输入");
            return;
        }
        capture_generation++;
        xQueueReset(recorded);
        request_id[0] = 0;
        capture = true;
        /* Prebuffer immediately, but do not expose a recording state until the
         * hold is established. A double click must leave the page unchanged. */
    }
    if (mask & PP_DISCARD) {
        capture = false;
        capture_generation++;
        xQueueReset(recorded);
        if (input_state.phase == PP_INPUT_PREBUFFER) {
            pp_input_abort(&input_state);
            set_status("就绪");
        }
    }
    if (mask & PP_BEGIN) {
        if (!capture || !pp_input_begin(&input_state))
            return;
        make_id(request_id);
        dismissed_input[0] = 0;
        if (!close_image()) {
            abort_record();
            return;
        }
        pp_net_send(command("image.close"));
        view = PP_UI_INPUT;
        view_page = view_next = 0;
        pp_navigation_open(&navigation, PP_SCREEN_CHAT);
        new_message = false;
        page_pending = false;
        index_direction = 0;
        body[0] = 0;
        review_choice = review_offset = review_next = 0;
        review_reading = review_pending = false;
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
        pp_text(pending, sizeof(pending), request_id);
        sequence = 0;
        set_status("正在听");
    }
    if (mask & PP_FINISH) {
        capture = false;
        if (input_state.phase == PP_INPUT_OPENING || input_state.phase == PP_INPUT_RECORDING) {
            pp_input_release(&input_state);
            set_status("识别中");
        }
    }
    if (mask & PP_TOGGLE) {
        if (pp_preference("mode", !settings.queue) != ESP_OK)
            set_status("模式保存失败");
        else
            set_status(settings.queue ? "已切换排队" : "已切换引导");
    }
    if (mask & PP_MENU) {
        if (!settings.paired || pp_provision_active())
            return;
        stop_play();
        if (browsing) {
            pp_navigation_open(&navigation, PP_SCREEN_CHAT);
            new_message = false;
            page_pending = false;
            index_direction = 0;
            if (latest_message[0])
                request_page(latest_message, 0, false);
            else
                view = PP_UI_IDLE;
            return;
        }
        pp_navigation_open(&navigation, PP_SCREEN_MENU);
        set_status("请选择");
    }
}
static const char *string(cJSON *j, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}
static unsigned number(cJSON *j, const char *key) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsNumber(v) && v->valuedouble >= 0 && v->valuedouble <= UINT32_MAX &&
                   v->valuedouble == (unsigned)v->valuedouble
               ? (unsigned)v->valuedouble
               : 0;
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
static void request_notice_page(unsigned offset) {
    if (!notice_view && (!notice_count || notice_index >= (unsigned)notice_count))
        return;
    const char *id = notice_view ? notice_read.id : notice_ids[notice_index];
    unsigned revision = notice_view ? notice_read.revision : notice_revisions[notice_index];
    if (!pp_notice_begin(&notice_read, id, revision, offset, now_ms()))
        return;
    if (!notice_view)
        pp_text(active_notice_title, sizeof(active_notice_title), notices[notice_index]);
    cJSON *j = command("notification.page");
    cJSON_AddStringToObject(j, "id", notice_read.id);
    cJSON_AddNumberToObject(j, "revision", notice_read.revision);
    cJSON_AddNumberToObject(j, "offset", notice_read.offset);
    if (!pp_net_send(j)) {
        notice_read.pending = false;
        set_status("任务读取未发送，请重试");
    } else {
        ESP_LOGI("pa_terminal", "notice_read requested offset=%u", offset);
        set_status("正在读取任务");
    }
}
static void incoming(char *raw) {
    cJSON *j = cJSON_Parse(raw);
    if (!j) {
        set_status("消息格式错误");
        return;
    }
    if (pp_history_cache_receive(j)) {
        cJSON_Delete(j);
        return;
    }
    pp_history_page_t decoded;
    const char *type = string(j, "type");
    if ((!strcmp(type, "playback.pending") || !strcmp(type, "playback.start")) &&
        !pp_navigation_audio(&navigation)) {
        unsigned incoming_generation = number(j, "generation");
        if (incoming_generation >= play_generation)
            play_generation = incoming_generation;
        stop_play();
        cJSON_Delete(j);
        return;
    }
    if (!strcmp(type, "hello")) {
        if (number(j, "input_rate") != 16000 || number(j, "output_rate") != 24000 ||
            pp_settings_binding(string(j, "binding")) != ESP_OK ||
            !pp_net_accept_protocol(number(j, "protocol"))) {
            set_status("协议或设备绑定不匹配，请检查服务版本");
            cJSON_Delete(j);
            return;
        }
        pp_settings_snapshot(&settings);
        pp_history_cache_bind(settings.url, settings.device, settings.binding,
                              settings.offline_cache, settings.show_images);
        playing = false;
        speech_session = false;
        play_generation = 0;
        page_pending = false;
        index_direction = 0;
        xQueueReset(playback);
        set_status(audio_available ? "就绪" : "音频不可用");
        if (!browsing && input_state.phase == PP_INPUT_IDLE) {
            body[0] = 0;
            view = PP_UI_IDLE;
            pp_navigation_open(&navigation, PP_SCREEN_HOME);
            new_message = false;
        }
        cJSON *clock = cJSON_GetObjectItemCaseSensitive(j, "server_time");
        if (cJSON_IsNumber(clock) && clock->valuedouble > 1700000000 &&
            clock->valuedouble < 4102444800.0) {
            struct timeval time = {.tv_sec = (time_t)clock->valuedouble};
            settimeofday(&time, NULL);
        }
        if (pp_pending_read(pending, sizeof(pending)) == ESP_OK && pending[0]) {
            pp_text(request_id, sizeof(request_id), pending);
            input_state.phase = PP_INPUT_UNKNOWN;
            view = PP_UI_INFO;
            set_status("正在核对上次输入");
            send_id("input.receipt", pending);
        }
    } else if (!strcmp(type, "setup")) {
        view = PP_UI_INFO;
        set_status("连接设备热点");
        snprintf(body, sizeof(body), "%s\n密码 %s\n\n手机打开\n%s", string(j, "ssid"),
                 string(j, "password"), string(j, "address"));
    } else if (!strcmp(type, "pairing")) {
        view = PP_UI_INFO;
        set_status("等待配对");
        snprintf(body, sizeof(body), "%s\n\n在 PA 后台输入这八位数字\n配对码十分钟有效",
                 string(j, "code"));
    } else if (!strcmp(type, "configuration")) {
        cJSON_Delete(pending_configuration);
        pending_configuration = cJSON_DetachItemFromObjectCaseSensitive(j, "fields");
    } else if (!strcmp(type, "cache.restored")) {
        if (!pp_net_connected() && settings.offline_cache && pp_timeline_index(&timeline, j) &&
            timeline.count && browsing) {
            offline_skips = 0;
            offline_direction = -1;
            request_page(timeline.entries[timeline.count - 1].id, 0, false);
        }
    } else if (!strcmp(type, "cache.unavailable")) {
        if (page_pending && !strcmp(string(j, "message_id"), requested_page) &&
            number(j, "offset") == requested_offset) {
            page_pending = false;
            if (!pp_net_connected() && !requested_offset && ++offline_skips < timeline.count) {
                const pp_history_entry_t *entry =
                    pp_timeline_neighbor(&timeline, requested_page, offline_direction);
                if (entry) {
                    request_page(entry->id, 0, false);
                    cJSON_Delete(j);
                    return;
                }
            }
            set_status("此处尚未缓存，联网后查看");
        }
    } else if (!strcmp(type, "cache.empty")) {
        if (!pp_net_connected() && browsing) {
            view = PP_UI_IDLE;
            body[0] = 0;
            set_status("最近记录尚未缓存，请联网同步");
        }
    } else if (!strcmp(type, "history.index")) {
        history_response(j);
    } else if (pp_picture_receive(j)) {
        view = pp_picture_active() ? PP_UI_IMAGE : PP_UI_REPLY;
    } else if (!strcmp(type, "playback.pending")) {
        if (number(j, "generation") >= play_generation) {
            play_generation = number(j, "generation");
            speech_session = true;
        }
    } else if (!strcmp(type, "playback.finished")) {
        if (number(j, "generation") == play_generation) {
            speech_session = false;
            set_status("回复");
        }
    } else if (!strcmp(type, "input.ready") && !strcmp(string(j, "request_id"), request_id)) {
        if (input_state.phase == PP_INPUT_OPENING &&
            !pp_input_ready(&input_state, input_state.generation, number(j, "max_audio_bytes"))) {
            abort_record();
            set_status("识别限制不匹配，本次未发送");
        }
    } else if (!strcmp(type, "transcript.partial")) {
        if (!strcmp(string(j, "request_id"), request_id) &&
            (input_state.phase == PP_INPUT_RECORDING || input_state.phase == PP_INPUT_OPENING))
            pp_text_suffix(body, sizeof(body), string(j, "text"));
    } else if (!strcmp(type, "transcript")) {
        if (!strcmp(string(j, "request_id"), request_id) &&
            pp_input_transcript(&input_state, input_state.generation)) {
            pp_text_suffix(body, sizeof(body), string(j, "text"));
            confirm_send = false;
            set_status(input_state.phase == PP_INPUT_CONFIRM ? "达到上限，尚未发送" : "处理中");
            review_choice = review_offset = review_next = 0;
            review_reading = review_pending = false;
        }
    } else if (!strcmp(type, "input.confirmation")) {
        if (!strcmp(string(j, "request_id"), request_id) && input_state.phase == PP_INPUT_CONFIRM)
            set_status("达到上限，确认后才发送");
    } else if (!strcmp(type, "input.transcript")) {
        if (review_pending && review_reading && input_state.phase == PP_INPUT_CONFIRM &&
            !strcmp(string(j, "request_id"), request_id) &&
            number(j, "offset") == review_requested) {
            review_pending = false;
            review_offset = number(j, "offset");
            review_next = number(j, "next_offset");
            pp_text(body, sizeof(body), string(j, "text"));
            view_page = view_next = 0;
        }
    } else if (!strcmp(type, "input.receipt")) {
        const char *ident = string(j, "request_id");
        if (strcmp(ident, request_id) && strcmp(ident, pending)) {
            cJSON_Delete(j);
            return;
        }
        const char *state = string(j, "state");
        if (!strcmp(state, "review")) {
            capture = false;
            capture_generation++;
            xQueueReset(recorded);
            pp_text(request_id, sizeof(request_id), ident);
            pp_input_restore_confirmation(&input_state);
            button_state =
                (pp_button_t){.wake_guard = button_state.wake_guard || button_state.down};
            review_choice = review_offset = review_next = 0;
            review_reading = review_pending = false;
            waiting = false;
            set_status("输入未发送，请确认");
        } else if (!strcmp(state, "accepted")) {
            pp_pending("");
            pending[0] = 0;
            pp_input_done(&input_state, input_state.generation);
            review_reading = review_pending = false;
            waiting = true;
            set_status("处理中");
        } else if (!strcmp(state, "prepared") || !strcmp(state, "cancelled")) {
            pp_pending("");
            pending[0] = 0;
            pp_input_abort(&input_state);
            if (strcmp(ident, dismissed_input))
                home("输入未执行，请重新说");
        } else if (!strcmp(state, "unknown")) {
            capture = false;
            input_state.phase = PP_INPUT_UNKNOWN;
            set_status("提交结果待确认");
            waiting = false;
        } else {
            pp_input_abort(&input_state);
            set_status("输入目标已变化，请重新说");
            waiting = false;
        }
    } else if (!strcmp(type, "state")) {
        pp_text(active_id, sizeof(active_id), string(j, "active_request_id"));
        waiting = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "task_running"));
        pp_text(question_text, sizeof(question_text),
                string(j, "question_id")[0] ? string(j, "questions") : "");
        if (question_text[0] && navigation.screen == PP_SCREEN_CHAT &&
            input_state.phase == PP_INPUT_IDLE) {
            pp_navigation_open(&navigation, PP_SCREEN_QUESTION);
            view = PP_UI_REPLY;
            view_page = view_next = 0;
            set_status("等待回答");
            waiting = false;
        } else if (!question_text[0] && navigation.screen == PP_SCREEN_QUESTION) {
            pp_navigation_open(&navigation, PP_SCREEN_CHAT);
            view_page = view_next = 0;
            body[0] = 0;
            set_status(waiting ? "处理中" : "就绪");
        } else if (waiting && input_state.phase == PP_INPUT_IDLE && !playing)
            set_status("处理中");
    } else if (!strcmp(type, "message.page")) {
        if (page_pending && !strcmp(string(j, "message_id"), requested_page) &&
            number(j, "offset") == requested_offset) {
            page_pending = false;
            pp_history_page_t *page = pp_timeline_decode(&decoded, j);
            if (page)
                show_history_page(page);
            else
                set_status("正文格式不匹配，未替换现有页面");
        }
    } else if (!strcmp(type, "message")) {
        pp_text(latest_message, sizeof(latest_message), string(j, "message_id"));
        pp_history_page_t *page = pp_timeline_decode(&decoded, j);
        if (navigation.screen != PP_SCREEN_CHAT ||
            (view == PP_UI_INPUT && input_state.phase != PP_INPUT_IDLE)) {
            if (!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "restored")))
                new_message = true;
            cJSON_Delete(j);
            return;
        }
        if (!close_image()) {
            cJSON_Delete(j);
            return;
        }
        view = PP_UI_REPLY;
        view_page = view_next = 0;
        message_streaming = !strcmp(string(j, "status"), "streaming");
        message_origin = !strcmp(string(j, "role"), "user") ? "你说的" : "回复";
        pp_text(message_id, sizeof(message_id), string(j, "message_id"));
        pp_text(body, sizeof(body), string(j, "text"));
        page_offset = number(j, "offset");
        page_next = number(j, "next_offset");
        message_total = number(j, "total");
        image_count = 0;
        if (page) {
            memcpy(image_ids, page->images, sizeof(image_ids));
            image_count = page->image_count;
        }
        waiting = false;
        set_status("回复");
        if (page && page->image_only && image_count) {
            pp_navigation_open(&navigation, PP_SCREEN_HISTORY);
            open_image(0);
        }
    } else if (!strcmp(type, "playback.stop")) {
        if (number(j, "generation") < play_generation) {
            cJSON_Delete(j);
            return;
        }
        playing = false;
        speech_session = false;
        play_end = false;
        play_generation = number(j, "generation");
        credited = 0;
        xQueueReset(playback);
    } else if (!strcmp(type, "playback.start")) {
        if (number(j, "generation") < play_generation) {
            cJSON_Delete(j);
            return;
        }
        if (capture) {
            pp_net_send(command("playback.stop"));
        } else {
            play_generation = number(j, "generation");
            credited = 0;
            playing = true;
            speech_session = true;
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
        int result = (notice_list || notice_view) ? pp_notice_page(&notice_read, j) : 0;
        if (result <= 0) {
            if (result < 0)
                set_status("任务内容格式不匹配，请重试");
            cJSON_Delete(j);
            return;
        }
        ESP_LOGI("pa_terminal", "notice_read received offset=%u", notice_read.offset);
        for (unsigned i = 0; i < (unsigned)notice_count; i++)
            if (!strcmp(notice_ids[i], notice_read.id))
                notice_index = i;
        view = PP_UI_REPLY;
        view_page = view_next = 0;
        message_origin = "任务结果";
        pp_navigation_open(&navigation, PP_SCREEN_TASK);
        pp_text(body, sizeof(body), string(j, "text"));
        page_offset = number(j, "offset");
        page_next = number(j, "next_offset");
        set_status("任务结果");
        cJSON *read = command("notification.receipt");
        cJSON_AddStringToObject(read, "id", notice_read.id);
        cJSON_AddNumberToObject(read, "revision", notice_read.revision);
        cJSON_AddStringToObject(read, "action", "read");
        pp_net_send(read);
    } else if (!strcmp(type, "execution.cancelled")) {
        set_status("已收到取消结果");
        waiting = false;
    } else if (!strcmp(type, "error") || !strcmp(type, "playback.unavailable")) {
        const char *input_hint = input_failure_hint(string(j, "code"));
        if (input_hint && !strcmp(string(j, "request_id"), request_id) &&
            strcmp(request_id, dismissed_input)) {
            pp_text(dismissed_input, sizeof(dismissed_input), request_id);
            waiting = false;
            abort_record();
            home(input_hint);
            cJSON_Delete(j);
            return;
        }
        if (!strcmp(string(j, "code"), "speech_no_transcript") &&
            !strcmp(string(j, "request_id"), request_id)) {
            pp_text(dismissed_input, sizeof(dismissed_input), request_id);
            waiting = false;
            home("未检测到内容，请重新说。");
            cJSON_Delete(j);
            return;
        }
        if (string(j, "request_id")[0] && !strcmp(string(j, "request_id"), dismissed_input)) {
            cJSON_Delete(j);
            return;
        }
        if (!strcmp(string(j, "operation"), "input.transcript")) {
            if (!review_pending) {
                cJSON_Delete(j);
                return;
            }
            review_pending = false;
            set_status("转写页读取失败，可重试");
            cJSON_Delete(j);
            return;
        }
        const char *operation = string(j, "operation");
        bool image_read = !strcmp(operation, "image.open") || !strcmp(operation, "image.chunk");
        if ((!strcmp(operation, "message.page") && !page_pending) ||
            (!strcmp(operation, "history.index") && !index_direction) ||
            (!strcmp(operation, "notification.page") && !notice_read.pending) ||
            (image_read && view != PP_UI_IMAGE)) {
            cJSON_Delete(j);
            return;
        }
        if (!strcmp(operation, "message.page"))
            page_pending = false;
        if (!strcmp(string(j, "operation"), "history.index"))
            index_direction = 0;
        if (!strcmp(operation, "notification.page")) {
            notice_read.pending = false;
            set_status("任务读取失败，请重试");
            cJSON_Delete(j);
            return;
        }
        if (!strcmp(string(j, "operation"), "image.open") ||
            !strcmp(string(j, "operation"), "image.chunk"))
            close_image();
        if (!strcmp(type, "error")) {
            view = PP_UI_INFO;
            pp_navigation_open(&navigation, PP_SCREEN_CHAT);
        }
        const char *code = string(j, "code");
        pp_text(body, sizeof(body),
                !strcmp(code, "terminal_request_failed") ? "请求处理失败，请查看后台诊断。"
                : !strcmp(code, "terminal_input_identity_mismatch")
                    ? "输入状态已变化，请返回主页核对。"
                    : code);
        set_status("请求未完成");
        if (!strcmp(type, "playback.unavailable"))
            stop_play();
        if (string(j, "request_id")[0] && !strcmp(string(j, "request_id"), request_id)) {
            waiting = false;
            if (input_state.phase != PP_INPUT_IDLE)
                abort_record();
        }
    }
    cJSON_Delete(j);
}
static void sleep_now(bool explicit) {
    if (sleep_failed || capture || input_state.phase != PP_INPUT_IDLE || waiting ||
        speech_session || bsp_button_read_mv() < BSP_BTN_MV_RELEASED_MIN) {
        if (explicit)
            set_status("当前交互未结束，暂不休眠");
        return;
    }
    audio_stop = true;
    for (unsigned i = 0; i < 100 && audio_busy; i++)
        vTaskDelay(pdMS_TO_TICKS(10));
    if (audio_busy) {
        audio_stop = false;
        set_status("音频尚未停止");
        return;
    }
    sleep_failed = true;
    if (!pp_net_stop()) {
        set_status("网络未正常停止，请重启");
        return;
    }
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
static void adjust_volume(int delta) {
    int value = (int)atomic_load(&target_volume) + delta;
    atomic_store(&target_volume, value < 0 ? 0 : value > 100 ? 100 : (unsigned)value);
    preference_changed = volume_feedback_at = now_ms();
}
static void choose(void) {
    pp_selection_t action = pp_navigation_selected(&navigation);
    switch (action) {
    case PP_SELECT_TASKS:
        show_notices();
        break;
    case PP_SELECT_VOLUME:
        pp_navigation_open(&navigation, PP_SCREEN_VOLUME);
        view = PP_UI_INFO;
        body[0] = 0;
        set_status("上下调整音量");
        break;
    case PP_SELECT_CANCEL:
        if (active_id[0]) {
            send_id("execution.cancel", active_id);
            set_status("已请求取消");
        } else
            set_status("没有当前执行");
        break;
    case PP_SELECT_SETTINGS:
        pp_navigation_open(&navigation, PP_SCREEN_SETTINGS);
        set_status("选择设置");
        break;
    case PP_SELECT_MAINTENANCE:
        pp_navigation_open(&navigation, PP_SCREEN_MAINTENANCE);
        set_status("维护操作");
        break;
    case PP_SELECT_HOME:
        home(NULL);
        break;
    case PP_SELECT_BACK:
        pp_navigation_back(&navigation);
        set_status("请选择");
        break;
    case PP_SELECT_NETWORK: {
        pp_navigation_open(&navigation, PP_SCREEN_NETWORK);
        view = PP_UI_INFO;
        view_page = view_next = 0;
        char ssid[33] = "未连接";
        int rssi = 0;
        bool wifi = pp_net_wifi_details(ssid, sizeof(ssid), &rssi);
        snprintf(body, sizeof(body), "%s\nWi-Fi %s\n信号 %d dBm\n可用内存 %u\n最大连续块 %u",
                 pp_net_connected() ? "PA 已连接" : "PA 离线", wifi ? ssid : "未连接", rssi,
                 (unsigned)esp_get_free_heap_size(),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
        set_status("网络状态");
        break;
    }
    case PP_SELECT_KEEP_ONLINE:
        if (pp_preference("online", !settings.keep_online) != ESP_OK)
            set_status("设置保存失败");
        else
            set_status(settings.keep_online ? "保持在线已开启" : "空闲后自动休眠");
        break;
    case PP_SELECT_RELOAD:
        set_status(pp_net_send(command("configuration.reload")) ? "正在获取配置"
                                                                : "离线，联网后才能重载");
        break;
    case PP_SELECT_RETRY:
        pp_net_retry();
        set_status("正在重试连接");
        break;
    case PP_SELECT_REPROVISION:
    case PP_SELECT_FACTORY:
        dangerous_action = action;
        dangerous_confirm = true;
        confirm_send = false;
        set_status(action == PP_SELECT_REPROVISION ? "重新配网？" : "恢复出厂？");
        pp_text(body, sizeof(body),
                action == PP_SELECT_REPROVISION
                    ? "将断开连接并开启设备热点，保留账号和本地设置。"
                    : "将移除此设备的配对、Wi-Fi 和本地设置。服务器会话和任务不会被删除。");
        break;
    case PP_SELECT_SLEEP:
        sleep_now(true);
        break;
    default:
        break;
    }
}
static void apply_configuration(void) {
    if (!pending_configuration || capture || audio_busy || speech_session || waiting ||
        input_state.phase != PP_INPUT_IDLE || dangerous_confirm || button_state.down)
        return;
    if (preference_changed) {
        if (pp_preference("volume", atomic_load(&target_volume)) != ESP_OK) {
            set_status("本地设置尚未保存");
            return;
        }
        preference_changed = 0;
    }
    cJSON *receipt = NULL;
    bool reconnect = false;
    esp_err_t e = pp_settings_cloud(pending_configuration, &receipt, &reconnect);
    cJSON_Delete(pending_configuration);
    pending_configuration = NULL;
    if (e != ESP_OK) {
        set_status("云端配置未应用，请检查并重载");
        return;
    }
    atomic_store(&target_volume, settings.volume);
    view_page = 0;
    pp_history_cache_bind(settings.url, settings.device, settings.binding, settings.offline_cache,
                          settings.show_images);
    if (!settings.show_images)
        close_image();
    pp_net_send(receipt);
    if (reconnect) {
        pp_net_reload();
        set_status("网络配置已保存，正在重连");
    }
}
void app_main(void) {
    esp_err_t e = nvs_flash_init();
    if (e != ESP_OK) {
        ESP_LOGE("pa_terminal", "NVS unavailable; no automatic erase");
        return;
    }
    events = xQueueCreate(8, sizeof(event_t));
    keys = xQueueCreate(12, sizeof(event_t));
    recorded = xQueueCreate(32, sizeof(recording_t));
    playback = xQueueCreate(8, sizeof(audio_t));
    if (!events || !keys || !recorded || !playback || !pp_ui_init())
        return;
    pp_picture_init(set_status);
    configured = pp_settings_load(&settings) == ESP_OK;
    pp_history_cache_init(pp_received);
    pp_history_cache_bind(settings.url, settings.device, settings.binding,
                          settings.paired && settings.offline_cache, settings.show_images);
    pp_usb_start();
    target_volume = settings.volume;
    if (!configured) {
        set_status("本地设置读取失败");
        strcpy(body, "未清空存储，请通过 USB 诊断检查设备。");
    }
    audio_available = bsp_audio_init() == ESP_OK;
    if (!audio_available)
        set_status("音频初始化失败");
    (void)bsp_battery_init();
    button_state.wake_guard = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_GPIO;
    ESP_ERROR_CHECK(bsp_button_init(key_event, NULL));
    if (bsp_button_read_mv() >= BSP_BTN_MV_RELEASED_MIN)
        button_state.wake_guard = false;
    if (xTaskCreate(audio_worker, "pp_audio", 4096, NULL, 4, NULL) != pdPASS)
        return;
    if (configured && !pp_net_start())
        set_status("网络初始化失败");
    activity = now_ms();
    uint32_t last_render = 0, last_memory = 0, last_telemetry = 0;
    int battery = -1, wifi_rssi = -127;
    for (;;) {
        if (sleep_failed) {
            pp_ui_frame_t frame = {.view = PP_UI_INFO,
                                   .state = status,
                                   .body = body,
                                   .origin = "设备状态",
                                   .hint = "请重启设备后检查",
                                   .volume = target_volume,
                                   .battery = battery,
                                   .wifi = -127};
            pp_ui_render(&frame);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        event_t event;
        if (atomic_exchange(&key_overflow, false)) {
            navigation.up_down = navigation.up_consumed = false;
            capture = false;
            xQueueReset(keys);
            abort_record();
            set_status("按键事件拥塞，本次收音已取消");
        }
        bool have_event = xQueueReceive(keys, &event, 0) == pdTRUE;
        if (!have_event)
            have_event = xQueueReceive(events, &event, 0) == pdTRUE;
        if (have_event) {
            if (event.kind == 1) {
                activity = now_ms();
                if (button_state.wake_guard) {
                    if (event.event == BSP_BTN_RELEASE)
                        button_state.wake_guard = false;
                    continue;
                }
                if (event.key == BSP_BTN_OK) {
                    if (event.event == BSP_BTN_PRESS && speech_session &&
                        (menu || notice_list || notice_view ||
                         navigation.screen == PP_SCREEN_VOLUME ||
                         navigation.screen == PP_SCREEN_NETWORK)) {
                        stop_play();
                        button_state.wake_guard = true;
                    }
                    if (button_state.wake_guard) {
                        if (event.event == BSP_BTN_RELEASE)
                            button_state.wake_guard = false;
                    } else if (dangerous_confirm) {
                        if (event.event == BSP_BTN_RELEASE) {
                            dangerous_confirm = false;
                            if (confirm_send) {
                                if (dangerous_action == PP_SELECT_REPROVISION) {
                                    stop_play();
                                    pp_net_provision();
                                } else if (pp_net_stop() && pp_settings_reset() == ESP_OK)
                                    esp_restart();
                                else
                                    set_status("未能恢复出厂，请重启后重试");
                            } else
                                set_status("已取消");
                        }
                    } else if (input_state.phase == PP_INPUT_CONFIRM) {
                        if (event.event == BSP_BTN_RELEASE) {
                            if (review_reading) {
                                review_reading = review_pending = false;
                                review_choice = 0;
                            } else if (review_choice == 2) {
                                review_reading = true;
                                body[0] = 0;
                                view_page = view_next = 0;
                                request_review_page(0);
                            } else if (review_choice == 1 && pp_input_confirm(&input_state)) {
                                send_id("input.confirm", request_id);
                                set_status("正在提交");
                            } else {
                                abort_record();
                                body[0] = 0;
                                view = PP_UI_IDLE;
                                set_status("已丢弃，未执行");
                            }
                        }
                    } else if (notice_list && !menu) {
                        if (event.event == BSP_BTN_RELEASE) {
                            if (notice_read.pending) {
                                set_status("正在读取任务");
                            } else if (notice_index == (unsigned)notice_count && notice_older) {
                                cJSON *j = command("notifications.list");
                                cJSON_AddItemToObject(j, "before", cJSON_Parse(notice_cursor));
                                pp_net_send(j);
                                notice_index = 0;
                            } else if (notice_index < (unsigned)notice_count) {
                                next_offset = 0;
                                request_notice_page(0);
                            } else
                                pp_navigation_back(&navigation);
                        }
                    } else if (notice_view) {
                        if (event.event == BSP_BTN_RELEASE) {
                            notice_read.pending = false;
                            pp_navigation_back(&navigation);
                            set_status("选择任务查看");
                        }
                    } else if (navigation.screen == PP_SCREEN_VOLUME ||
                               navigation.screen == PP_SCREEN_NETWORK) {
                        if (event.event == BSP_BTN_RELEASE)
                            pp_navigation_back(&navigation);
                        if (event.event == BSP_BTN_RELEASE)
                            set_status("请选择");
                    } else if (menu) {
                        if (event.event == BSP_BTN_RELEASE)
                            choose();
                    } else
                        actions(event.event == BSP_BTN_PRESS
                                    ? pp_press(&button_state, now_ms(), speech_session)
                                    : pp_release(&button_state, now_ms()));
                } else if (event.key == BSP_BTN_UP && event.event == BSP_BTN_PRESS) {
                    pp_navigation_up_press(&navigation, now_ms());
                } else if (event.event == BSP_BTN_RELEASE) {
                    if (event.key == BSP_BTN_UP && !pp_navigation_up_release(&navigation))
                        continue;
                    if (!settings.paired || pp_provision_active())
                        continue;
                    if (input_state.phase == PP_INPUT_CONFIRM) {
                        if (!review_reading) {
                            review_choice = (review_choice + (event.key == BSP_BTN_UP ? 2 : 1)) % 3;
                        } else if (!review_pending) {
                            if (event.key == BSP_BTN_UP && view_page)
                                view_page = pp_ui_previous(body, view_page);
                            else if (event.key == BSP_BTN_DOWN && view_next)
                                view_page = view_next;
                            else if (event.key == BSP_BTN_UP && review_offset)
                                request_review_page(review_offset < 80 ? 0 : review_offset - 80);
                            else if (event.key == BSP_BTN_DOWN && review_next)
                                request_review_page(review_next);
                        }
                    } else if (dangerous_confirm) {
                        confirm_send = !confirm_send;
                    } else if (navigation.screen == PP_SCREEN_VOLUME ||
                               (speech_session && !menu && !notice_list)) {
                        adjust_volume(event.key == BSP_BTN_UP ? 5 : -5);
                        /* Audio worker owns codec I2C; only change its target. */
                        activity = now_ms();
                    } else if (input_state.phase != PP_INPUT_IDLE) {
                        set_status("先结束或确认当前输入");
                    } else if (notice_list && !menu) {
                        unsigned count = notice_choices();
                        /* Changing selection cancels only the local read wait.
                         * A late page must not open a different selected task. */
                        notice_read.pending = false;
                        notice_index =
                            (notice_index + (event.key == BSP_BTN_UP ? count - 1 : 1)) % count;
                        set_status("选择任务查看");
                    } else if (menu)
                        pp_navigation_move(&navigation, event.key == BSP_BTN_UP ? -1 : 1);
                    else if (navigation.screen == PP_SCREEN_HOME) {
                        if (question_text[0]) {
                            pp_navigation_open(&navigation, PP_SCREEN_QUESTION);
                            view = PP_UI_REPLY;
                            view_page = view_next = 0;
                            set_status("等待回答");
                            continue;
                        }
                        pp_navigation_open(&navigation, PP_SCREEN_HISTORY);
                        new_message = false;
                        if (!pp_net_connected()) {
                            body[0] = 0;
                            view = PP_UI_IDLE;
                            if (settings.offline_cache && pp_history_cache_recent())
                                set_status("正在读取最近缓存记录");
                            else
                                set_status(settings.offline_cache ? "本地读取忙，请稍后重试"
                                                                  : "离线缓存已关闭");
                        } else if (latest_message[0])
                            request_page(latest_message, 0, event.key == BSP_BTN_UP);
                        else {
                            message_id[0] = 0;
                            history_step(-1);
                        }
                    } else if (navigation.screen == PP_SCREEN_QUESTION) {
                        if (event.key == BSP_BTN_UP && view_page)
                            view_page = pp_ui_previous(question_text, view_page);
                        else if (event.key == BSP_BTN_DOWN && view_next)
                            view_page = view_next;
                    } else if (message_id[0] || notice_view) {
                        if (view == PP_UI_IMAGE) {
                            if (pp_picture_loading()) {
                                close_image();
                                pp_net_send(command("image.close"));
                            }
                            if (event.key == BSP_BTN_UP) {
                                if (image_index)
                                    open_image(image_index - 1);
                                else {
                                    close_image();
                                    view = PP_UI_REPLY;
                                    if (!body[0])
                                        history_step(-1);
                                }
                            } else if (image_index + 1 < image_count)
                                open_image(image_index + 1);
                            else {
                                close_image();
                                history_step(1);
                            }
                            continue;
                        }
                        if (event.key == BSP_BTN_UP && view_page) {
                            view_page = pp_ui_previous(body, view_page);
                            continue;
                        }
                        if (event.key == BSP_BTN_DOWN && view_next) {
                            view_page = view_next;
                            continue;
                        }
                        if (!notice_view && event.key == BSP_BTN_DOWN && !page_next &&
                            settings.show_images && image_count) {
                            pp_navigation_open(&navigation, PP_SCREEN_HISTORY);
                            open_image(0);
                            continue;
                        }
                        if (!notice_view && !page_pending &&
                            ((event.key == BSP_BTN_UP && !page_offset) ||
                             (event.key == BSP_BTN_DOWN && !page_next))) {
                            history_step(event.key == BSP_BTN_UP ? -1 : 1);
                            continue;
                        }
                        unsigned offset = event.key == BSP_BTN_UP
                                              ? (page_offset < 80 ? 0 : page_offset - 80)
                                              : page_next;
                        if (event.key != BSP_BTN_UP && !offset)
                            continue;
                        if (notice_view)
                            request_notice_page(offset);
                        else {
                            request_page(message_id, offset, false);
                        }
                    }
                }
            } else if (event.kind == 2) {
                incoming(event.text);
                free(event.text);
            } else {
                if (!event.event) {
                    if (input_state.phase == PP_INPUT_CONFIRM ||
                        input_state.phase == PP_INPUT_SUBMITTING ||
                        input_state.phase == PP_INPUT_UNKNOWN) {
                        capture = false;
                        capture_generation++;
                        pp_input_abort(&input_state);
                        input_state.phase = PP_INPUT_UNKNOWN;
                        button_state = (pp_button_t){.wake_guard = button_state.wake_guard ||
                                                                   button_state.down};
                        xQueueReset(recorded);
                        review_reading = review_pending = false;
                        view = PP_UI_INFO;
                    } else
                        abort_record();
                    stop_play();
                    waiting = false;
                    page_pending = false;
                    notice_read.pending = false;
                    index_direction = 0;
                    close_image();
                    if (settings.offline_cache) {
                        if (browsing) {
                            body[0] = 0;
                            view = PP_UI_IDLE;
                        }
                        /* Flash cache owns retention; the displayed body is an independent copy. */
                        pp_history_cache_recent();
                    }
                    if (!settings.offline_cache) {
                        /* Flash cache owns retention; the displayed body is an independent copy. */
                        if (view == PP_UI_REPLY)
                            body[0] = 0;
                    }
                }
                set_status(event.text ? event.text : "网络错误");
                free(event.text);
            }
        }
        if (pp_navigation_up_tick(&navigation, now_ms()))
            home(NULL);
        pp_disk_allow_writes(input_state.phase == PP_INPUT_IDLE && !waiting && !playing &&
                             !speech_session);
        pp_history_cache_poll(input_state.phase == PP_INPUT_IDLE && !waiting && !playing &&
                              !speech_session && !browsing && !notice_view && !notice_list &&
                              !pp_picture_active());
        if (!menu && !notice_list && !notice_view && navigation.screen != PP_SCREEN_VOLUME &&
            navigation.screen != PP_SCREEN_NETWORK && !dangerous_confirm &&
            input_state.phase != PP_INPUT_CONFIRM)
            actions(pp_tick(&button_state, now_ms()));
        if (pp_input_limit(&input_state, now_ms(), 60000))
            input_limit_reached(false);
        if (failed) {
            failed = false;
            abort_record();
            stop_play();
            set_status("缓冲或音频错误，未重试");
        }
        if (pp_picture_poll(now_ms()) && view == PP_UI_IMAGE)
            view = PP_UI_REPLY;
        if (input_state.phase == PP_INPUT_RECORDING || input_state.phase == PP_INPUT_DRAINING) {
            recording_t audio;
            unsigned n = 0;
            while (n++ < 4 && xQueueReceive(recorded, &audio, 0)) {
                if (audio.generation != capture_generation)
                    continue;
                if (!pp_input_audio(&input_state, audio.size)) {
                    abort_record();
                    set_status("音频帧超出限制，本次未发送");
                    break;
                }
                if (!pp_net_pcm(sequence++, audio.pcm, audio.size)) {
                    abort_record();
                    break;
                }
                if (input_state.audio_bytes == input_state.max_audio_bytes) {
                    input_limit_reached(true);
                    break;
                }
            }
            if (input_state.phase == PP_INPUT_DRAINING && !audio_busy &&
                uxQueueMessagesWaiting(recorded) == 0) {
                cJSON *j = command("input.finish");
                cJSON_AddStringToObject(j, "request_id", request_id);
                cJSON_AddNumberToObject(j, "frames", sequence);
                cJSON_AddBoolToObject(j, "confirmation_required", input_state.limited);
                pp_input_drained(&input_state);
                if (!pp_net_send(j))
                    set_status("提交待核对");
                else
                    waiting = true;
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
            if ((!speech_session && navigation.screen != PP_SCREEN_VOLUME) ||
                now_ms() - volume_feedback_at >= 1400)
                volume_feedback_at = 0;
            if (now_ms() - last_telemetry > 10000 || !last_telemetry) {
                last_telemetry = now_ms();
                battery = bsp_battery_soc();
                char ssid[33];
                if (!pp_net_wifi_details(ssid, sizeof(ssid), &wifi_rssi))
                    wifi_rssi = -127;
            }
            const char *labels[10];
            unsigned count = pp_navigation_count(&navigation);
            char keep_label[48], task_heading[96], title[64];
            snprintf(keep_label, sizeof(keep_label), "保持在线 · %s",
                     settings.keep_online ? "开" : "关");
            for (unsigned i = 0; i < count; i++)
                labels[i] = navigation.screen == PP_SCREEN_SETTINGS && i == 0
                                ? keep_label
                                : pp_navigation_label(&navigation, i);
            bool listening = capture && pp_input_listening(&input_state);
            bool volume_screen = navigation.screen == PP_SCREEN_VOLUME;
            pp_ui_frame_t frame = {
                .view = dangerous_confirm     ? PP_UI_INFO
                        : menu || notice_list ? PP_UI_MENU
                        : navigation.screen == PP_SCREEN_HOME && settings.paired &&
                                !pp_provision_active()
                            ? PP_UI_IDLE
                            : view,
                .state = new_message && (browsing || navigation.screen == PP_SCREEN_HOME)
                             ? "有新回复 · 上下查看"
                             : status,
                .body = navigation.screen == PP_SCREEN_QUESTION ? question_text : body,
                .origin = navigation.screen == PP_SCREEN_QUESTION ? "等待回答"
                          : dangerous_confirm                     ? status
                          : menu || notice_list || notice_view || volume_screen ||
                                  navigation.screen == PP_SCREEN_NETWORK
                              ? pp_navigation_title(&navigation)
                          : view == PP_UI_INFO ? status
                                               : message_origin,
                .choices = labels,
                .choice_count = count,
                .selection = navigation.selection,
                .task_context = notice_list || notice_view,
                .queue = settings.queue,
                .online = pp_net_connected(),
                .volume = atomic_load(&target_volume),
                .volume_feedback = volume_screen || volume_feedback_at != 0,
                .brightness = settings.brightness,
                .text_size = settings.text_size,
                .battery = battery,
                .wifi = wifi_rssi,
                .now = now_ms(),
                .page = view_page,
                .follow = message_streaming && !view_page,
                .level = listening ? atomic_load(&input_level) : 0,
                .elapsed = listening ? now_ms() - input_state.started : 0,
                .hint = !settings.paired || pp_provision_active() ? "按屏幕完成配置"
                        : dangerous_confirm ? (confirm_send ? "确认 · OK执行" : "取消 · OK返回")
                        : input_state.phase == PP_INPUT_CONFIRM
                            ? (review_reading ? "上下查看 · OK返回" : "上下选择 · OK确认")
                        : notice_view                             ? "OK 返回列表 · 长↑主页"
                        : menu || notice_list                     ? "OK 确认 · 长↑主页"
                        : volume_screen                           ? "上下音量 · OK返回"
                        : navigation.screen == PP_SCREEN_NETWORK  ? "OK 返回 · 长↑主页"
                        : listening                               ? "正在听 · 松开结束"
                        : speech_session                          ? "上下音量 · OK停播"
                        : navigation.screen == PP_SCREEN_HOME     ? "按住说 · OK菜单"
                        : navigation.screen == PP_SCREEN_QUESTION ? "按住回答 · 长↑主页"
                        : browsing                                ? "OK 返回 · 长↑主页"
                                                                  : "上下翻页 · 长↑主页"};
            if (input_state.phase == PP_INPUT_CONFIRM) {
                frame.view = review_reading ? PP_UI_REPLY : PP_UI_MENU;
                frame.origin = review_reading ? "你说的 · 待发送" : "输入尚未发送";
                frame.choices = review_choices;
                frame.choice_count = 3;
                frame.selection = review_choice;
                frame.follow = false;
                frame.elapsed = 0;
            } else if (notice_list) {
                count = 0;
                for (unsigned i = 0; i < (unsigned)notice_count; i++)
                    labels[count++] = notices[i];
                if (notice_older)
                    labels[count++] = "更早的任务消息";
                labels[count++] = "返回菜单";
                frame.choice_count = count;
                frame.selection = notice_index < count ? notice_index : count - 1;
                frame.follow = false;
            } else if (notice_view) {
                pp_text(title, sizeof(title), active_notice_title);
                snprintf(task_heading, sizeof(task_heading), "任务报告 · %s", title);
                frame.origin = task_heading;
            }
            view_next = pp_ui_render(&frame);
        }
        if (pp_notice_expire(&notice_read, now_ms())) {
            set_status("任务读取超时，OK重试或返回");
            ESP_LOGW("pa_terminal", "notice_read timed out");
        }
        apply_configuration();
        if (configured && settings.paired && settings.wifi_count && !pp_provision_active() &&
            !settings.keep_online && now_ms() - activity > settings.sleep_seconds * 1000)
            sleep_now(false);
        if (preference_changed && now_ms() - preference_changed >= 500) {
            preference_changed = 0;
            if (pp_preference("volume", atomic_load(&target_volume)) != ESP_OK)
                set_status("音量保存失败");
        }
        if (now_ms() - last_memory > 60000) {
            last_memory = now_ms();
            ESP_LOGI("pa_terminal", "heap=%u largest=%u low=%u", (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
                     (unsigned)esp_get_minimum_free_heap_size());
        }
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}

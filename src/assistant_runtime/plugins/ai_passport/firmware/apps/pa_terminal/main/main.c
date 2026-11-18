#include "terminal_audio.h"
#include "terminal_events.h"
#include "pp_board_power.h"
#include "bsp_battery.h"
#include "bsp_button.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "network.h"
#include "provision.h"
#include "esp_system.h"
#include <sys/time.h>
#include "nvs_flash.h"
#include "settings.h"
#include "diagnostics.h"
#include "navigation.h"
#include "notification_model.h"
#include "terminal_model.h"
#include "text_layout.h"
#include "timeline.h"
#include "history_cache.h"
#include "pictures.h"
#include "ui.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static pp_terminal_settings_t settings;
static pp_button_t button_state;
static pp_input_t input_state;
static pp_navigation_t navigation;
#define menu pp_navigation_menu(&navigation)
#define browsing (navigation.screen == PP_SCREEN_HISTORY)
#define notice_list (navigation.screen == PP_SCREEN_TASKS)
#define notice_view (navigation.screen == PP_SCREEN_TASK)
static bool playing, play_end;
static unsigned play_generation;
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
static uint32_t review_started;
static uint32_t preference_changed, volume_feedback_at;
static bool waiting, configured, sleep_failed, maintenance_confirmed, speech_session,
    audio_available, dangerous_confirm;
static pp_selection_t dangerous_action;
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
static struct {
    char id[37];
    uint32_t started;
    bool correlated, pending, older, viewing_older;
} notice_directory;
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
/* The home card describes connectivity. Menus keep their own route and never
 * borrow the chat body for credentials, network diagnostics or pairing codes. */
static char network_details[640];
static char connection_status[160] = "连接中", hotspot_info[192], pairing_info[128];
static bool setup_home(void) {
    return !configured || !settings.paired || pp_provision_active() || pp_net_rejected();
}
static const char *connection_info(void) {
    if (!configured)
        return "本地设置读取失败，请通过 USB 诊断检查设备。";
    pp_hotspot_t hotspot;
    if (pp_provision_credentials(&hotspot)) {
        snprintf(hotspot_info, sizeof(hotspot_info), "%s\n密码 %s\n\n手机打开\nhttp://192.168.4.1",
                 hotspot.ssid, hotspot.password);
        memset(&hotspot, 0, sizeof(hotspot));
        return hotspot_info;
    }
    memset(hotspot_info, 0, sizeof(hotspot_info));
    if (pp_net_rejected())
        return "请检查 PA 接入设置。配对已被撤销时，可在设备维护中重新配对，保留 Wi-Fi。";
    return pairing_info;
}
static void confirm_maintenance(pp_selection_t action) {
    dangerous_action = action;
    dangerous_confirm = true;
    maintenance_confirmed = false;
}
static void request_review_page(unsigned offset) {
    if (review_pending)
        return;
    cJSON *j = command("input.transcript");
    cJSON_AddStringToObject(j, "request_id", request_id);
    cJSON_AddNumberToObject(j, "offset", offset);
    if (pp_net_send(j)) {
        review_pending = true;
        review_started = now_ms();
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
static void reset_play(void) {
    playing = false;
    speech_session = false;
    play_end = false;
    play_generation++;
    pp_audio_play_stop();
}
static void stop_play(void) {
    reset_play();
    pp_net_send(command("playback.stop"));
}
static void reset_record_controls(void) {
    pp_audio_capture_discard();
    review_reading = review_pending = false;
    button_state = (pp_button_t){.wake_guard = button_state.wake_guard || button_state.down};
}
static void abort_record(void) {
    bool pending_input = input_state.phase != PP_INPUT_IDLE;
    reset_record_controls();
    pp_input_abort(&input_state);
    if (pending_input && request_id[0])
        send_id("input.cancel", request_id);
    set_status("收音中断，请重新说");
}
static void input_limit_reached(bool discard_excess) {
    pp_audio_capture_release();
    button_state = (pp_button_t){.wake_guard = button_state.down || button_state.wake_guard};
    if (discard_excess) {
        pp_audio_capture_discard();
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
    dangerous_confirm = maintenance_confirmed = notice_read.pending = page_pending = false;
    notice_directory.pending = notice_directory.viewing_older = false;
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
static void make_id(char *out);
static void request_notice_list(bool older) {
    cJSON *j = command("notifications.list");
    if (older) {
        cJSON *cursor = cJSON_Parse(notice_cursor);
        if (!cJSON_IsArray(cursor)) {
            cJSON_Delete(cursor);
            cJSON_Delete(j);
            set_status("任务分页不可用，请重新打开");
            return;
        }
        cJSON_AddItemToObject(j, "before", cursor);
    }
    if (notice_directory.correlated) {
        make_id(notice_directory.id);
        cJSON_AddStringToObject(j, "request_id", notice_directory.id);
    }
    notice_directory.started = now_ms();
    notice_directory.older = older;
    notice_directory.pending = pp_net_send(j);
    set_status(notice_directory.pending ? "正在读取任务列表"
                                        : "任务列表未发送，请重新打开");
}
static void show_notices(void) {
    stop_play();
    close_image();
    pp_navigation_open(&navigation, PP_SCREEN_TASKS);
    notice_index = notice_count = 0;
    notice_older = notice_directory.viewing_older = false;
    notice_read.pending = false;
    request_notice_list(false);
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
        pp_audio_capture_begin();
        request_id[0] = 0;
        /* Prebuffer immediately, but do not expose a recording state until the
         * hold is established. A double click must leave the page unchanged. */
    }
    if (mask & PP_DISCARD) {
        pp_audio_capture_discard();

        if (input_state.phase == PP_INPUT_PREBUFFER) {
            pp_input_abort(&input_state);
            set_status("就绪");
        }
    }
    if (mask & PP_BEGIN) {
        if (!pp_audio_capturing() || !pp_input_begin(&input_state))
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
        pp_audio_capture_release();
        if (input_state.phase == PP_INPUT_OPENING || input_state.phase == PP_INPUT_RECORDING) {
            pp_input_release(&input_state);
            set_status("识别中");
        }
    }
    if (mask & PP_TOGGLE) {
        if (pp_preference(PP_PREF_QUEUE, !settings.queue, &settings) != ESP_OK)
            set_status("模式保存失败");
        else
            set_status(settings.queue ? "已切换排队" : "已切换引导");
    }
    if (mask & PP_MENU) {
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
static void receive_hello(cJSON *j, uint32_t generation) {
    if (number(j, "protocol") != 2 || number(j, "input_rate") != PP_CAPTURE_RATE || number(j, "output_rate") != PP_PLAYBACK_RATE ||
        pp_settings_binding(string(j, "binding"), &settings) != ESP_OK ||
        !pp_net_accept_protocol(number(j, "protocol"), generation)) {
        strcpy(connection_status, "协议或设备绑定不匹配，请检查服务版本");
        set_status(connection_status);
        return;
    }
    /* A full event queue could have dropped the preceding link-down notice. */
    reset_record_controls();
    pp_input_disconnect(&input_state);
    pp_history_cache_bind(settings.url, settings.device, settings.binding,
                          settings.offline_cache, settings.show_images);
    strcpy(connection_status, "PA 已连接");
    pairing_info[0] = hotspot_info[0] = 0;
    notice_directory.correlated =
        cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "notification_list_ids"));
    if (notice_list && !notice_directory.pending && !notice_directory.viewing_older)
        request_notice_list(false);
    playing = false;
    speech_session = false;
    play_generation = 0;
    page_pending = false;
    index_direction = 0;
    pp_audio_play_stop();
    if (!notice_list)
        set_status(audio_available ? "就绪" : "音频不可用");
    if ((navigation.screen == PP_SCREEN_HOME || navigation.screen == PP_SCREEN_CHAT) &&
        !dangerous_confirm && input_state.phase == PP_INPUT_IDLE) {
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
}

static void receive_input_receipt(cJSON *j) {
    const char *ident = string(j, "request_id");
    if (strcmp(ident, request_id) && strcmp(ident, pending)) {
        return;
    }
    const char *state = string(j, "state");
    if (!strcmp(state, "review")) {
        pp_audio_capture_discard();

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
        pp_audio_capture_release();
        input_state.phase = PP_INPUT_UNKNOWN;
        set_status("提交结果待确认");
        waiting = false;
    } else {
        pp_input_abort(&input_state);
        set_status("输入目标已变化，请重新说");
        waiting = false;
    }
}

static void receive_message(cJSON *j) {
    pp_history_page_t decoded;
    pp_text(latest_message, sizeof(latest_message), string(j, "message_id"));
    pp_history_page_t *page = pp_timeline_decode(&decoded, j);
    if (navigation.screen != PP_SCREEN_CHAT ||
        (view == PP_UI_INPUT && input_state.phase != PP_INPUT_IDLE)) {
        if (!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "restored")))
            new_message = true;
        return;
    }
    if (!close_image()) {
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
}

static void receive_notifications(cJSON *j) {
    const char *request = string(j, "request_id");
    bool response = notice_directory.pending &&
                    (notice_directory.correlated ? !strcmp(request, notice_directory.id) : !*request);
    const bool past_page = notice_directory.viewing_older ||
                           (notice_directory.pending && notice_directory.older);
    if (!response && (*request || past_page))
        return;
    cJSON *items = cJSON_GetObjectItemCaseSensitive(j, "items"), *i;
    if (!cJSON_IsArray(items)) {
        if (response) {
            notice_directory.pending = false;
            if (notice_list)
                set_status("任务列表格式有误，请重新打开");
        }
        return;
    }
    char selected[sizeof(notice_ids[0])] = "";
    if (!response && notice_index < (unsigned)notice_count)
        pp_text(selected, sizeof(selected), notice_ids[notice_index]);
    if (response) {
        notice_directory.pending = false;
        notice_directory.viewing_older = notice_directory.older;
        notice_index = 0;
    }
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
        if (selected[0] && !strcmp(selected, notice_ids[notice_count]))
            notice_index = notice_count;
        notice_count++;
    }
    if (notice_index >= (unsigned)notice_count)
        notice_index = 0;
    if (notice_list && !notice_directory.pending && !notice_read.pending)
        set_status(notice_count ? "选择任务查看" : "暂无任务消息");
    for (unsigned k = 0; k < (unsigned)notice_count; k++) {
        cJSON *r = command("notification.receipt");
        cJSON_AddStringToObject(r, "id", notice_ids[k]);
        cJSON_AddNumberToObject(r, "revision", notice_revisions[k]);
        cJSON_AddStringToObject(r, "action", "received");
        pp_net_send(r);
    }
}

static void receive_notification_page(cJSON *j) {
    int result = (notice_list || notice_view) ? pp_notice_page(&notice_read, j) : 0;
    if (result <= 0) {
        if (result < 0)
            set_status("任务内容格式不匹配，请重试");
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
}

static void receive_error(cJSON *j, const char *type) {
    const char *input_hint = input_failure_hint(string(j, "code"));
    if (input_hint && !strcmp(string(j, "request_id"), request_id) &&
        strcmp(request_id, dismissed_input)) {
        pp_text(dismissed_input, sizeof(dismissed_input), request_id);
        waiting = false;
        abort_record();
        home(input_hint);
        return;
    }
    if (!strcmp(string(j, "code"), "speech_no_transcript") &&
        !strcmp(string(j, "request_id"), request_id)) {
        pp_text(dismissed_input, sizeof(dismissed_input), request_id);
        waiting = false;
        home("未检测到内容，请重新说。");
        return;
    }
    if (string(j, "request_id")[0] && !strcmp(string(j, "request_id"), dismissed_input)) {
        return;
    }
    if (!strcmp(string(j, "operation"), "input.transcript")) {
        if (!review_pending) {
            return;
        }
        review_pending = false;
        set_status("转写页读取失败，返回后可重试");
        return;
    }
    const char *operation = string(j, "operation");
    if (!strcmp(operation, "notifications.list")) {
        if (notice_directory.pending &&
            (!notice_directory.correlated || !strcmp(string(j, "request_id"), notice_directory.id))) {
            notice_directory.pending = false;
            if (notice_list)
                set_status("任务列表读取失败，请重新打开");
        }
        return;
    }
    bool image_read = !strcmp(operation, "image.open") || !strcmp(operation, "image.chunk");
    if ((!strcmp(operation, "message.page") && !page_pending) ||
        (!strcmp(operation, "history.index") && !index_direction) ||
        (!strcmp(operation, "notification.page") && !notice_read.pending) ||
        (image_read && view != PP_UI_IMAGE)) {
        return;
    }
    if (!strcmp(operation, "message.page"))
        page_pending = false;
    if (!strcmp(string(j, "operation"), "history.index"))
        index_direction = 0;
    if (!strcmp(operation, "notification.page")) {
        notice_read.pending = false;
        set_status("任务读取失败，请重试");
        return;
    }
    if (!strcmp(string(j, "operation"), "image.open") ||
        !strcmp(string(j, "operation"), "image.chunk"))
        close_image();
    if (!strcmp(type, "error") && !pp_navigation_local(&navigation) && !dangerous_confirm) {
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

static void dispatch_message(cJSON *j, uint32_t generation) {
    const char *type = string(j, "type");
    if (!strcmp(type, "hello")) {
        if (generation && !pp_net_connected() && pp_net_current_frame(generation))
            receive_hello(j, generation);
        return;
    }
    if (generation && (!pp_net_current_frame(generation) || !pp_net_connected()))
        return;
    if (pp_history_cache_receive(j)) {
        return;
    }
    if ((!strcmp(type, "playback.pending") || !strcmp(type, "playback.start")) &&
        (!audio_available || !pp_navigation_audio(&navigation))) {
        unsigned incoming_generation = number(j, "generation");
        if (incoming_generation >= play_generation) {
            play_generation = incoming_generation;
            stop_play();
        }
        return;
    }
    if (!strcmp(type, "pairing")) {
        strcpy(connection_status, "等待配对");
        snprintf(pairing_info, sizeof(pairing_info),
                 "%s\n\n在 PA 后台输入这八位数字\n配对码十分钟有效", string(j, "code"));
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
            if (play_generation != number(j, "generation")) {
                pp_audio_play_stop();
                play_end = playing = false;
            }
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
            set_status("");
        }
    } else if (!strcmp(type, "input.receipt")) {
        receive_input_receipt(j);
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
            pp_history_page_t decoded;
            pp_history_page_t *page = pp_timeline_decode(&decoded, j);
            if (page)
                show_history_page(page);
            else
                set_status("正文格式不匹配，未替换现有页面");
        }
    } else if (!strcmp(type, "message")) {
        receive_message(j);
    } else if (!strcmp(type, "playback.stop")) {
        if (number(j, "generation") < play_generation) {
            return;
        }
        playing = false;
        speech_session = false;
        play_end = false;
        play_generation = number(j, "generation");
        pp_audio_play_stop();
    } else if (!strcmp(type, "playback.start")) {
        if (number(j, "generation") < play_generation) {
            return;
        }
        if (pp_audio_capturing()) {
            play_generation = number(j, "generation");
            stop_play();
        } else {
            play_generation = number(j, "generation");
            pp_audio_play_begin(play_generation);
            playing = true;
            speech_session = true;
            play_end = false;
            next_offset = number(j, "offset");
            segment_end = number(j, "next_offset");
            credit(PP_PLAYBACK_WINDOW);
            set_status("播放中");
        }
    } else if (!strcmp(type, "playback.end") && number(j, "generation") == play_generation)
        play_end = true;
    else if (!strcmp(type, "notifications")) {
        receive_notifications(j);
    } else if (!strcmp(type, "notification.page")) {
        receive_notification_page(j);
    } else if (!strcmp(type, "execution.cancelled")) {
        set_status("已收到取消结果");
        waiting = false;
    } else if (!strcmp(type, "error") || !strcmp(type, "playback.unavailable")) {
        receive_error(j, type);
    }
}

static void incoming(const char *raw, uint32_t generation) {
    cJSON *j = cJSON_Parse(raw);
    if (!j) {
        set_status("消息格式错误");
        return;
    }
    dispatch_message(j, generation);
    cJSON_Delete(j);
}

static void sleep_now(bool explicit) {
    if (sleep_failed || pp_audio_capturing() || input_state.phase != PP_INPUT_IDLE || waiting ||
        speech_session || !pp_board_keys_released()) {
        if (explicit)
            set_status("当前交互未结束，暂不休眠");
        return;
    }
    if (!pp_audio_quiesce()) {
        set_status("音频尚未停止");
        return;
    }
    sleep_failed = true;
    if (!pp_net_stop()) {
        set_status("网络未正常停止，请重启");
        return;
    }
    pp_board_enter_deep_sleep();
}
static void adjust_volume(int delta) {
    int value = (int)pp_audio_volume() + delta;
    pp_audio_set_volume(value < 0 ? 0 : value > 100 ? 100 : (unsigned)value);
    preference_changed = volume_feedback_at = now_ms();
}
static void choose(void) {
    pp_selection_t action = pp_navigation_selected(&navigation);
    if (!configured && action != PP_SELECT_SETTINGS && action != PP_SELECT_MAINTENANCE &&
        action != PP_SELECT_NETWORK && action != PP_SELECT_HOME && action != PP_SELECT_BACK &&
        action != PP_SELECT_FACTORY) {
        set_status("本地设置读取失败");
        return;
    }
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
            set_status(pp_net_connected() && send_id("execution.cancel", active_id)
                           ? "已请求取消"
                           : "取消未发送，请检查连接");
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
    case PP_SELECT_NETWORK:
        pp_navigation_open(&navigation, PP_SCREEN_NETWORK);
        view = PP_UI_INFO;
        view_page = view_next = 0;
        set_status("网络状态");
        break;
    case PP_SELECT_KEEP_ONLINE:
        if (pp_preference(PP_PREF_KEEP_ONLINE, !settings.keep_online, &settings) != ESP_OK)
            set_status("设置保存失败");
        else
            set_status(settings.keep_online ? "保持在线已开启" : "空闲后自动休眠");
        break;
    case PP_SELECT_RELOAD:
        set_status(pp_net_connected() && pp_net_send(command("configuration.reload"))
                       ? "正在获取配置"
                       : "离线，联网后才能重载");
        break;
    case PP_SELECT_REPAIR:
        if (pp_net_rejected())
            confirm_maintenance(action);
        else
            set_status("仅在 PA 拒绝身份后重新配对");
        break;
    case PP_SELECT_RETRY:
        if (pp_net_connected()) {
            set_status("PA 已连接");
            break;
        }
        if (pp_provision_active() && !settings.wifi_count) {
            set_status("请先通过设备热点填写 Wi-Fi");
            break;
        }
        home(NULL);
        pp_net_retry();
        set_status("正在重试连接");
        break;
    case PP_SELECT_REPROVISION:
        if (pp_provision_active()) {
            home(NULL);
            break;
        }
        confirm_maintenance(action);
        break;
    case PP_SELECT_FACTORY:
        confirm_maintenance(action);
        break;
    case PP_SELECT_SLEEP:
        sleep_now(true);
        break;
    default:
        break;
    }
}
static pp_route_t current_route(void) {
    return pp_navigation_route(&navigation, dangerous_confirm, setup_home(),
                               input_state.phase == PP_INPUT_CONFIRM);
}
static void local_ok(pp_route_t route) {
    if (route == PP_ROUTE_CONFIRM) {
        dangerous_confirm = false;
        if (!maintenance_confirmed) {
            set_status("已取消");
            return;
        }
        switch (dangerous_action) {
        case PP_SELECT_REPROVISION:
            home(NULL);
            pp_net_provision();
            break;
        case PP_SELECT_REPAIR:
            home(NULL);
            pp_net_repair();
            break;
        case PP_SELECT_FACTORY:
            if (pp_net_stop() && pp_settings_reset() == ESP_OK)
                esp_restart();
            set_status("未能恢复出厂，请重启后重试");
            break;
        default:
            break;
        }
    } else if (route == PP_ROUTE_SETUP) {
        pp_navigation_open(&navigation, PP_SCREEN_SETTINGS);
        set_status("选择设置");
    } else if (menu) {
        choose();
    } else {
        pp_navigation_back(&navigation);
        set_status("请选择");
    }
}
static void local_move(pp_route_t route, int direction) {
    if (route == PP_ROUTE_CONFIRM)
        maintenance_confirmed = !maintenance_confirmed;
    else if (route == PP_ROUTE_SETUP && direction > 0 && pp_net_rejected())
        confirm_maintenance(PP_SELECT_REPAIR);
    else if (menu)
        pp_navigation_move(&navigation, direction);
    else if (navigation.screen == PP_SCREEN_VOLUME)
        adjust_volume(-5 * direction);
    else if (navigation.screen == PP_SCREEN_NETWORK) {
        if (direction < 0 && view_page)
            view_page = pp_ui_previous(network_details, view_page);
        else if (direction > 0 && view_next)
            view_page = view_next;
    }
}
static void apply_configuration(void) {
    if (!pending_configuration || pp_audio_capturing() || pp_audio_busy() || speech_session || waiting ||
        input_state.phase != PP_INPUT_IDLE || dangerous_confirm || button_state.down)
        return;
    if (preference_changed) {
        if (pp_preference(PP_PREF_VOLUME, pp_audio_volume(), &settings) != ESP_OK) {
            set_status("本地设置尚未保存");
            return;
        }
        preference_changed = 0;
    }
    cJSON *receipt = NULL;
    bool reconnect = false;
    esp_err_t e = pp_settings_cloud(pending_configuration, &receipt, &reconnect, &settings);
    cJSON_Delete(pending_configuration);
    pending_configuration = NULL;
    if (e != ESP_OK) {
        set_status("云端配置未应用，请检查并重载");
        return;
    }
    pp_audio_set_volume(settings.volume);
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
static int battery = -1;

static void dispatch_key(pp_event_t event, pp_route_t route) {
    if (button_state.wake_guard) {
        if (event.button == BSP_BTN_RELEASE)
            button_state.wake_guard = false;
        return;
    }
    if (event.key == BSP_BTN_OK) {
        if (event.button == BSP_BTN_PRESS && speech_session &&
            (menu || notice_list || notice_view ||
             navigation.screen == PP_SCREEN_VOLUME ||
             navigation.screen == PP_SCREEN_NETWORK)) {
            stop_play();
            button_state.wake_guard = true;
        }
        if (button_state.wake_guard) {
            if (event.button == BSP_BTN_RELEASE)
                button_state.wake_guard = false;
        } else if (route == PP_ROUTE_CONFIRM || route == PP_ROUTE_LOCAL ||
                   route == PP_ROUTE_SETUP) {
            if (event.button == BSP_BTN_RELEASE)
                local_ok(route);
        } else if (route == PP_ROUTE_REVIEW) {
            if (event.button == BSP_BTN_RELEASE) {
                if (review_reading) {
                    review_reading = review_pending = false;
                    review_choice = 0;
                    set_status("确认后才发送");
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
            if (event.button == BSP_BTN_RELEASE) {
                if (notice_read.pending) {
                    set_status("正在读取任务");
                } else if (notice_directory.pending && notice_index < notice_choices() - 1) {
                    set_status("正在读取任务列表");
                } else if (notice_index == (unsigned)notice_count && notice_older) {
                    request_notice_list(true);
                } else if (notice_index < (unsigned)notice_count) {
                    next_offset = 0;
                    request_notice_page(0);
                } else {
                    notice_directory.pending = notice_directory.viewing_older = false;
                    pp_navigation_back(&navigation);
                    set_status("请选择");
                }
            }
        } else if (notice_view) {
            if (event.button == BSP_BTN_RELEASE) {
                notice_read.pending = false;
                pp_navigation_back(&navigation);
                set_status("选择任务查看");
            }
        } else
            actions(event.button == BSP_BTN_PRESS
                        ? pp_press(&button_state, now_ms(), speech_session)
                        : pp_release(&button_state, now_ms()));
    } else if (event.key == BSP_BTN_UP && event.button == BSP_BTN_PRESS) {
        pp_navigation_up_press(&navigation, now_ms());
    } else if (event.button == BSP_BTN_RELEASE) {
        if (event.key == BSP_BTN_UP && !pp_navigation_up_release(&navigation))
            return;
        if (route == PP_ROUTE_CONFIRM || route == PP_ROUTE_LOCAL ||
            route == PP_ROUTE_SETUP) {
            local_move(route, event.key == BSP_BTN_UP ? -1 : 1);
            return;
        }
        if (route == PP_ROUTE_REVIEW) {
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
        } else if (speech_session && !notice_list) {
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
        } else if (navigation.screen == PP_SCREEN_HOME) {
            if (question_text[0]) {
                pp_navigation_open(&navigation, PP_SCREEN_QUESTION);
                view = PP_UI_REPLY;
                view_page = view_next = 0;
                set_status("等待回答");
                return;
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
                return;
            }
            if (event.key == BSP_BTN_UP && view_page) {
                view_page = pp_ui_previous(body, view_page);
                return;
            }
            if (event.key == BSP_BTN_DOWN && view_next) {
                view_page = view_next;
                return;
            }
            if (!notice_view && event.key == BSP_BTN_DOWN && !page_next &&
                settings.show_images && image_count) {
                pp_navigation_open(&navigation, PP_SCREEN_HISTORY);
                open_image(0);
                return;
            }
            if (!notice_view && !page_pending &&
                ((event.key == BSP_BTN_UP && !page_offset) ||
                 (event.key == BSP_BTN_DOWN && !page_next))) {
                history_step(event.key == BSP_BTN_UP ? -1 : 1);
                return;
            }
            unsigned offset = event.key == BSP_BTN_UP
                                  ? (page_offset < 80 ? 0 : page_offset - 80)
                                  : page_next;
            if (event.key != BSP_BTN_UP && !offset)
                return;
            if (notice_view)
                request_notice_page(offset);
            else {
                request_page(message_id, offset, false);
            }
        }
    }
}

static void handle_key(pp_event_t event) {
    const pp_route_t route = current_route();
    const uint32_t page_epoch = navigation.page_epoch;
    const uint32_t now = now_ms();
    activity = now;
    if (event.key != BSP_BTN_OK) {
        if (event.button == BSP_BTN_PRESS)
            navigation.transition_click = false;
        dispatch_key(event, route);
        return;
    }
    if (event.button == BSP_BTN_PRESS) {
        if (!button_state.wake_guard && !pp_navigation_ok_press(&navigation, route, now))
            return;
    } else if (!pp_navigation_ok_release(&navigation, route) && !button_state.wake_guard) {
        /* Recording can move from home to chat while held. The audio gesture
         * still owns this release; a destination control cannot consume it. */
        if (button_state.down)
            actions(pp_release(&button_state, now));
        return;
    }
    dispatch_key(event, route);
    if (event.button == BSP_BTN_RELEASE &&
        (navigation.page_epoch != page_epoch || current_route() != route))
        pp_navigation_ok_transition(&navigation, now);
}

static void handle_link(pp_event_t event) {
    if (!event.online) {
        reset_record_controls();
        if (pp_input_disconnect(&input_state))
            view = PP_UI_INFO;
        reset_play();
        waiting = false;
        page_pending = false;
        notice_read.pending = false;
        notice_directory.pending = false;
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
    pp_text(connection_status, sizeof(connection_status),
            event.text.data ? event.text.data : "网络错误");
    pairing_info[0] = 0;
    if (notice_list)
        set_status(notice_count ? "连接中断，显示上次列表" : "离线，暂无法读取任务");
    else if (!pp_navigation_local(&navigation))
        set_status(connection_status);
}

static void poll_audio(void) {
    if (input_state.phase == PP_INPUT_RECORDING || input_state.phase == PP_INPUT_DRAINING) {
        pp_recording_t audio;
        unsigned n = 0;
        while (n++ < 4 && pp_audio_recorded(&audio)) {
            if (!pp_input_audio(&input_state, sizeof(audio.pcm))) {
                abort_record();
                set_status("音频帧超出限制，本次未发送");
                break;
            }
            if (!pp_net_pcm(sequence++, audio.pcm, sizeof(audio.pcm))) {
                abort_record();
                break;
            }
            if (input_state.audio_bytes == input_state.max_audio_bytes) {
                input_limit_reached(true);
                break;
            }
        }
        if (input_state.phase == PP_INPUT_DRAINING && pp_audio_capture_drained()) {
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
    unsigned consumed = pp_audio_take_credit();
    if (playing && consumed)
        credit(consumed);
    if (playing && play_end && pp_audio_play_drained()) {
        cJSON *j = command("playback.completed");
        cJSON_AddNumberToObject(j, "generation", play_generation);
        pp_net_send(j);
        next_offset = segment_end;
        playing = false;
        pp_audio_play_stop();
        activity = now_ms();
        set_status("回复");
    }
}

static void render_screen(void) {
    static uint32_t last_telemetry;
    static int wifi_rssi = -127;
    if ((!speech_session && navigation.screen != PP_SCREEN_VOLUME) ||
        now_ms() - volume_feedback_at >= 1400)
        volume_feedback_at = 0;
    static char wifi_name[33];
    if (now_ms() - last_telemetry > 10000 || !last_telemetry) {
        last_telemetry = now_ms();
        battery = bsp_battery_soc();
        if (!pp_net_wifi_details(wifi_name, sizeof(wifi_name), &wifi_rssi))
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
    bool listening = pp_audio_capturing() && pp_input_listening(&input_state);
    pp_route_t route = current_route();
    bool setup_card = route == PP_ROUTE_SETUP;
    bool network_page = navigation.screen == PP_SCREEN_NETWORK;
    const char *details = setup_card ? connection_info() : "";
    if (!setup_card)
        memset(hotspot_info, 0, sizeof(hotspot_info));
    const char *link_status = (details == hotspot_info || (network_page && pp_provision_active()))
                                  ? "连接设备热点"
                                  : connection_status;
    if (network_page) {
        char signal[24] = "未连接";
        if (wifi_rssi != -127)
            snprintf(signal, sizeof(signal), "%d dBm", wifi_rssi);
        snprintf(network_details, sizeof(network_details),
                 "Wi-Fi %s\n%s\n信号 %s\n可用内存 %u\n最大连续块 %u",
                 wifi_rssi == -127 ? "未连接" : wifi_name, link_status, signal,
                 (unsigned)esp_get_free_heap_size(),
                 (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    }
    bool volume_screen = navigation.screen == PP_SCREEN_VOLUME;
    pp_ui_frame_t frame = {
        .view = dangerous_confirm                             ? PP_UI_INFO
                : menu || notice_list                         ? PP_UI_MENU
                : setup_card || network_page || volume_screen ? PP_UI_INFO
                : navigation.screen == PP_SCREEN_HOME         ? PP_UI_IDLE
                                                              : view,
        .state =
            (setup_card || (navigation.screen == PP_SCREEN_HOME && !pp_net_connected()))
                ? link_status
            : new_message && (browsing || navigation.screen == PP_SCREEN_HOME)
                ? "有新回复 · 上下查看"
                : status,
        .body = setup_card                                ? details
                : network_page                            ? network_details
                : navigation.screen == PP_SCREEN_QUESTION ? question_text
                                                          : body,
        .origin = setup_card                                ? "设备连接"
                  : navigation.screen == PP_SCREEN_QUESTION ? "等待回答"
                  : menu || notice_list || notice_view || volume_screen ||
                          navigation.screen == PP_SCREEN_NETWORK
                      ? pp_navigation_title(&navigation)
                  : view == PP_UI_INFO ? status
                                       : message_origin,
        .choices = labels,
        .choice_count = count,
        .selection = navigation.selection,
        .task_context = notice_list || notice_view,
        .local_page_hint = network_page && !dangerous_confirm,
        .queue = settings.queue,
        .online = pp_net_connected(),
        .volume = pp_audio_volume(),
        .volume_feedback = volume_screen || volume_feedback_at != 0,
        .brightness = settings.brightness,
        .text_size = settings.text_size,
        .battery = battery,
        .wifi = wifi_rssi,
        .now = now_ms(),
        .page = setup_card ? 0 : view_page,
        .follow = route == PP_ROUTE_SESSION && message_streaming && !view_page,
        .level = listening ? pp_audio_level() : 0,
        .elapsed = listening ? now_ms() - input_state.started : 0,
        .hint = setup_card ? (pp_net_rejected() ? "下 重新配对 · OK 设置" : "OK 设置")
                : route == PP_ROUTE_REVIEW
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
    if (dangerous_confirm) {
        bool reprovision = dangerous_action == PP_SELECT_REPROVISION;
        bool repair = dangerous_action == PP_SELECT_REPAIR;
        frame.origin = frame.state = reprovision ? "重新配网？"
                                     : repair    ? "重新配对？"
                                                 : "恢复出厂？";
        frame.body =
            reprovision ? "将开启设备热点。提交前保留原配置。"
            : repair
                ? "将更换设备身份并重启。保留 Wi-Fi 和设置。请在 PA 后台用新码绑定新会话。"
                : "将移除此设备的配对、Wi-Fi 和本地设置。服务器会话和任务不会被删除。";
        frame.hint = maintenance_confirmed ? "确认 · OK执行" : "上下选择 · OK取消";
        frame.page = 0;
        frame.follow = false;
    } else if (route == PP_ROUTE_REVIEW) {
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

void app_main(void) {
    esp_err_t e = nvs_flash_init();
    if (e != ESP_OK) {
        ESP_LOGE("pa_terminal", "NVS unavailable; no automatic erase");
        return;
    }
    if (!pp_events_init() || !pp_ui_init())
        return;
    pp_picture_init(set_status);
    configured = pp_settings_load(&settings) == ESP_OK;
    pp_history_cache_init(pp_received);
    pp_history_cache_bind(settings.url, settings.device, settings.binding,
                          settings.paired && settings.offline_cache, settings.show_images);
    pp_usb_start();
    if (!configured) {
        set_status("本地设置读取失败");
        strcpy(body, "未清空存储，请通过 USB 诊断检查设备。");
    }
    audio_available = pp_audio_start(settings.volume);
    if (!audio_available)
        set_status("音频初始化失败");
    (void)bsp_battery_init();
    button_state.wake_guard = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_GPIO;
    ESP_ERROR_CHECK(bsp_button_init(pp_events_key, NULL));
    if (pp_board_keys_released())
        button_state.wake_guard = false;
    if (configured && !pp_net_start()) {
        strcpy(connection_status, "网络初始化失败");
        set_status(connection_status);
    }
    activity = now_ms();
    uint32_t last_render = 0, last_memory = 0;
    bool provision_saved = false;
    for (;;) {
        if (sleep_failed) {
            pp_ui_frame_t frame = {.view = PP_UI_INFO,
                                   .state = status,
                                   .body = body,
                                   .origin = "设备状态",
                                   .hint = "请重启设备后检查",
                                   .volume = pp_audio_volume(),
                                   .battery = battery,
                                   .wifi = -127};
            pp_ui_render(&frame);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        /* Refresh local pairing and Wi-Fi state even if the new PA is unreachable. */
        bool fresh = configured && pp_settings_refresh(&settings);
        if (!pp_provision_complete())
            provision_saved = false;
        else if (!provision_saved && fresh) {
            provision_saved = true;
            strcpy(connection_status, "正在连接 Wi-Fi");
        }
        pp_event_t event;
        if (pp_events_take_key_overflow()) {
            navigation.up_down = navigation.up_consumed = false;
            navigation.ok_held = false;
            pp_audio_capture_release();
            abort_record();
            set_status("按键事件拥塞，本次收音已取消");
        }
        if (pp_events_next(&event)) {
            switch (event.kind) {
            case PP_EVENT_KEY:
                handle_key(event);
                break;
            case PP_EVENT_MESSAGE:
                if (!event.generation || pp_net_current_frame(event.generation))
                    incoming(event.text.data, event.generation);
                break;
            case PP_EVENT_LINK:
                if (event.generation == pp_net_generation() && !pp_net_connected())
                    handle_link(event);
                break;
            }
            pp_buffer_free(&event.text);
        }
        if (pp_navigation_up_tick(&navigation, now_ms()))
            home(NULL);
        pp_disk_allow_writes(input_state.phase == PP_INPUT_IDLE && !waiting && !playing &&
                             !speech_session);
        pp_history_cache_poll(input_state.phase == PP_INPUT_IDLE && !waiting && !playing &&
                              !speech_session && !browsing && !notice_view && !notice_list &&
                              !pp_picture_active());
        if (current_route() == PP_ROUTE_SESSION && !notice_list && !notice_view)
            actions(pp_tick(&button_state, now_ms()));
        if (pp_input_limit(&input_state, now_ms(), 60000))
            input_limit_reached(false);
        pp_audio_error_t audio_error = pp_audio_take_error();
        bool event_error = pp_events_take_error();
        if (audio_error != PP_AUDIO_OK || event_error) {
            if (audio_error == PP_AUDIO_CODEC_FAILURE)
                audio_available = false;
            if (input_state.phase == PP_INPUT_PREBUFFER || input_state.phase == PP_INPUT_OPENING ||
                input_state.phase == PP_INPUT_RECORDING || input_state.phase == PP_INPUT_DRAINING)
                abort_record();
            if ((event_error || audio_error == PP_AUDIO_CODEC_FAILURE) && (playing || speech_session))
                stop_play();
            set_status(audio_error == PP_AUDIO_CODEC_FAILURE ? "音频不可用，仍可查看消息"
                       : audio_error == PP_AUDIO_CAPTURE_OVERFLOW ? "录音缓冲已满，请重新说"
                                                                  : "消息缓冲错误，未重试");
        }
        if (pp_picture_poll(now_ms()) && view == PP_UI_IMAGE)
            view = PP_UI_REPLY;
        poll_audio();
        if (now_ms() - last_render >= 100) {
            last_render = now_ms();
            render_screen();
        }
        if (pp_notice_expire(&notice_read, now_ms())) {
            set_status("任务读取超时，OK重试或返回");
            ESP_LOGW("pa_terminal", "notice_read timed out");
        }
        if (notice_directory.pending &&
            now_ms() - notice_directory.started >= PP_TERMINAL_READ_TIMEOUT_MS) {
            notice_directory.pending = false;
            if (notice_list)
                set_status("任务列表读取超时，请重新打开");
        }
        if (review_pending && now_ms() - review_started >= PP_TERMINAL_READ_TIMEOUT_MS) {
            review_pending = false;
            set_status("转写读取超时，返回后可重试");
        }
        apply_configuration();
        if (configured && !setup_home() && settings.wifi_count &&
            current_route() == PP_ROUTE_SESSION && !settings.keep_online &&
            now_ms() - activity > settings.sleep_seconds * 1000)
            sleep_now(false);
        if (preference_changed && now_ms() - preference_changed >= 500) {
            preference_changed = 0;
            if (pp_preference(PP_PREF_VOLUME, pp_audio_volume(), &settings) != ESP_OK)
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

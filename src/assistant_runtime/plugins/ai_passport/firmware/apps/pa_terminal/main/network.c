#include "terminal_limits.h"
#include "network.h"
#include "net_traffic.h"
#include "object_cache.h"
#include "image_model.h"
#include "network_model.h"
#include "esp_system.h"
#include "settings.h"
#include "terminal_audio.h"
#include "byte_buffer.h"
#include "provision.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "mbedtls/ssl.h"
#include "sdkconfig.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    pp_buffer_t buffer;
    bool binary;
    uint32_t generation;
} packet_t;
static esp_websocket_client_handle_t client;
static QueueHandle_t outgoing;
static atomic_uint pcm_in_flight;
static pp_buffer_slot_t pcm_slots[2];
static pp_buffer_pool_t pcm_packets = {.slots = pcm_slots, .count = sizeof(pcm_slots) / sizeof(*pcm_slots)};
static atomic_uint expected_image;
static EventGroupHandle_t network;
static pp_link_t ws_link;
static atomic_bool stopped, standby, reconfigure, retry, provision, rejected, repair;
typedef enum { WIFI_IDLE, WIFI_SEARCHING, WIFI_WAITING, WIFI_RETRY_PENDING } wifi_phase_t;
static _Atomic wifi_phase_t wifi_phase;
#define WIFI_READY BIT0
#define WIFI_FAILED BIT1
#define CONTROL BIT2
#define WORKER_DONE BIT3
#define STANDBY_PAUSED BIT4
#define STANDBY_FAILED BIT5
static pp_buffer_t received;
static size_t used, frame_used;
static int opcode;
static unsigned char binary_prefix[8];
static unsigned prefix_size;
static bool binary_classified, image_binary, drop_image;
static atomic_bool socket_error;
static bool wifi_uncertain; /* Owned only by the network worker. */
#define PP_NETWORK_TASK_PRIORITY 3
#define PP_HELLO_TIMEOUT_MS 30000

_Static_assert(PP_INPUT_BATCH_BYTES + PP_INPUT_TAGGED_HEADER_BYTES <= PP_WS_IO_BYTES,
               "One microphone packet must fit one WebSocket fragment");
_Static_assert(PP_WS_IO_BYTES <= CONFIG_MBEDTLS_SSL_OUT_CONTENT_LEN,
               "One WebSocket fragment must fit one TLS record");

static void memory_stage(const char *stage) {
    ESP_LOGI("pp_network", "stage=%s heap=%u largest=%u low=%u", stage,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));
}

static void report_offline(const char *reason) {
    if (!standby)
        pp_network_state(false, reason, pp_link_generation(&ws_link));
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    (void)data;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(network, WIFI_READY);
        memory_stage("wifi_ready");
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        pp_traffic_new_window();
        bool had_ip = (xEventGroupClearBits(network, WIFI_READY) & WIFI_READY) != 0;
        xEventGroupSetBits(network, WIFI_FAILED | (had_ip ? CONTROL : 0));
        pp_link_close(&ws_link, pp_link_generation(&ws_link));
        if (had_ip && !provision && !standby)
            report_offline("");
    }
}
static uint32_t wire_word(const unsigned char *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}
static bool store_binary(const esp_websocket_event_data_t *event) {
    const char *data = event->data_ptr;
    size_t bytes = event->data_len;
    bool prepend = false;
    if (!binary_classified) {
        unsigned take = sizeof(binary_prefix) - prefix_size;
        if (take > bytes)
            take = bytes;
        if (take) {
            memcpy(binary_prefix + prefix_size, data, take);
            data += take;
            bytes -= take;
            prefix_size += take;
        }
        bool finished = event->fin && frame_used + event->data_len == (size_t)event->payload_len;
        if (prefix_size < sizeof(binary_prefix) && !finished)
            return true;
        prepend = binary_classified = true;
        image_binary = pp_image_strip_tag(binary_prefix, prefix_size);
        if (image_binary && (prefix_size < sizeof(binary_prefix) ||
            !wire_word(binary_prefix + 4) || wire_word(binary_prefix + 4) != atomic_load(&expected_image)))
            drop_image = true;
    }
    size_t required = used + (size_t)event->payload_len - frame_used;
    if (drop_image || (image_binary && required > PP_IMAGE_STRIP_MAX))
        return false;
    if (prepend && image_binary && received.capacity > PP_IMAGE_STRIP_MAX)
        pp_buffer_free(&received);
    if (required > received.capacity &&
        !pp_buffer_reserve(&received, required, image_binary ? PP_MEMORY_NORMAL : PP_MEMORY_CRITICAL,
                           image_binary ? PP_MEMORY_IMAGE : PP_MEMORY_AUDIO))
        return false;
    if (prepend && prefix_size) {
        memcpy(received.data, binary_prefix, prefix_size);
        received.size = prefix_size;
    }
    if (bytes) {
        memcpy(received.data + received.size, data, bytes);
        received.size += bytes;
    }
    return true;
}
static void socket_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    uint32_t generation = (uint32_t)(uintptr_t)arg;
    (void)base;
    if (generation != pp_link_generation(&ws_link))
        return;
    if (id == WEBSOCKET_EVENT_CONNECTED) {
        if (!pp_link_open(&ws_link, generation))
            return;
        used = 0;
        opcode = 0;
        atomic_store(&expected_image, 0);
        pp_buffer_free(&received);
        memory_stage("ws_connected");
        pp_network_state(false, "正在同步会话", generation);
        return;
    }
    if (id == WEBSOCKET_EVENT_DISCONNECTED || id == WEBSOCKET_EVENT_ERROR) {
        pp_link_close(&ws_link, generation);
        used = 0;
        opcode = 0;
        pp_buffer_free(&received);
        if (id == WEBSOCKET_EVENT_ERROR) {
            socket_error = true;
            esp_websocket_event_data_t *event = data;
            /* 1.6.1 only initializes transport/TLS fields for TCP_TRANSPORT.
             * ERROR may arrive with NONE before DISCONNECTED supplies its type. */
            esp_websocket_error_codes_t error = {
                .error_type = event->error_handle.error_type,
                .esp_ws_handshake_status_code = event->error_handle.esp_ws_handshake_status_code};
            if (error.error_type == WEBSOCKET_ERROR_TYPE_TCP_TRANSPORT) {
                error.esp_tls_last_esp_err = event->error_handle.esp_tls_last_esp_err;
                error.esp_tls_stack_err = event->error_handle.esp_tls_stack_err;
                error.esp_tls_cert_verify_flags = event->error_handle.esp_tls_cert_verify_flags;
                error.esp_transport_sock_errno = event->error_handle.esp_transport_sock_errno;
            }
            ESP_LOGE("pp_network", "websocket esp=%d tls=%d cert_flags=%d http=%d errno=%d",
                     error.esp_tls_last_esp_err, error.esp_tls_stack_err,
                     error.esp_tls_cert_verify_flags, error.esp_ws_handshake_status_code,
                     error.esp_transport_sock_errno);
            memory_stage("ws_error");
            const char *reason = "连接失败，请查看 USB 诊断";
            if (error.esp_tls_stack_err == MBEDTLS_ERR_SSL_ALLOC_FAILED ||
                error.esp_tls_stack_err == -MBEDTLS_ERR_SSL_ALLOC_FAILED ||
                error.esp_tls_last_esp_err == ESP_ERR_NO_MEM)
                reason = "TLS 内存不足";
            else if (error.esp_tls_cert_verify_flags)
                reason = "证书验证失败";
            else if (error.esp_ws_handshake_status_code == 401) {
                rejected = true;
                reason = "PA 身份验证失败 (401)";
            } else if (error.esp_ws_handshake_status_code == 403) {
                rejected = true;
                reason = "PA 拒绝接入 (403)";
            } else if (error.esp_ws_handshake_status_code == 409)
                reason = "设备已有连接，等待重连";
            pp_network_state(false, reason, generation);
        } else if (!socket_error)
            pp_network_state(false, "连接断开，正在重连", generation);
        return;
    }
    if (id != WEBSOCKET_EVENT_DATA || !pp_link_connected(&ws_link, generation))
        return;
    esp_websocket_event_data_t *event = data;
    if (event->op_code == 8) {
        pp_link_close(&ws_link, generation);
        return;
    }
    if (event->op_code != 0 && event->op_code != 1 && event->op_code != 2)
        return;
    if (event->payload_offset == 0) {
        frame_used = 0;
        if (event->op_code && !opcode) {
            used = 0;
            received.size = 0;
            opcode = event->op_code;
            prefix_size = 0;
            binary_classified = image_binary = drop_image = false;
        } else if (event->op_code || !opcode) {
            pp_link_close(&ws_link, generation);
            pp_network_state(false, "无效消息分片", generation);
            return;
        }
    }
    size_t limit = opcode == 2 ? PP_PLAYBACK_WINDOW + 4 : PP_WIRE_MESSAGE_BYTES;
    if (event->data_len < 0 || event->payload_len < 0 ||
        event->payload_offset != (int)frame_used || frame_used > (size_t)event->payload_len ||
        (size_t)event->data_len > (size_t)event->payload_len - frame_used ||
        (size_t)event->payload_len - frame_used > limit - used) {
        pp_link_close(&ws_link, generation);
        pp_network_state(false, "消息超限", generation);
        return;
    }
    bool stored = true;
    if (opcode == 2)
        stored = store_binary(event);
    else if (event->data_len)
        stored = pp_buffer_append(&received, event->data_ptr, event->data_len, limit);
    if (!stored && image_binary) {
        pp_buffer_free(&received);
        drop_image = true; /* An optional picture cannot abort an active audio connection. */
    } else if (!stored) {
        socket_error = true;
        pp_link_close(&ws_link, generation);
        pp_buffer_free(&received);
        pp_network_state(false, "接收缓冲内存不足", generation);
        return;
    }
    used += (size_t)event->data_len;
    frame_used += event->data_len;
    if (frame_used != (size_t)event->payload_len || !event->fin)
        return;
    if (opcode == 1) {
        pp_received_frame(&received, generation);
    } else if (opcode == 2 && pp_link_ready(&ws_link) == generation && !drop_image) {
        const unsigned char *p = (const unsigned char *)received.data;
        bool valid = image_binary || (used >= 6 && used % 2 == 0);
        if (image_binary) {
            unsigned request = wire_word(p + 4);
            if (used >= PP_IMAGE_STRIP_HEADER &&
                atomic_compare_exchange_strong(&expected_image, &request, 0))
                pp_received_image(&received, generation);
        } else if (valid) {
            valid = pp_play_pcm(wire_word(p), p + 4, used - 4);
        }
        if (!valid) {
            pp_link_close(&ws_link, generation);
            pp_network_state(false, "播放缓冲超限", generation);
        }
    }
    used = 0;
    received.size = 0;
    if (received.capacity > PP_NET_REUSE_CAPACITY)
        pp_buffer_free(&received);
    opcode = 0;
}

typedef struct {
    char text[1024];
    size_t size;
    bool overflow;
} pair_response_t;
static esp_err_t pair_event(esp_http_client_event_t *event) {
    pair_response_t *r = event->user_data;
    if (event->event_id == HTTP_EVENT_ON_DATA) {
        if (event->data_len < 0 || r->size + event->data_len >= sizeof(r->text)) {
            r->overflow = true;
            return ESP_FAIL;
        }
        memcpy(r->text + r->size, event->data, event->data_len);
        r->size += event->data_len;
        r->text[r->size] = 0;
    }
    return ESP_OK;
}
static bool pairing(pp_connection_settings_t *settings, const char *pem) {
    char address[600];
    if (!pp_server_endpoint(settings->url, "pair", false, address, sizeof(address)))
        return false;
    cJSON *body = cJSON_CreateObject();
    cJSON_AddNumberToObject(body, "protocol", PP_PROTOCOL_VERSION);
    cJSON_AddStringToObject(body, "device_id", settings->device);
    cJSON_AddStringToObject(body, "credential", settings->credential);
    char *raw = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    if (!raw)
        return false;
    pair_response_t response = {0};
    bool secure = !strncmp(address, "https://", 8);
    esp_http_client_config_t cfg = {.url = address,
                                    .event_handler = pair_event,
                                    .user_data = &response,
                                    .timeout_ms = 10000,
                                    .disable_auto_redirect = true,
                                    .buffer_size = 1024,
                                    .cert_pem = secure ? pem : NULL,
                                    .crt_bundle_attach =
                                        secure && !pem ? esp_crt_bundle_attach : NULL};
    esp_http_client_handle_t http = esp_http_client_init(&cfg);
    bool paired = false;
    if (http) {
        esp_http_client_set_method(http, HTTP_METHOD_POST);
        esp_http_client_set_header(http, "Content-Type", "application/json");
        esp_http_client_set_post_field(http, raw, strlen(raw));
        esp_err_t e = esp_http_client_perform(http);
        int status = esp_http_client_get_status_code(http);
        ESP_LOGI("pp_network", "pair esp=%d http=%d", e, status);
        esp_http_client_cleanup(http);
        if (status == 401 || status == 403) {
            rejected = true;
            report_offline(status == 401 ? "PA 身份验证失败 (401)" : "PA 拒绝接入 (403)");
        }
        if (e == ESP_OK && status == 200 && !response.overflow) {
            cJSON *answer = cJSON_Parse(response.text),
                  *protocol = cJSON_GetObjectItemCaseSensitive(answer, "protocol");
            if (cJSON_IsNumber(protocol) && protocol->valuedouble == PP_PROTOCOL_VERSION) {
                paired = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(answer, "paired"));
                cJSON *code = cJSON_GetObjectItemCaseSensitive(answer, "code");
                if (!paired && cJSON_IsString(code) && strlen(code->valuestring) == 8) {
                    cJSON *display = cJSON_CreateObject();
                    cJSON_AddStringToObject(display, "type", "pairing");
                    cJSON_AddStringToObject(display, "code", code->valuestring);
                    char *text = cJSON_PrintUnformatted(display);
                    cJSON_Delete(display);
                    if (text) {
                        pp_received(text, strlen(text));
                        free(text);
                    }
                }
            }
            cJSON_Delete(answer);
        } else if (!rejected)
            report_offline("PA 暂不可达，稍后重连");
    }
    memset(raw, 0, strlen(raw));
    free(raw);
    if (paired && pp_preference(PP_PREF_PAIRED, 1, NULL) == ESP_OK) {
        settings->paired = true;
        return true;
    }
    return false;
}
static bool interruptible_wait(unsigned ms) {
    pp_objects_connecting(false);
    const bool had_wifi = pp_net_wifi_connected();
    const TickType_t started = xTaskGetTickCount(), duration = pdMS_TO_TICKS(ms);
    TickType_t elapsed;
    while (!stopped && !standby && !reconfigure && !provision && !retry && !repair &&
           wifi_phase != WIFI_RETRY_PENDING &&
           (!had_wifi || pp_net_wifi_connected()) &&
           (elapsed = xTaskGetTickCount() - started) < duration)
        xEventGroupWaitBits(network, CONTROL, pdTRUE, pdFALSE, duration - elapsed);
    return !stopped;
}
static bool disconnect_wifi(void) {
    wifi_ap_record_t ap;
    const bool unassociated = esp_wifi_sta_get_ap_info(&ap) == ESP_ERR_WIFI_NOT_CONNECT;
    xEventGroupClearBits(network, WIFI_FAILED);
    if (esp_wifi_disconnect() != ESP_OK)
        return false;
    /* An already disconnected station, including a cancelled association attempt,
     * does not necessarily post STA_DISCONNECTED. Still verify the final state. */
    if (unassociated && esp_wifi_sta_get_ap_info(&ap) == ESP_ERR_WIFI_NOT_CONNECT) {
        xEventGroupClearBits(network, WIFI_READY);
        return true;
    }
    EventBits_t bits =
        xEventGroupWaitBits(network, WIFI_FAILED, pdFALSE, pdFALSE, pdMS_TO_TICKS(2000));
    return (bits & WIFI_FAILED) != 0;
}
static bool connect_wifi(const pp_connection_settings_t *s, pp_wifi_plan_t *plan) {
    if (wifi_uncertain) {
        if (!disconnect_wifi())
            return false;
        wifi_uncertain = false;
    }
    plan->count = s->wifi_count;
    plan->attempt = 0;
    unsigned index;
    while (!stopped && !standby && !provision && !reconfigure && !retry && pp_wifi_next(plan, &index)) {
        xEventGroupClearBits(network, WIFI_READY | WIFI_FAILED);
        wifi_config_t cfg = {0};
        memcpy(cfg.sta.ssid, s->wifi[index].ssid, strlen(s->wifi[index].ssid));
        memcpy(cfg.sta.password, s->wifi[index].password, strlen(s->wifi[index].password));
        ESP_LOGI("pp_network", "wifi_attempt=%u profile=%u", plan->attempt, index + 1);
        esp_err_t e = esp_wifi_set_config(WIFI_IF_STA, &cfg);
        memset(&cfg, 0, sizeof(cfg));
        if (e == ESP_OK)
            e = esp_wifi_connect();
        if (e != ESP_OK)
            continue;
        EventBits_t bits = 0;
        for (unsigned waited = 0; waited < 100 && !stopped && !standby && !provision && !reconfigure && !retry;
             waited++) {
            bits = xEventGroupWaitBits(network, WIFI_READY | WIFI_FAILED | CONTROL, pdFALSE,
                                       pdFALSE, pdMS_TO_TICKS(100));
            if (bits & CONTROL)
                xEventGroupClearBits(network, CONTROL);
            if (bits & (WIFI_READY | WIFI_FAILED))
                break;
        }
        if (bits & WIFI_READY) {
            wifi_ap_record_t ap;
            if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK &&
                !strcmp((char *)ap.ssid, s->wifi[index].ssid))
                return true;
        }
        if (!(bits & WIFI_FAILED) && !disconnect_wifi()) {
            wifi_uncertain = true;
            return false;
        }
    }
    return false;
}
static void release_packet(packet_t *p) {
    if (p->binary) {
        pp_pool_return(&pcm_packets, &p->buffer);
        pcm_in_flight--;
    } else
        pp_buffer_free(&p->buffer);
}

static void discard_outgoing(void) {
    packet_t p;
    while (xQueueReceive(outgoing, &p, 0)) {
        release_packet(&p);
    }
}
static void worker(void *arg) {
    (void)arg;
    pp_buffer_t setup = {0}, certificate = {0};
    esp_sntp_config_t timecfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    bool time_started = esp_netif_sntp_init(&timecfg) == ESP_OK;
    bool invalid_address = false;
    pp_wifi_plan_t wifi_plan = {0};
    unsigned reconnect = 1000;
    while (!stopped) {
        pp_buffer_free(&setup);
        pp_buffer_free(&certificate);
        xEventGroupClearBits(network, CONTROL);
        if (standby) {
            pp_objects_connecting(false);
            if (time_started) {
                esp_netif_sntp_deinit();
                time_started = false;
            }
            /* The previous iteration has closed the PA socket. Only an
             * uncertain station transition still needs cleanup. */
            const bool detached = !wifi_uncertain || disconnect_wifi();
            wifi_uncertain = !detached;
            xEventGroupSetBits(network, detached ? STANDBY_PAUSED : STANDBY_FAILED);
            while (standby && !stopped)
                xEventGroupWaitBits(network, CONTROL, pdTRUE, pdFALSE, portMAX_DELAY);
            xEventGroupClearBits(network, STANDBY_PAUSED | STANDBY_FAILED);
            reconnect = 1000;
            if (!stopped)
                time_started = esp_netif_sntp_init(&timecfg) == ESP_OK;
            continue;
        }
        if (atomic_exchange(&repair, false) && rejected) {
            if (pp_settings_new_identity() == ESP_OK)
                esp_restart();
            report_offline("配对身份保存失败，请重试");
        }
        if (atomic_exchange(&retry, false)) {
            invalid_address = false;
            wifi_plan.cursor = 0;
            rejected = false;
        }
        if (atomic_exchange(&reconfigure, false)) {
            invalid_address = false;
            wifi_plan.cursor = 0;
            rejected = false;
            if ((xEventGroupGetBits(network) & WIFI_READY) && !disconnect_wifi()) {
                wifi_uncertain = true;
            }
        }
        bool buffer_ready = pp_buffer_reserve(&setup, sizeof(pp_connection_settings_t),
                                             PP_MEMORY_CRITICAL, PP_MEMORY_NETWORK_SETUP);
        pp_connection_settings_t *settings = (void *)setup.data;
        esp_err_t snapshot = buffer_ready ? pp_settings_connection(settings, &certificate)
                                          : ESP_ERR_NO_MEM;
        if (snapshot != ESP_OK) {
            pp_buffer_free(&setup);
            report_offline(snapshot == ESP_ERR_NO_MEM ? "网络配置内存不足" : "设置读取失败");
            interruptible_wait(3000);
            continue;
        }
        if (!settings->wifi_count || provision) {
            bool has_wifi = settings->wifi_count != 0;
            pp_buffer_free(&setup);
            pp_buffer_free(&certificate);
            provision = false;
            rejected = false;
            if ((xEventGroupGetBits(network) & WIFI_READY) && !disconnect_wifi()) {
                report_offline("Wi-Fi 未正常断开，请重启");
                break;
            }
            pp_objects_connecting(false);
            esp_err_t e = pp_provision_start();
            if (e != ESP_OK) {
                report_offline("热点启动失败，请重启");
                break;
            }
            while (!stopped && !standby && !pp_provision_complete() && !(retry && has_wifi))
                xEventGroupWaitBits(network, CONTROL, pdTRUE, pdFALSE, pdMS_TO_TICKS(250));
            if (pp_provision_stop() != ESP_OK) {
                report_offline("热点未正常关闭，请重启");
                break;
            }
            wifi_plan.cursor = 0;
            invalid_address = false;
            continue;
        }
        if (wifi_uncertain || !pp_net_wifi_connected()) {
            const uint32_t started = esp_timer_get_time() / 1000;
            pp_objects_connecting(true);
            wifi_phase = WIFI_SEARCHING;
            bool connected = connect_wifi(settings, &wifi_plan);
            if (!connected) {
                wifi_phase = WIFI_WAITING;
                pp_buffer_free(&setup);
                pp_buffer_free(&certificate);
                if (wifi_uncertain)
                    ESP_LOGW("pp_network", "Wi-Fi disconnect not confirmed; deferring association");
                interruptible_wait(pp_wifi_retry_remaining(started, esp_timer_get_time() / 1000));
                wifi_phase = WIFI_IDLE;
                continue;
            }
            wifi_phase = WIFI_IDLE;
            wifi_plan.cursor = 0;
            reconnect = 1000;
        }
        if (rejected || invalid_address) {
            pp_buffer_free(&setup);
            pp_buffer_free(&certificate);
            pp_objects_connecting(false);
            while (!stopped && !standby && !retry && !provision && !reconfigure && !repair &&
                   pp_net_wifi_connected())
                xEventGroupWaitBits(network, CONTROL, pdTRUE, pdFALSE, pdMS_TO_TICKS(1000));
            continue;
        }
        pp_objects_connecting(true);
        bool secure = !strncmp(settings->url, "https://", 8);
        if (secure && time(NULL) < 1700000000) {
            report_offline("HTTPS 正在校准时间");
            for (unsigned i = 0;
                 i < 150 && !stopped && !standby && !provision && !reconfigure && time(NULL) < 1700000000; i++)
                vTaskDelay(pdMS_TO_TICKS(100));
            if (time(NULL) < 1700000000) {
                pp_buffer_free(&setup);
                pp_buffer_free(&certificate);
                report_offline("NTP 未就绪，HTTPS 暂不可用");
                interruptible_wait(10000);
                continue;
            }
        }
        if (stopped || standby || provision || reconfigure)
            continue;
        if (!settings->paired && !pairing(settings, certificate.data)) {
            pp_buffer_free(&setup);
            pp_buffer_free(&certificate);
            interruptible_wait(3000);
            continue;
        }
        char uri[600], headers[180];
        if (!pp_server_endpoint(settings->url, "stream", true, uri, sizeof(uri))) {
            report_offline("PA 地址无效，请检查设置");
            invalid_address = true;
            continue;
        }
        snprintf(headers, sizeof(headers), "Authorization: Bearer %s\r\nX-Device-Id: %s\r\n",
                 settings->credential, settings->device);
        pp_buffer_free(&setup);
        pp_buffer_free(&received);
        socket_error = false;
        memory_stage("before_ws");
        esp_websocket_client_config_t cfg = {
            .uri = uri,
            .headers = headers,
            .disable_auto_reconnect = true,
            .buffer_size = PP_WS_IO_BYTES,
            .task_stack = 6144,
            /* C3 is single-core. In locked client 1.6.1, start clears STOPPED
             * after task creation. Keep that task below its owner so a fast
             * connect failure cannot finish before start clears the bit. */
            .task_prio = PP_NETWORK_TASK_PRIORITY - 1,
            .network_timeout_ms = 10000,
            .ping_interval_sec = 15,
            .cert_pem = certificate.data,
            .crt_bundle_attach = secure && !certificate.data ? esp_crt_bundle_attach : NULL};
        client = esp_websocket_client_init(&cfg);
        memset(headers, 0, sizeof(headers));
        if (!client) {
            pp_buffer_free(&certificate);
            report_offline("连接内存不足");
            interruptible_wait(5000);
            continue;
        }
        uint32_t generation = pp_link_begin(&ws_link);
        esp_websocket_register_events(client, WEBSOCKET_EVENT_ANY, socket_event,
                                      (void *)(uintptr_t)generation);
        if (esp_websocket_client_start(client) == ESP_OK) {
            for (unsigned n = 0;
                 n < 150 && !pp_link_connected(&ws_link, generation) && !socket_error && !stopped && !standby && !provision && !reconfigure;
                 n++)
                vTaskDelay(pdMS_TO_TICKS(100));
        }
        TickType_t hello_started = xTaskGetTickCount();
        if (pp_link_connected(&ws_link, generation) && !pp_link_ready(&ws_link) &&
            !stopped && !standby && !provision && !reconfigure) {
            /* IDF 5.5.3 can buffer the first frame with HTTP 101 while polling only
             * the socket. A protocol ping makes the peer's pong wake that reader. */
            static const uint8_t ping = 0;
            (void)esp_websocket_client_send_with_opcode(client, WS_TRANSPORT_OPCODES_PING,
                                                        &ping, sizeof(ping), pdMS_TO_TICKS(1000));
        }
        unsigned draining = 0;
        while (pp_link_connected(&ws_link, generation) && !stopped && !standby && !provision) {
            if (reconfigure && !uxQueueMessagesWaiting(outgoing))
                break;
            if (pp_link_ready(&ws_link) == generation)
                reconnect = 1000;
            else if (xTaskGetTickCount() - hello_started >= pdMS_TO_TICKS(PP_HELLO_TIMEOUT_MS)) {
                pp_link_close(&ws_link, generation);
                report_offline("PA 会话同步超时，稍后重连");
                break;
            }
            packet_t p;
            if (xQueueReceive(outgoing, &p, pdMS_TO_TICKS(100))) {
                if (p.generation != generation || pp_link_ready(&ws_link) != generation) {
                    release_packet(&p);
                    continue;
                }
                TickType_t timeout = pdMS_TO_TICKS(PP_WS_WRITE_TIMEOUT_MS);
                int sent = p.binary ? esp_websocket_client_send_bin(client, p.buffer.data, p.buffer.size,
                                                                    timeout)
                                    : esp_websocket_client_send_text(client, p.buffer.data, p.buffer.size,
                                                                     timeout);
                if (sent != (int)p.buffer.size) {
                    if (p.binary) {
                        ESP_LOGW("pp_network", "audio_send_failed result=%d bytes=%u", sent,
                                 (unsigned)p.buffer.size);
                        memory_stage("audio_send_failed");
                    }
                    pp_link_close(&ws_link, generation);
                    report_offline("发送中断，等待核对");
                }
                release_packet(&p);
            }
            if (reconfigure && ++draining >= 16)
                break;
        }
        pp_link_close(&ws_link, generation);
        esp_websocket_client_stop(client);
        esp_websocket_client_destroy(client);
        client = NULL;
        pp_buffer_free(&certificate);
        pp_buffer_free(&received);
        discard_outgoing();
        if (!stopped && pp_net_wifi_connected()) {
            interruptible_wait(reconnect);
            reconnect = reconnect < 16000 ? reconnect * 2 : 30000;
        }
    }
    if (time_started)
        esp_netif_sntp_deinit();
    pp_buffer_free(&setup);
    pp_buffer_free(&certificate);
    pp_objects_connecting(false);
    if (pp_provision_active())
        pp_provision_stop();
    pp_link_close(&ws_link, pp_link_generation(&ws_link));
    esp_wifi_disconnect();
    esp_wifi_stop();
    discard_outgoing();
    xEventGroupSetBits(network, WORKER_DONE);
    vTaskDelete(NULL);
}
static bool enqueue(pp_buffer_t *data, bool binary, uint32_t generation) {
    if (!data->data)
        return false;
    packet_t p = {pp_buffer_take(data), binary, generation};
    if (binary)
        pcm_in_flight++;
    if (!generation || generation != pp_link_ready(&ws_link) || !outgoing ||
        p.buffer.size > PP_WIRE_MESSAGE_BYTES || xQueueSend(outgoing, &p, 0) != pdTRUE) {
        release_packet(&p);
        return false;
    }
    return true;
}
bool pp_net_send(cJSON *command) {
    char *raw = cJSON_PrintUnformatted(command);
    cJSON_Delete(command);
    if (!raw)
        return false;
    size_t bytes = strlen(raw);
    pp_buffer_t buffer = pp_buffer_adopt(raw, bytes, bytes + 1, PP_MEMORY_WIRE);
    return enqueue(&buffer, false, pp_link_ready(&ws_link));
}
void pp_net_expect_image(uint32_t request) {
    atomic_store(&expected_image, request);
}
bool pp_net_open_audio(void) {
    return pp_pool_open(&pcm_packets, PP_INPUT_TAGGED_HEADER_BYTES + PP_INPUT_BATCH_BYTES, PP_MEMORY_AUDIO);
}
void pp_net_close_audio(void) { pp_pool_close(&pcm_packets); }
void pp_net_return_audio(pp_buffer_t *packet) { pp_pool_return(&pcm_packets, packet); }
size_t pp_net_audio_header(const uint8_t *input_id, uint32_t sequence, pp_buffer_t *packet,
                           size_t pcm_capacity) {
    size_t prefix = input_id ? PP_INPUT_TAGGED_HEADER_BYTES - 4 : 0;
    if (!pcm_capacity || pcm_capacity > PP_INPUT_BATCH_BYTES || pcm_capacity % 2)
        return 0;
    *packet = pp_pool_take(&pcm_packets);
    if (!packet->data)
        return 0;
    unsigned char *data = (unsigned char *)packet->data;
    if (input_id) {
        memset(data, 0xff, 4);
        memcpy(data + 4, input_id, PP_INPUT_AUDIO_ID_BYTES);
    }
    data[prefix] = sequence >> 24;
    data[prefix + 1] = sequence >> 16;
    data[prefix + 2] = sequence >> 8;
    data[prefix + 3] = sequence;
    return prefix + 4;
}
bool pp_net_send_audio(pp_buffer_t *packet, uint32_t generation) {
    if (pp_net_has_capacity(true))
        return enqueue(packet, true, generation);
    pp_net_return_audio(packet);
    return false;
}
bool pp_net_has_capacity(bool pcm) {
    /* The application produces both PCM and commands. Leave room for the
     * terminal command even when the network worker is draining slowly. */
    return pp_net_connected() && outgoing && uxQueueSpacesAvailable(outgoing) > (pcm ? 1 : 0) &&
           (!pcm || pcm_in_flight < pcm_packets.count);
}
void pp_net_interrupt_input(void) {
    uint32_t generation = pp_link_ready(&ws_link);
    if (generation) {
        pp_link_close(&ws_link, generation);
        pp_network_state(false, "收音连接中断，正在重连", generation);
    }
}
bool pp_net_connected(void) { return pp_link_ready(&ws_link) != 0; }
uint32_t pp_net_generation(void) { return pp_link_generation(&ws_link); }
bool pp_net_current_frame(uint32_t generation) { return pp_link_connected(&ws_link, generation); }
bool pp_net_accept_protocol(unsigned protocol, uint32_t generation) {
    bool ready = pp_link_accept(&ws_link, generation, protocol);
    if (ready)
        pp_objects_connecting(false);
    return ready;
}
bool pp_net_rejected(void) { return rejected; }
void pp_net_repair(void) {
    if (network && rejected) {
        repair = true;
        xEventGroupSetBits(network, CONTROL);
    }
}
void pp_net_retry(void) {
    pp_objects_connecting(true);
    retry = true;
    if (network)
        xEventGroupSetBits(network, CONTROL);
}
void pp_net_retry_wifi(void) {
    if (!network || stopped || standby || provision || pp_net_wifi_connected())
        return;
    /* Only the worker's cooldown can be interrupted. One request is enough. */
    wifi_phase_t waiting = WIFI_WAITING;
    if (atomic_compare_exchange_strong(&wifi_phase, &waiting, WIFI_RETRY_PENDING))
        xEventGroupSetBits(network, CONTROL);
}
void pp_net_reload(void) {
    reconfigure = true;
    if (network)
        xEventGroupSetBits(network, CONTROL);
}
void pp_net_provision(void) {
    provision = true;
    if (network)
        xEventGroupSetBits(network, CONTROL);
}
bool pp_net_enter_standby(void) {
    if (!network || stopped || pp_provision_active() || provision)
        return false;
    xEventGroupClearBits(network, STANDBY_PAUSED | STANDBY_FAILED);
    standby = true;
    pp_link_close(&ws_link, pp_link_generation(&ws_link));
    xEventGroupSetBits(network, CONTROL);
    EventBits_t bits = xEventGroupWaitBits(network, STANDBY_PAUSED | STANDBY_FAILED, pdFALSE,
                                         pdFALSE, pdMS_TO_TICKS(12000));
    if (bits & STANDBY_PAUSED)
        return true;
    pp_net_leave_standby();
    return false;
}
void pp_net_leave_standby(void) {
    standby = false;
    xEventGroupSetBits(network, CONTROL);
}
bool pp_net_in_standby(void) {
    return standby && network && (xEventGroupGetBits(network) & STANDBY_PAUSED);
}
bool pp_net_stop(void) {
    stopped = true;
    pp_link_close(&ws_link, pp_link_generation(&ws_link));
    if (!network)
        return true;
    xEventGroupSetBits(network, CONTROL);
    return (xEventGroupWaitBits(network, WORKER_DONE, pdFALSE, pdTRUE, pdMS_TO_TICKS(12000)) &
            WORKER_DONE) != 0;
}
bool pp_net_start(void) {
    esp_log_level_set("transport_ws", ESP_LOG_NONE); /* Never log HTTP upgrade credentials. */
    outgoing = xQueueCreate(16, sizeof(packet_t));
    network = xEventGroupCreate();
    if (!outgoing || !network)
        return false;
    if (esp_netif_init() != ESP_OK || esp_event_loop_create_default() != ESP_OK)
        return false;
    esp_netif_t *sta = esp_netif_create_default_wifi_sta();
    pp_traffic_attach(sta);
    if (!sta || !esp_netif_create_default_wifi_ap())
        return false;
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    if (esp_wifi_init(&init) != ESP_OK || esp_wifi_set_storage(WIFI_STORAGE_RAM) != ESP_OK ||
        esp_wifi_set_mode(WIFI_MODE_STA) != ESP_OK ||
        esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL) != ESP_OK ||
        esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL) != ESP_OK ||
        esp_wifi_start() != ESP_OK)
        return false;
    return xTaskCreate(worker, "pp_network", 6144, NULL, PP_NETWORK_TASK_PRIORITY, NULL) == pdPASS;
}
bool pp_net_wifi_connected(void) {
    return network && (xEventGroupGetBits(network) & WIFI_READY);
}
bool pp_net_wifi_searching(void) { return wifi_phase == WIFI_SEARCHING; }
bool pp_net_wifi_details(char *ssid, size_t capacity, int *rssi) {
    if (!pp_net_wifi_connected())
        return false;
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK)
        return false;
    snprintf(ssid, capacity, "%s", (char *)ap.ssid);
    *rssi = ap.rssi;
    return true;
}

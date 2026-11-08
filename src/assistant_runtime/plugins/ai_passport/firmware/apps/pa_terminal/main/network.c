#include "network.h"
#include "settings.h"
#include "byte_buffer.h"
#include "provision.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "mbedtls/ssl.h"
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

typedef struct {
    void *data;
    size_t bytes;
    bool binary;
} packet_t;
static esp_websocket_client_handle_t client;
static QueueHandle_t outgoing;
static EventGroupHandle_t network;
static atomic_bool connected, ready, stopped, reconfigure, retry, provision, rejected;
#define WIFI_READY BIT0
#define WIFI_FAILED BIT1
#define CONTROL BIT2
#define WORKER_DONE BIT3
static pp_buffer_t received;
static size_t used, frame_used;
static int opcode;
static atomic_bool socket_error;
static bool wifi_uncertain; /* Owned only by the network worker. */
#define PP_NETWORK_TASK_PRIORITY 3

static void memory_stage(const char *stage) {
    ESP_LOGI("pp_network", "stage=%s heap=%u largest=%u low=%u", stage,
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
             (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_8BIT));
}

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    (void)data;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        xEventGroupSetBits(network, WIFI_READY);
        memory_stage("wifi_ready");
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(network, WIFI_READY);
        xEventGroupSetBits(network, WIFI_FAILED);
        connected = ready = false;
        if (!provision)
            pp_network_state(false, "网络断开");
    }
}
static void socket_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    (void)base;
    if (id == WEBSOCKET_EVENT_CONNECTED) {
        connected = true;
        ready = false;
        used = 0;
        opcode = 0;
        pp_buffer_free(&received);
        memory_stage("ws_connected");
        pp_network_state(false, "正在同步会话");
        return;
    }
    if (id == WEBSOCKET_EVENT_DISCONNECTED || id == WEBSOCKET_EVENT_ERROR) {
        connected = ready = false;
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
                reason = "设备授权失效，请重新配对";
            } else if (error.esp_ws_handshake_status_code == 403)
                reason = "接入被拒绝";
            else if (error.esp_ws_handshake_status_code == 409)
                reason = "设备已有连接，等待重连";
            pp_network_state(false, reason);
        } else if (!socket_error)
            pp_network_state(false, "连接断开，正在重连");
        return;
    }
    if (id != WEBSOCKET_EVENT_DATA)
        return;
    esp_websocket_event_data_t *event = data;
    if (event->op_code == 8) {
        connected = false;
        return;
    }
    if (event->op_code != 0 && event->op_code != 1 && event->op_code != 2)
        return;
    if (event->payload_offset == 0) {
        frame_used = 0;
        if (event->op_code) {
            used = 0;
            received.size = 0;
            opcode = event->op_code;
        } else if (!opcode) {
            connected = false;
            pp_network_state(false, "无效消息分片");
            return;
        }
    }
    if (event->data_len < 0 || event->payload_len < 0 || event->payload_len > 16384 ||
        event->payload_offset != (int)frame_used || used + event->data_len > 16384) {
        connected = false;
        pp_network_state(false, "消息超限");
        return;
    }
    if (event->data_len && !pp_buffer_append(&received, event->data_ptr, event->data_len, 16384)) {
        socket_error = true;
        connected = false;
        pp_buffer_free(&received);
        pp_network_state(false, "接收缓冲内存不足");
        return;
    }
    used = received.size;
    frame_used += event->data_len;
    if (frame_used != (size_t)event->payload_len || !event->fin)
        return;
    if (opcode == 1) {
        pp_received(received.data ? received.data : "", used);
    } else if (opcode == 2 && used >= 6 && used % 2 == 0) {
        unsigned char *p = (unsigned char *)received.data;
        uint32_t generation =
            ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
        if (!pp_play_pcm(generation, p + 4, used - 4)) {
            connected = false;
            pp_network_state(false, "播放缓冲超限");
        }
    }
    used = 0;
    received.size = 0;
    if (received.capacity > 4096)
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
static bool pairing(pp_settings_t *settings) {
    char address[600];
    if (!pp_server_endpoint(settings->url, "pair", false, address, sizeof(address)))
        return false;
    cJSON *body = cJSON_CreateObject();
    cJSON_AddNumberToObject(body, "protocol", 2);
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
                                    .cert_pem = secure && settings->ca[0] ? settings->ca : NULL,
                                    .crt_bundle_attach =
                                        secure && !settings->ca[0] ? esp_crt_bundle_attach : NULL};
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
            pp_network_state(false, "配对已撤销，请恢复出厂后重新配对");
        }
        if (e == ESP_OK && status == 200 && !response.overflow) {
            cJSON *answer = cJSON_Parse(response.text),
                  *protocol = cJSON_GetObjectItemCaseSensitive(answer, "protocol");
            if (cJSON_IsNumber(protocol) && protocol->valuedouble == 2) {
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
            pp_network_state(false, "PA 暂不可达，稍后重连");
    }
    memset(raw, 0, strlen(raw));
    free(raw);
    if (paired && pp_preference("paired", 1) == ESP_OK) {
        settings->paired = true;
        return true;
    }
    return false;
}
static bool interruptible_wait(unsigned ms) {
    unsigned elapsed = 0;
    while (!stopped && !reconfigure && !provision && !retry && elapsed < ms) {
        unsigned part = ms - elapsed > 100 ? 100 : ms - elapsed;
        xEventGroupWaitBits(network, CONTROL, pdTRUE, pdFALSE, pdMS_TO_TICKS(part));
        elapsed += part;
    }
    return !stopped;
}
static bool disconnect_wifi(void) {
    xEventGroupClearBits(network, WIFI_FAILED);
    if (esp_wifi_disconnect() != ESP_OK)
        return false;
    EventBits_t bits =
        xEventGroupWaitBits(network, WIFI_FAILED, pdFALSE, pdFALSE, pdMS_TO_TICKS(2000));
    return (bits & WIFI_FAILED) != 0;
}
static bool connect_wifi(const pp_settings_t *s) {
    if (wifi_uncertain) {
        if (!disconnect_wifi())
            return false;
        wifi_uncertain = false;
    }
    pp_wifi_plan_t plan = {.count = s->wifi_count};
    unsigned index, round;
    while (pp_wifi_next(&plan, &index, &round) && !stopped && !provision && !reconfigure) {
        xEventGroupClearBits(network, WIFI_READY | WIFI_FAILED);
        wifi_config_t cfg = {0};
        memcpy(cfg.sta.ssid, s->wifi[index].ssid, strlen(s->wifi[index].ssid));
        memcpy(cfg.sta.password, s->wifi[index].password, strlen(s->wifi[index].password));
        char reason[96];
        snprintf(reason, sizeof(reason), "连接 Wi-Fi %u/%u · 第 %u/3 轮", index + 1, s->wifi_count,
                 round);
        pp_network_state(false, reason);
        esp_err_t e = esp_wifi_set_config(WIFI_IF_STA, &cfg);
        memset(&cfg, 0, sizeof(cfg));
        if (e == ESP_OK)
            e = esp_wifi_connect();
        if (e != ESP_OK)
            continue;
        EventBits_t bits = 0;
        for (unsigned waited = 0; waited < 100 && !stopped && !provision && !reconfigure;
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
static void discard_outgoing(void) {
    packet_t p;
    while (xQueueReceive(outgoing, &p, 0)) {
        memset(p.data, 0, p.bytes);
        free(p.data);
    }
}
static void worker(void *arg) {
    (void)arg;
    pp_settings_t *settings = malloc(sizeof(*settings));
    if (!settings) {
        pp_network_state(false, "网络配置内存不足");
        goto done;
    }
    esp_sntp_config_t timecfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    bool time_started = esp_netif_sntp_init(&timecfg) == ESP_OK;
    bool exhausted = false;
    unsigned reconnect = 1000;
    while (!stopped) {
        xEventGroupClearBits(network, CONTROL);
        if (atomic_exchange(&retry, false)) {
            exhausted = false;
            rejected = false;
        }
        if (atomic_exchange(&reconfigure, false)) {
            exhausted = false;
            rejected = false;
            if ((xEventGroupGetBits(network) & WIFI_READY) && !disconnect_wifi()) {
                wifi_uncertain = true;
                exhausted = true;
            }
        }
        if (pp_settings_snapshot(settings) != ESP_OK) {
            pp_network_state(false, "设置读取失败");
            interruptible_wait(3000);
            continue;
        }
        if (!settings->wifi_count || provision) {
            provision = false;
            rejected = false;
            pp_network_state(false, "等待手机配网");
            if ((xEventGroupGetBits(network) & WIFI_READY) && !disconnect_wifi()) {
                pp_network_state(false, "Wi-Fi 未正常断开，请重启");
                break;
            }
            esp_err_t e = pp_provision_start();
            if (e != ESP_OK) {
                pp_network_state(false, "热点启动失败，请重启");
                break;
            }
            while (!stopped && !pp_provision_complete())
                xEventGroupWaitBits(network, CONTROL, pdTRUE, pdFALSE, pdMS_TO_TICKS(250));
            if (pp_provision_stop() != ESP_OK) {
                pp_network_state(false, "热点未正常关闭，请重启");
                break;
            }
            exhausted = false;
            continue;
        }
        if (rejected || exhausted) {
            if (exhausted)
                pp_network_state(false, wifi_uncertain
                                            ? "Wi-Fi 状态未确认，请重试或重启"
                                            : "Wi-Fi 三轮连接失败，请在菜单重试或重新配网");
            while (!stopped && !retry && !provision && !reconfigure)
                xEventGroupWaitBits(network, CONTROL, pdTRUE, pdFALSE, pdMS_TO_TICKS(1000));
            continue;
        }
        if (!(xEventGroupGetBits(network) & WIFI_READY)) {
            if (!connect_wifi(settings)) {
                if (!stopped && !provision && !reconfigure)
                    exhausted = true;
                continue;
            }
        }
        bool secure = !strncmp(settings->url, "https://", 8);
        if (secure && time(NULL) < 1700000000) {
            pp_network_state(false, "HTTPS 正在校准时间");
            for (unsigned i = 0;
                 i < 150 && !stopped && !provision && !reconfigure && time(NULL) < 1700000000; i++)
                vTaskDelay(pdMS_TO_TICKS(100));
            if (time(NULL) < 1700000000) {
                pp_network_state(false, "NTP 未就绪，HTTPS 暂不可用");
                interruptible_wait(10000);
                continue;
            }
        }
        if (stopped || provision || reconfigure)
            continue;
        if (!settings->paired && !pairing(settings)) {
            interruptible_wait(3000);
            continue;
        }
        char uri[600], headers[180];
        if (!pp_server_endpoint(settings->url, "stream", true, uri, sizeof(uri))) {
            pp_network_state(false, "PA 地址无效，请重新配网");
            exhausted = true;
            continue;
        }
        snprintf(headers, sizeof(headers), "Authorization: Bearer %s\r\nX-Device-Id: %s\r\n",
                 settings->credential, settings->device);
        pp_buffer_free(&received);
        socket_error = false;
        memory_stage("before_ws");
        esp_websocket_client_config_t cfg = {
            .uri = uri,
            .headers = headers,
            .disable_auto_reconnect = true,
            .buffer_size = 4096,
            .task_stack = 6144,
            /* C3 is single-core. In locked client 1.6.1, start clears STOPPED
             * after task creation. Keep that task below its owner so a fast
             * connect failure cannot finish before start clears the bit. */
            .task_prio = PP_NETWORK_TASK_PRIORITY - 1,
            .network_timeout_ms = 10000,
            .ping_interval_sec = 15,
            .cert_pem = secure && settings->ca[0] ? settings->ca : NULL,
            .crt_bundle_attach = secure && !settings->ca[0] ? esp_crt_bundle_attach : NULL};
        client = esp_websocket_client_init(&cfg);
        if (!client) {
            pp_network_state(false, "连接内存不足");
            interruptible_wait(5000);
            continue;
        }
        esp_websocket_register_events(client, WEBSOCKET_EVENT_ANY, socket_event, NULL);
        if (esp_websocket_client_start(client) == ESP_OK) {
            for (unsigned n = 0;
                 n < 150 && !connected && !socket_error && !stopped && !provision && !reconfigure;
                 n++)
                vTaskDelay(pdMS_TO_TICKS(100));
        }
        if (connected)
            reconnect = 1000;
        unsigned draining = 0;
        while (connected && !stopped && !provision) {
            if (reconfigure && !uxQueueMessagesWaiting(outgoing))
                break;
            packet_t p;
            if (xQueueReceive(outgoing, &p, pdMS_TO_TICKS(100))) {
                int sent = p.binary ? esp_websocket_client_send_bin(client, p.data, p.bytes,
                                                                    pdMS_TO_TICKS(1000))
                                    : esp_websocket_client_send_text(client, p.data, p.bytes,
                                                                     pdMS_TO_TICKS(1000));
                if (sent != (int)p.bytes) {
                    connected = ready = false;
                    pp_network_state(false, "发送中断，等待核对");
                }
                memset(p.data, 0, p.bytes);
                free(p.data);
            }
            if (reconfigure && ++draining >= 16)
                break;
        }
        connected = ready = false;
        esp_websocket_client_stop(client);
        esp_websocket_client_destroy(client);
        client = NULL;
        pp_buffer_free(&received);
        discard_outgoing();
        if (!stopped) {
            interruptible_wait(reconnect);
            reconnect = reconnect < 16000 ? reconnect * 2 : 30000;
        }
    }
    if (time_started)
        esp_netif_sntp_deinit();
    memset(settings, 0, sizeof(*settings));
    free(settings);
done:
    if (pp_provision_active())
        pp_provision_stop();
    connected = ready = false;
    esp_wifi_disconnect();
    esp_wifi_stop();
    discard_outgoing();
    xEventGroupSetBits(network, WORKER_DONE);
    vTaskDelete(NULL);
}
static bool enqueue(void *data, size_t bytes, bool binary) {
    if (!data)
        return false;
    packet_t p = {data, bytes, binary};
    if (!connected || !outgoing || bytes > 16384 || xQueueSend(outgoing, &p, 0) != pdTRUE) {
        memset(data, 0, bytes);
        free(data);
        return false;
    }
    return true;
}
bool pp_net_send(cJSON *command) {
    char *raw = cJSON_PrintUnformatted(command);
    cJSON_Delete(command);
    return raw ? enqueue(raw, strlen(raw), false) : false;
}
bool pp_net_pcm(uint32_t sequence, const void *pcm, size_t bytes) {
    if (!bytes || bytes > 640 || bytes % 2)
        return false;
    unsigned char *data = malloc(bytes + 4);
    if (!data)
        return false;
    data[0] = sequence >> 24;
    data[1] = sequence >> 16;
    data[2] = sequence >> 8;
    data[3] = sequence;
    memcpy(data + 4, pcm, bytes);
    return enqueue(data, bytes + 4, true);
}
bool pp_net_connected(void) { return ready && connected; }
bool pp_net_accept_protocol(unsigned protocol) {
    if (protocol != 2 || !connected)
        return false;
    ready = true;
    return true;
}
void pp_net_retry(void) {
    retry = true;
    if (network)
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
bool pp_net_stop(void) {
    stopped = true;
    ready = connected = false;
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
    if (!esp_netif_create_default_wifi_sta() || !esp_netif_create_default_wifi_ap())
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
bool pp_net_wifi_details(char *ssid, size_t capacity, int *rssi) {
    if (!network || !(xEventGroupGetBits(network) & WIFI_READY))
        return false;
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK)
        return false;
    snprintf(ssid, capacity, "%s", (char *)ap.ssid);
    *rssi = ap.rssi;
    return true;
}

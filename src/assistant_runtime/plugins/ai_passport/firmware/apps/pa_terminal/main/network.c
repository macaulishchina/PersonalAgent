#include "network.h"
#include "esp_event.h"
#include "esp_http_client.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/queue.h"
#include "freertos/task.h"
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
static atomic_bool connected, stopped;
static pp_settings_t *settings;
static char received[16385];
static size_t used, frame_used;
static int opcode;

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    (void)data;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP)
        xEventGroupSetBits(network, 1);
    else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(network, 1);
        connected = false;
        pp_network_state(false, "网络断开");
    }
}
static void socket_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
    (void)arg;
    (void)base;
    if (id == WEBSOCKET_EVENT_CONNECTED) {
        connected = true;
        used = 0;
        pp_network_state(true, "已连接");
        return;
    }
    if (id == WEBSOCKET_EVENT_DISCONNECTED || id == WEBSOCKET_EVENT_ERROR) {
        connected = false;
        used = 0;
        pp_network_state(false, "连接断开");
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
            opcode = event->op_code;
        } else if (!opcode) {
            connected = false;
            pp_network_state(false, "无效消息分片");
            return;
        }
    }
    if (event->payload_len > 16384 || event->payload_offset != (int)frame_used ||
        used + event->data_len > 16384) {
        connected = false;
        pp_network_state(false, "消息超限");
        return;
    }
    memcpy(received + used, event->data_ptr, event->data_len);
    used += event->data_len;
    frame_used += event->data_len;
    if (frame_used != (size_t)event->payload_len || !event->fin)
        return;
    if (opcode == 1) {
        received[used] = 0;
        pp_received(received, used);
    } else if (opcode == 2 && used >= 6 && used % 2 == 0) {
        unsigned char *p = (unsigned char *)received;
        uint32_t generation =
            ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
        if (!pp_play_pcm(generation, p + 4, used - 4)) {
            connected = false;
            pp_network_state(false, "播放缓冲超限");
        }
    }
    used = 0;
    opcode = 0;
}
typedef struct {
    char text[512];
    size_t size;
    bool overflow;
} pair_response_t;
static esp_err_t pair_event(esp_http_client_event_t *event) {
    pair_response_t *response = event->user_data;
    if (event->event_id == HTTP_EVENT_ON_DATA) {
        if (event->data_len < 0 || response->size + event->data_len >= sizeof(response->text)) {
            response->overflow = true;
            return ESP_FAIL;
        }
        memcpy(response->text + response->size, event->data, event->data_len);
        response->size += event->data_len;
        response->text[response->size] = 0;
    }
    return ESP_OK;
}
static bool pair(void) {
    char address[513];
    snprintf(address, sizeof(address), "https://%s", settings->url + 6);
    char *tail = strrchr(address, '/');
    if (!tail || strcmp(tail, "/stream"))
        return false;
    strcpy(tail, "/pair");
    cJSON *body = cJSON_CreateObject();
    cJSON_AddNumberToObject(body, "protocol", 1);
    cJSON_AddStringToObject(body, "device_id", settings->device);
    cJSON_AddStringToObject(body, "credential", settings->credential);
    cJSON_AddStringToObject(body, "pairing_token", settings->pairing);
    char *raw = cJSON_PrintUnformatted(body);
    cJSON_Delete(body);
    if (!raw)
        return false;
    pair_response_t response = {0};
    esp_http_client_config_t cfg = {.event_handler = pair_event,
                                    .user_data = &response,
                                    .url = address,
                                    .cert_pem = settings->ca,
                                    .timeout_ms = 15000,
                                    .disable_auto_redirect = true,
                                    .buffer_size = 1024};
    esp_http_client_handle_t http = esp_http_client_init(&cfg);
    bool ok = false;
    if (http) {
        esp_http_client_set_method(http, HTTP_METHOD_POST);
        esp_http_client_set_header(http, "Content-Type", "application/json");
        esp_http_client_set_post_field(http, raw, strlen(raw));
        ok =
            esp_http_client_perform(http) == ESP_OK && esp_http_client_get_status_code(http) == 200;
        esp_http_client_cleanup(http);
        cJSON *answer = cJSON_Parse(response.text),
              *protocol = cJSON_GetObjectItemCaseSensitive(answer, "protocol");
        ok = ok && !response.overflow &&
             cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(answer, "paired")) &&
             cJSON_IsNumber(protocol) && protocol->valuedouble == 1;
        cJSON_Delete(answer);
    }
    memset(raw, 0, strlen(raw));
    free(raw);
    if (ok && pp_preference("paired", 1) == ESP_OK) {
        settings->paired = true;
        return true;
    }
    return false;
}
static void worker(void *arg) {
    (void)arg;
    esp_sntp_config_t timecfg = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
    esp_netif_sntp_init(&timecfg);
    unsigned reconnect = 1000;
    while (!stopped) {
        if (!(xEventGroupGetBits(network) & 1)) {
            esp_wifi_connect();
            xEventGroupWaitBits(network, 1, pdFALSE, pdTRUE, pdMS_TO_TICKS(10000));
            if (!(xEventGroupGetBits(network) & 1)) {
                vTaskDelay(pdMS_TO_TICKS(1000));
                continue;
            }
        }
        if (time(NULL) < 1700000000) {
            pp_network_state(false, "同步时间");
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        if (!settings->paired && !pair()) {
            pp_network_state(false, "配对失败，请重新配置");
            vTaskDelay(pdMS_TO_TICKS(30000));
            continue;
        }
        char headers[300];
        snprintf(headers, sizeof(headers), "Authorization: Bearer %s\r\nX-Device-Id: %s\r\n",
                 settings->credential, settings->device);
        esp_websocket_client_config_t config = {.uri = settings->url,
                                                .cert_pem = settings->ca,
                                                .headers = headers,
                                                .disable_auto_reconnect = true,
                                                .buffer_size = 4096,
                                                .task_stack = 6144,
                                                .network_timeout_ms = 10000,
                                                .ping_interval_sec = 15};
        client = esp_websocket_client_init(&config);
        if (!client) {
            pp_network_state(false, "内存不足");
            vTaskDelay(pdMS_TO_TICKS(5000));
            continue;
        }
        esp_websocket_register_events(client, WEBSOCKET_EVENT_ANY, socket_event, NULL);
        esp_websocket_client_start(client);
        for (unsigned wait = 0; wait < 150 && !connected && !stopped; wait++)
            vTaskDelay(pdMS_TO_TICKS(100));
        if (connected)
            reconnect = 1000;
        while (connected && !stopped) {
            packet_t packet;
            if (xQueueReceive(outgoing, &packet, pdMS_TO_TICKS(100))) {
                int sent = packet.binary
                               ? esp_websocket_client_send_bin(client, packet.data, packet.bytes,
                                                               pdMS_TO_TICKS(1000))
                               : esp_websocket_client_send_text(client, packet.data, packet.bytes,
                                                                pdMS_TO_TICKS(1000));
                if (sent != (int)packet.bytes) {
                    connected = false;
                    pp_network_state(false, "发送中断，等待核对");
                }
                memset(packet.data, 0, packet.bytes);
                free(packet.data);
            }
        }
        connected = false;
        esp_websocket_client_stop(client);
        esp_websocket_client_destroy(client);
        client = NULL;
        packet_t stale;
        while (xQueueReceive(outgoing, &stale, 0)) {
            memset(stale.data, 0, stale.bytes);
            free(stale.data);
        }
        if (!stopped) {
            vTaskDelay(pdMS_TO_TICKS(reconnect));
            if (reconnect < 30000)
                reconnect *= 2;
        }
    }
    esp_netif_sntp_deinit();
    vTaskDelete(NULL);
}
static bool enqueue(void *data, size_t bytes, bool binary) {
    if (!data)
        return false;
    packet_t p = {data, bytes, binary};
    if (!connected || !outgoing || xQueueSend(outgoing, &p, 0) != pdTRUE) {
        memset(data, 0, bytes);
        free(data);
        return false;
    }
    return true;
}
bool pp_net_send(cJSON *command) {
    char *text = cJSON_PrintUnformatted(command);
    cJSON_Delete(command);
    if (!text)
        return false;
    return enqueue(text, strlen(text), false);
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
bool pp_net_connected(void) { return connected; }
void pp_net_stop(void) {
    stopped = true;
    connected = false;
}
void pp_net_start(pp_settings_t *value) {
    settings = value;
    outgoing = xQueueCreate(32, sizeof(packet_t));
    network = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    wifi_config_t config = {0};
    memcpy(config.sta.ssid, settings->ssid, strlen(settings->ssid));
    memcpy(config.sta.password, settings->password, strlen(settings->password));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
    if (!outgoing || !network || xTaskCreate(worker, "pp_network", 6144, NULL, 3, NULL) != pdPASS)
        pp_network_state(false, "内存不足");
}

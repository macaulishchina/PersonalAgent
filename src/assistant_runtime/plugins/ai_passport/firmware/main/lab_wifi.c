#include "lab_language.h"
#include "lab_runtime.h"
#include "lab_model.h"
#include "esp_event.h"
#include "esp_http_server.h"
#include "esp_netif.h"
#include "esp_random.h"
#include "esp_wifi.h"
#include "esp_wifi_default.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include <stdio.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include "lwip/inet.h"
#include <stdatomic.h>
#include <string.h>

static EventGroupHandle_t events;
static atomic_uint_least32_t assigned_ip;
static atomic_uint disconnect_reason;
#define GOT_IP BIT0
#define DISCONNECTED BIT1

static void network_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        atomic_store(&assigned_ip, ((ip_event_got_ip_t *)data)->ip_info.ip.addr);
        xEventGroupSetBits(events, GOT_IP);
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        atomic_store(&disconnect_reason, ((wifi_event_sta_disconnected_t *)data)->reason);
        xEventGroupSetBits(events, DISCONNECTED);
    }
}
static bool from_test_ap(httpd_req_t *r)
{
    struct sockaddr_in local = {0};
    socklen_t length = sizeof(local);
    return getsockname(httpd_req_to_sockfd(r), (struct sockaddr *)&local, &length) == 0 &&
        local.sin_family == AF_INET && local.sin_addr.s_addr == inet_addr("192.168.4.1");
}
static esp_err_t form(httpd_req_t *r)
{
    if (!from_test_ap(r)) return httpd_resp_send_err(r, HTTPD_403_FORBIDDEN, LAB_TEXT("Use the PP test hotspot", "请连接 PP 测试热点"));
#ifdef PPDEMO_ZH
    static const char html[] = "<!doctype html><html lang=zh-CN><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>PPDemo-Zh 配网</title><h1>PP 中文体验</h1>"
        "<p>请选择 2.4 GHz 网络。名称与密码仅存内存，测试结束即清除；不测试互联网。</p>"
        "<form method=post action=/connect><p>网络名称 <input name=ssid maxlength=32 required></p>"
        "<p>密码 <input name=password type=password maxlength=63></p><button>连接</button></form></html>";
#else
    static const char html[] = "<!doctype html><meta name=viewport content='width=device-width'>"
        "<title>PP DEMO Wi-Fi</title><h1>PP DEMO</h1><p>Connect to a 2.4 GHz network. "
        "Credentials stay in RAM and disappear when this test ends. No Internet test is performed.</p>"
        "<form method=post action=/connect><p>SSID <input name=ssid maxlength=32 required></p>"
        "<p>Password <input name=password type=password maxlength=63></p><button>Connect</button></form>";
#endif
    httpd_resp_set_type(r, "text/html; charset=utf-8");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    return httpd_resp_send(r, html, HTTPD_RESP_USE_STRLEN);
}
static esp_err_t connect_network(httpd_req_t *r)
{
    if (!from_test_ap(r)) return httpd_resp_send_err(r, HTTPD_403_FORBIDDEN, LAB_TEXT("Use the PP test hotspot", "请连接 PP 测试热点"));
    char body[257] = {0}, ssid[33] = {0}, password[65] = {0}, origin[80];
    if (r->content_len <= 0 || r->content_len > 256)
        return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, LAB_TEXT("Body exceeds limit", "请求内容过长"));
    size_t origin_len = httpd_req_get_hdr_value_len(r, "Origin");
    if (origin_len && (origin_len >= sizeof(origin) ||
        httpd_req_get_hdr_value_str(r, "Origin", origin, sizeof(origin)) != ESP_OK ||
        strcmp(origin, "http://192.168.4.1")))
        return httpd_resp_send_err(r, HTTPD_403_FORBIDDEN, LAB_TEXT("Wrong origin", "请求来源不符"));
    int used = 0;
    while (used < r->content_len) {
        int got = httpd_req_recv(r, body+used, r->content_len-used);
        if (got <= 0) {
            memset(body, 0, sizeof(body));
            return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, LAB_TEXT("Incomplete form", "表单接收不完整"));
        }
        used += got;
    }
    if (!lab_parse_credentials(body, ssid, password)) {
        memset(body, 0, sizeof(body)); memset(password, 0, sizeof(password));
        return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, LAB_TEXT("Invalid SSID/password", "网络名称或密码无效"));
    }
    wifi_config_t config = {0};
    memcpy(config.sta.ssid, ssid, strlen(ssid));
    memcpy(config.sta.password, password, strlen(password));
    memset(body, 0, sizeof(body)); memset(password, 0, sizeof(password));
    (void)esp_wifi_disconnect();
    xEventGroupClearBits(events, GOT_IP | DISCONNECTED);
    esp_err_t error = esp_wifi_set_config(WIFI_IF_STA, &config);
    memset(&config, 0, sizeof(config));
    if (error == ESP_OK) error = esp_wifi_connect();
    if (error != ESP_OK) return httpd_resp_send_err(r, HTTPD_500_INTERNAL_SERVER_ERROR, LAB_TEXT("Connection rejected", "连接请求被拒绝"));
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    return httpd_resp_sendstr(r, LAB_TEXT("Connection requested. Check PP screen for IP or failure. This is not proof of Internet access.", "已请求连接，请查看 PP 屏幕上的 IP 或错误。这不代表互联网可用。"));
}

esp_err_t lab_wifi_test(void)
{
    esp_netif_t *sta = NULL, *ap = NULL;
    httpd_handle_t server = NULL;
    esp_event_handler_instance_t wifi_handler = NULL, ip_handler = NULL;
    bool initialized = false, started = false;
    esp_err_t error = ESP_FAIL;
    events = xEventGroupCreate();
    if (!events) return ESP_ERR_NO_MEM;
    sta = esp_netif_create_default_wifi_sta();
    ap = esp_netif_create_default_wifi_ap();
    if (!sta || !ap) goto cleanup;
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    error = esp_wifi_init(&init);
    if (error != ESP_OK) goto cleanup;
    initialized = true;
    if ((error = esp_wifi_set_storage(WIFI_STORAGE_RAM)) != ESP_OK) goto cleanup;
    if ((error = esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
        network_event, NULL, &wifi_handler)) != ESP_OK) goto cleanup;
    if ((error = esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
        network_event, NULL, &ip_handler)) != ESP_OK) goto cleanup;
    wifi_config_t config = {0};
    unsigned random = esp_random();
    char name[32], key[16];
    snprintf(name, sizeof(name), "PP-DEMO-%04X", random & 0xffff);
    snprintf(key, sizeof(key), "%08X", (unsigned)esp_random());
    memcpy(config.ap.ssid, name, strlen(name)); config.ap.ssid_len = strlen(name);
    memcpy(config.ap.password, key, strlen(key));
    config.ap.max_connection = 1; config.ap.authmode = WIFI_AUTH_WPA2_PSK;
    if ((error = esp_wifi_set_mode(WIFI_MODE_APSTA)) != ESP_OK) goto cleanup;
    if ((error = esp_wifi_set_config(WIFI_IF_AP, &config)) != ESP_OK) goto cleanup;
    memset(&config, 0, sizeof(config));
    if ((error = esp_wifi_start()) != ESP_OK) goto cleanup;
    started = true;
    wifi_scan_config_t scan = {.show_hidden = false};
    uint16_t found = 0;
    if ((error = esp_wifi_scan_start(&scan, true)) == ESP_OK) {
        error = esp_wifi_scan_get_ap_num(&found);
        (void)esp_wifi_clear_ap_list();
    }
    if (error != ESP_OK || lab_cancelled()) goto cleanup;
    httpd_config_t http = HTTPD_DEFAULT_CONFIG();
    http.max_open_sockets = 2; http.stack_size = 4096; http.recv_wait_timeout = 4;
    http.lru_purge_enable = true;
    if ((error = httpd_start(&server, &http)) != ESP_OK) goto cleanup;
    const httpd_uri_t get = {.uri="/", .method=HTTP_GET, .handler=form};
    const httpd_uri_t post = {.uri="/connect", .method=HTTP_POST, .handler=connect_network};
    if ((error = httpd_register_uri_handler(server, &get)) != ESP_OK) goto cleanup;
    if ((error = httpd_register_uri_handler(server, &post)) != ESP_OK) goto cleanup;
    lab_notice(LAB_TEXT("Found %u networks\n\nJoin %s\nKey %s\nOpen 192.168.4.1\n\nRAM only / 180s limit", "找到 %u 个网络\n热点 %s\n密码 %s\n打开 192.168.4.1\n\n仅存内存 / 限时 180 秒"), found, name, key);
    for (int second = 0; second < 180 && !lab_cancelled(); second++) {
        EventBits_t bits = xEventGroupWaitBits(events, GOT_IP | DISCONNECTED, pdTRUE, pdFALSE, pdMS_TO_TICKS(1000));
        esp_ip4_addr_t ip = {.addr=atomic_load(&assigned_ip)};
        if (bits & GOT_IP) lab_notice(LAB_TEXT("Wi-Fi IP acquired\n", "Wi-Fi 已获取 IP\n") IPSTR LAB_TEXT("\n\nLAN connection only.\nInternet not tested.\n\nHold OK to stop.", "\n\n仅确认局域网连接\n未验证互联网\n长按 OK 停止"), IP2STR(&ip));
        if ((bits & DISCONNECTED) && !(bits & GOT_IP)) lab_notice(LAB_TEXT("Wi-Fi disconnected.\nReason code %u\nCheck password/signal.\n\nReopen the phone form\nor hold OK to stop.", "Wi-Fi 已断开\n原因代码 %u\n请检查密码和信号\n\n手机重新配网\n或长按 OK 停止"), atomic_load(&disconnect_reason));
    }
cleanup:
    // Failed shutdown is not permission to free resources still in use.
    if (server) {
        esp_err_t e = httpd_stop(server);
        if (e != ESP_OK) { lab_require_restart(); return e; }
    }
    if (started) {
        esp_err_t e = esp_wifi_stop();
        if (e != ESP_OK) { lab_require_restart(); return e; }
    }
    if (wifi_handler) {
        esp_err_t e = esp_event_handler_instance_unregister(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, wifi_handler);
        if (e != ESP_OK) { lab_require_restart(); return e; }
    }
    if (ip_handler) {
        esp_err_t e = esp_event_handler_instance_unregister(IP_EVENT, IP_EVENT_STA_GOT_IP, ip_handler);
        if (e != ESP_OK) { lab_require_restart(); return e; }
    }
    if (initialized) {
        esp_err_t e = esp_wifi_deinit();
        if (e != ESP_OK) { lab_require_restart(); return e; }
    }
    if (ap) esp_netif_destroy_default_wifi(ap);
    if (sta) esp_netif_destroy_default_wifi(sta);
    vEventGroupDelete(events); events = NULL;
    return error;
}

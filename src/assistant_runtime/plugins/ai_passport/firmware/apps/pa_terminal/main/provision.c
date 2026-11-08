/* Hotspot-only bootstrap. No supplier/admin secrets, uploads or browser storage. */
#include "provision.h"
#include "provision_access.h"
#include "settings.h"
#include "network.h"
#include "esp_http_server.h"
#include "esp_wifi.h"
#include "esp_random.h"
#include <sys/socket.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static httpd_handle_t server;
static atomic_bool complete, active;
static bool local_request(httpd_req_t *r) {
    struct sockaddr_storage address = {0};
    socklen_t size = sizeof(address);
    char host[32], origin[64];
    if (getsockname(httpd_req_to_sockfd(r), (struct sockaddr *)&address, &size) ||
        size > sizeof(address) ||
        httpd_req_get_hdr_value_str(r, "Host", host, sizeof(host)) != ESP_OK)
        return false;
    size_t n = httpd_req_get_hdr_value_len(r, "Origin");
    if (n && (n >= sizeof(origin) ||
              httpd_req_get_hdr_value_str(r, "Origin", origin, sizeof(origin)) != ESP_OK))
        return false;
    return pp_provision_request_allowed((struct sockaddr *)&address, size, host, n ? origin : NULL);
}
static esp_err_t page(httpd_req_t *r) {
    if (!local_request(r))
        return httpd_resp_send_err(r, HTTPD_403_FORBIDDEN, "请连接 PP 屏幕显示的热点后重试");
    static const char html[] =
        "<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport "
        "content='width=device-width,initial-scale=1'>"
        "<title>PP 初始连接</title><style>body{font:16px "
        "system-ui;background:#f4f6f3;color:#21352d;margin:0;padding:24px}"
        "main{max-width:460px;margin:24px "
        "auto;padding:24px;background:white;border-radius:16px}h1{font-size:24px}"
        "label{display:block;margin:18px 0 "
        "6px}input,textarea,button{box-sizing:border-box;width:100%;padding:12px;"
        "font:inherit;border:1px solid "
        "#cbd5ce;border-radius:8px}button{background:#285e49;color:white;margin-top:20px}"
        "small,p{color:#637168;line-height:1.6}details{margin-top:16px}#status{white-space:pre-"
        "wrap}</style>"
        "<main><h1>连接你的 PP</h1><p>先连接 2.4 GHz Wi-Fi，再到 PA "
        "后台输入设备屏幕上的八位配对数字。</p>"
        "<form id=f><label for=ssid>Wi-Fi 名称</label><input id=ssid required maxlength=32 "
        "autocomplete=off>"
        "<label for=password>Wi-Fi 密码</label><input id=password type=password maxlength=64 "
        "autocomplete=new-password>"
        "<label for=url>PA 业务地址</label><input id=url type=url required "
        "placeholder='http://服务器地址:8047' autocomplete=off>"
        "<small>填写业务根地址。固件网站的 9443 端口不用于对话。</small>"
        "<details><summary>HTTPS 私有 CA（仅需要时填写）</summary><textarea id=ca rows=5 "
        "placeholder='公开 CA 证书，不是私钥'></textarea></details>"
        "<button id=save>保存并连接</button></form><p id=status role=status></p></main>"
        "<script>const "
        "f=document.querySelector('#f'),b=document.querySelector('#save'),s=document.querySelector("
        "'#status');"
        "f.onsubmit=async "
        "e=>{e.preventDefault();b.disabled=true;s.textContent='正在保存…';try{const r=await "
        "fetch('/configure',"
        "{method:'POST',headers:{'Content-Type':'application/"
        "json'},body:JSON.stringify({ssid:document.querySelector('#ssid').value,"
        "password:document.querySelector('#password').value,server_url:document.querySelector('#"
        "url').value.trim(),"
        "trusted_ca:document.querySelector('#ca').value.trim()})});if(!r.ok)throw Error(await "
        "r.text());"
        "s.textContent='已保存。请查看 PP 屏幕，联网后到 PA 后台输入八位配对码。';f.hidden=true;"
        "document.querySelector('#password').value='';}catch(e){s.textContent='未保存：'+e.message;"
        "b.disabled=false;}};</script></html>";
    httpd_resp_set_type(r, "text/html; charset=utf-8");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    httpd_resp_set_hdr(r, "X-Content-Type-Options", "nosniff");
    return httpd_resp_send(r, html, HTTPD_RESP_USE_STRLEN);
}
static esp_err_t configure(httpd_req_t *r) {
    if (!local_request(r))
        return httpd_resp_send_err(r, HTTPD_403_FORBIDDEN, "请连接 PP 屏幕显示的热点后重试");
    if (r->content_len <= 0 || r->content_len > 6144)
        return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, "内容过长");
    char type[64];
    if (httpd_req_get_hdr_value_str(r, "Content-Type", type, sizeof(type)) != ESP_OK ||
        strcmp(type, "application/json"))
        return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST, "无效表单");
    char *body = malloc(r->content_len + 1);
    if (!body)
        return httpd_resp_send_err(r, HTTPD_500_INTERNAL_SERVER_ERROR, "内存不足，请重试");
    int used = 0;
    while (used < r->content_len) {
        int n = httpd_req_recv(r, body + used, r->content_len - used);
        if (n <= 0) {
            memset(body, 0, r->content_len);
            free(body);
            return ESP_FAIL;
        }
        used += n;
    }
    body[used] = 0;
    esp_err_t e = pp_settings_bootstrap(body);
    memset(body, 0, used);
    free(body);
    if (e != ESP_OK)
        return httpd_resp_send_err(r, HTTPD_400_BAD_REQUEST,
                                   "请检查 Wi-Fi、密码、PA 根地址及 CA 证书；配置未保存。");
    httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    esp_err_t sent = httpd_resp_sendstr(r, "已保存，请查看设备屏幕");
    complete = true; /* Owner stops this server only after the handler returns. */
    return sent;
}
esp_err_t pp_provision_start(void) {
    if (server)
        return ESP_OK;
    complete = false;
    wifi_config_t ap = {0};
    char name[24], password[16];
    snprintf(name, sizeof(name), "PP-%06lX", (unsigned long)(esp_random() & 0xffffff));
    snprintf(password, sizeof(password), "%08lu", (unsigned long)(esp_random() % 100000000));
    memcpy(ap.ap.ssid, name, strlen(name));
    ap.ap.ssid_len = strlen(name);
    memcpy(ap.ap.password, password, strlen(password));
    ap.ap.max_connection = 1;
    ap.ap.authmode = WIFI_AUTH_WPA2_PSK;
    esp_err_t e = esp_wifi_set_mode(WIFI_MODE_APSTA);
    if (e == ESP_OK)
        e = esp_wifi_set_config(WIFI_IF_AP, &ap);
    memset(&ap, 0, sizeof(ap));
    if (e != ESP_OK)
        return e;
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.max_open_sockets = 2;
    cfg.stack_size = 4096;
    cfg.recv_wait_timeout = 4;
    cfg.send_wait_timeout = 4;
    cfg.lru_purge_enable = true;
    cfg.server_port = 80;
    e = httpd_start(&server, &cfg);
    if (e == ESP_OK) {
        const httpd_uri_t get = {.uri = "/", .method = HTTP_GET, .handler = page};
        const httpd_uri_t post = {.uri = "/configure", .method = HTTP_POST, .handler = configure};
        e = httpd_register_uri_handler(server, &get);
        if (e == ESP_OK)
            e = httpd_register_uri_handler(server, &post);
    }
    if (e != ESP_OK) {
        pp_provision_stop();
        return e;
    }
    cJSON *display = cJSON_CreateObject();
    cJSON_AddStringToObject(display, "type", "setup");
    cJSON_AddStringToObject(display, "ssid", name);
    cJSON_AddStringToObject(display, "password", password);
    cJSON_AddStringToObject(display, "address", "http://192.168.4.1");
    char *raw = cJSON_PrintUnformatted(display);
    cJSON_Delete(display);
    memset(password, 0, sizeof(password));
    if (!raw) {
        pp_provision_stop();
        return ESP_ERR_NO_MEM;
    }
    pp_received(raw, strlen(raw));
    memset(raw, 0, strlen(raw));
    free(raw);
    active = true;
    return ESP_OK;
}
esp_err_t pp_provision_stop(void) {
    if (server) {
        esp_err_t e = httpd_stop(server);
        if (e != ESP_OK)
            return e;
        server = NULL;
    }
    active = false;
    return esp_wifi_set_mode(WIFI_MODE_STA);
}
bool pp_provision_complete(void) { return complete; }
bool pp_provision_active(void) { return active; }

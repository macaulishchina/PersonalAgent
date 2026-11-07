#include "lab_language.h"
#include "lab_runtime.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "host/ble_gap.h"
#include "host/ble_gatt.h"
#include "host/ble_hs.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

static uint8_t address_type;
static uint16_t tx_handle;
static atomic_uint writes, connections;
static atomic_int failure;
static atomic_bool active;
static char echo[65];
static size_t echo_size;
static SemaphoreHandle_t stopped;
static TaskHandle_t host_task;

static int access_value(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctx, void *arg)
{
    (void)attr; (void)arg;
    if (ctx->op == BLE_GATT_ACCESS_OP_READ_CHR)
        return os_mbuf_append(ctx->om, echo, echo_size) ? BLE_ATT_ERR_INSUFFICIENT_RES : 0;
    if (ctx->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return BLE_ATT_ERR_UNLIKELY;
    uint16_t n = OS_MBUF_PKTLEN(ctx->om);
    if (!n || n > 64) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    if (ble_hs_mbuf_to_flat(ctx->om, echo, 64, &n)) return BLE_ATT_ERR_UNLIKELY;
    echo_size = n; echo[n] = 0; atomic_fetch_add(&writes, 1);
    struct os_mbuf *reply = ble_hs_mbuf_from_flat(echo, n);
    if (reply) (void)ble_gatts_notify_custom(conn, tx_handle, reply);
    return 0;
}
static const struct ble_gatt_svc_def services[] = {
    {.type=BLE_GATT_SVC_TYPE_PRIMARY, .uuid=BLE_UUID16_DECLARE(0xfff0),
     .characteristics=(struct ble_gatt_chr_def[]) {
        {.uuid=BLE_UUID16_DECLARE(0xfff1), .access_cb=access_value, .flags=BLE_GATT_CHR_F_WRITE},
        {.uuid=BLE_UUID16_DECLARE(0xfff2), .access_cb=access_value,
         .flags=BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY, .val_handle=&tx_handle}, {0}
     }}, {0}
};
static int gap(struct ble_gap_event *event, void *arg);
static void advertise(void)
{
    if (!atomic_load(&active)) return;
    const char *name = "PP-DEMO";
    struct ble_hs_adv_fields fields = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (const uint8_t *)name; fields.name_len = strlen(name); fields.name_is_complete = 1;
    int rc = ble_gap_adv_set_fields(&fields);
    struct ble_gap_adv_params params = {.conn_mode=BLE_GAP_CONN_MODE_UND, .disc_mode=BLE_GAP_DISC_MODE_GEN};
    if (!rc) rc = ble_gap_adv_start(address_type, NULL, BLE_HS_FOREVER, &params, gap, NULL);
    if (rc) atomic_store(&failure, rc);
}
static int gap(struct ble_gap_event *event, void *arg)
{
    (void)arg;
    if (event->type == BLE_GAP_EVENT_CONNECT) {
        if (!event->connect.status) atomic_fetch_add(&connections, 1);
        else advertise();
    } else if (event->type == BLE_GAP_EVENT_DISCONNECT || event->type == BLE_GAP_EVENT_ADV_COMPLETE) advertise();
    return 0;
}
static void sync_host(void)
{
    int rc = ble_hs_util_ensure_addr(0);
    if (!rc) rc = ble_hs_id_infer_auto(0, &address_type);
    if (rc) atomic_store(&failure, rc); else advertise();
}
static void reset_host(int reason) { atomic_store(&failure, reason); }
static void run_host(void *arg)
{
    (void)arg; nimble_port_run(); xSemaphoreGive(stopped);
    for (;;) vTaskSuspend(NULL);
}
esp_err_t lab_ble_test(void)
{
    esp_err_t error = nimble_port_init();
    if (error != ESP_OK) return error;
    atomic_store(&writes, 0); atomic_store(&connections, 0); atomic_store(&failure, 0);
    atomic_store(&active, true); echo_size = 0;
    stopped = xSemaphoreCreateBinary();
    if (!stopped) { (void)nimble_port_deinit(); return ESP_ERR_NO_MEM; }
    ble_svc_gap_init(); ble_svc_gatt_init();
    int rc = ble_svc_gap_device_name_set("PP-DEMO");
    if (!rc) rc = ble_gatts_count_cfg(services);
    if (!rc) rc = ble_gatts_add_svcs(services);
    if (rc) { error = ESP_FAIL; goto cleanup; }
    ble_hs_cfg.sync_cb = sync_host; ble_hs_cfg.reset_cb = reset_host;
    if (xTaskCreate(run_host, "pp_ble", 4096, NULL, 5, &host_task) != pdPASS) {
        error = ESP_ERR_NO_MEM; goto cleanup;
    }
    for (unsigned second = 0; second < 120 && !lab_cancelled(); second++) {
        if (atomic_load(&failure)) { error = ESP_FAIL; break; }
        lab_notice(LAB_TEXT("Scan: PP-DEMO\nService FFF0\nWrite FFF1 / Read FFF2\n\nConnections %u\nEcho writes %u\n\nNo secrets on test BLE", "扫描 PP-DEMO\n服务 FFF0\n写入 FFF1 / 读取 FFF2\n连接次数 %u\n回读次数 %u\n\n请勿发送隐私数据"), atomic_load(&connections), atomic_load(&writes));
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
cleanup:
    atomic_store(&active, false);
    if (host_task) {
        if (nimble_port_stop() || xSemaphoreTake(stopped, pdMS_TO_TICKS(3000)) != pdTRUE) {
            lab_notice(LAB_TEXT("BLE stop unconfirmed.\nRestart required.", "蓝牙停止未确认\n请重启设备"));
            // Do not free resources the unconfirmed host may still access.
            // The UI/report remains available, but no replacement test can start.
            lab_require_restart();
            return ESP_ERR_TIMEOUT;
        }
        vTaskDelete(host_task); host_task = NULL;
    }
    esp_err_t e = nimble_port_deinit();
    if (error == ESP_OK) error = e;
    vSemaphoreDelete(stopped); stopped = NULL;
    return error;
}

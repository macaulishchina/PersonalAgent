#include "settings.h"
#include "settings_store_fixture.h"
#include <assert.h>
#include <string.h>

/* One open NVS handle at a time. Only committed writes become readable. */
static struct {
    char settings[8193], pending[72], staged[8193];
    char *destination;
    unsigned commits;
    bool erase, open, fail_settings_commit;
} nvs;
static int locked;
static char *slot(const char *key) {
    if (!strcmp(key, "settings"))
        return nvs.settings;
    assert(!strcmp(key, "pending"));
    return nvs.pending;
}
esp_err_t nvs_open(const char *space, int mode, nvs_handle_t *handle) {
    assert(!strcmp(space, "pa_terminal_v2") && !nvs.open);
    assert(mode == NVS_READONLY || mode == NVS_READWRITE);
    nvs.open = true;
    *handle = mode;
    return ESP_OK;
}
esp_err_t nvs_set_str(nvs_handle_t handle, const char *key, const char *value) {
    assert(nvs.open && handle == NVS_READWRITE && !nvs.destination);
    nvs.destination = slot(key);
    size_t capacity = nvs.destination == nvs.settings ? sizeof(nvs.settings) : sizeof(nvs.pending);
    assert(strlen(value) < capacity);
    strcpy(nvs.staged, value);
    return ESP_OK;
}
esp_err_t nvs_get_str(nvs_handle_t handle, const char *key, char *value, size_t *size) {
    (void)handle;
    assert(nvs.open);
    const char *saved = slot(key);
    if (!*saved)
        return ESP_ERR_NVS_NOT_FOUND;
    size_t required = strlen(saved) + 1;
    if (value && *size < required)
        return ESP_ERR_INVALID_SIZE;
    if (value)
        memcpy(value, saved, required);
    *size = required;
    return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t handle) {
    assert(nvs.open && handle == NVS_READWRITE);
    if (nvs.fail_settings_commit && nvs.destination == nvs.settings)
        return ESP_FAIL;
    if (nvs.erase)
        nvs.settings[0] = nvs.pending[0] = 0;
    if (nvs.destination)
        strcpy(nvs.destination, nvs.staged);
    ++nvs.commits;
    return ESP_OK;
}
esp_err_t nvs_erase_all(nvs_handle_t handle) {
    assert(nvs.open && handle == NVS_READWRITE);
    nvs.erase = true;
    return ESP_OK;
}
void nvs_close(nvs_handle_t handle) {
    (void)handle;
    assert(nvs.open);
    nvs.open = nvs.erase = false;
    nvs.destination = NULL;
}
SemaphoreHandle_t xSemaphoreCreateMutex(void) { return &locked; }
int xSemaphoreTake(SemaphoreHandle_t mutex, int ticks) {
    if (*mutex) {
        assert(ticks == 0); /* Rendering during a write must never wait. */
        return 0;
    }
    *mutex = 1;
    return pdTRUE;
}
void xSemaphoreGive(SemaphoreHandle_t mutex) {
    assert(*mutex);
    *mutex = 0;
}
void esp_fill_random(void *buffer, size_t size) {
    static unsigned sequence = 0;
    memset(buffer, ++sequence, size);
}
void bootloader_random_enable(void) {}
void bootloader_random_disable(void) {}

static void check_publication(pp_terminal_settings_t *ui) {
    unsigned revision = ui->revision;
    assert(pp_settings_bootstrap("{\"ssid\":\"Home\"}", "http://pa.local:8047", "") == ESP_OK);
    assert(!ui->wifi_count);
    locked = 1;
    assert(!pp_settings_refresh(ui) && ui->revision == revision);
    locked = 0;
    assert(pp_settings_refresh(ui) && ui->wifi_count == 1 && ui->revision != revision);
    unsigned commits = nvs.commits;
    assert(pp_preference(PP_PREF_VOLUME, ui->volume, ui) == ESP_OK && nvs.commits == commits);
    revision = ui->revision;
    nvs.fail_settings_commit = true;
    assert(pp_preference(PP_PREF_VOLUME, 77, ui) == ESP_FAIL);
    assert(pp_settings_refresh(ui) && ui->revision == revision && ui->volume == 45);
    pp_settings_t full;
    assert(pp_settings_snapshot(&full) == ESP_OK && full.volume == 45);
    assert(pp_settings_decode(nvs.settings, &full) && full.volume == 45);
    nvs.fail_settings_commit = false;
    assert(pp_preference(PP_PREF_VOLUME, 77, ui) == ESP_OK && ui->volume == 77);
    assert(pp_preference(PP_PREF_PAIRED, 1, NULL) == ESP_OK && !ui->paired);
    assert(pp_settings_refresh(ui) && ui->paired);
}
static void check_cloud_receipt(pp_terminal_settings_t *ui) {
    cJSON *fields = cJSON_Parse("{\"text_size\":{\"version\":1,\"value\":18}}");
    cJSON *receipt = NULL;
    bool reconnect;
    nvs.fail_settings_commit = true;
    unsigned revision = ui->revision;
    assert(pp_settings_cloud(fields, &receipt, &reconnect, ui) == ESP_FAIL);
    assert(receipt == NULL && ui->revision == revision && ui->text_size == 16);
    nvs.fail_settings_commit = false;
    assert(pp_settings_cloud(fields, &receipt, &reconnect, ui) == ESP_OK);
    assert(!reconnect && ui->text_size == 18);
    assert(cJSON_IsObject(cJSON_GetObjectItem(receipt, "actual")));
    cJSON *versions = cJSON_GetObjectItem(receipt, "versions");
    assert(cJSON_GetNumberValue(cJSON_GetObjectItem(versions, "text_size")) == 1);
    cJSON_Delete(receipt);
    cJSON_Delete(fields);
}
static void check_identity_transition(pp_terminal_settings_t *ui) {
    assert(pp_settings_binding("old-scope", ui) == ESP_OK && !strcmp(ui->binding, "old-scope"));
    strcpy(nvs.pending, "legacy-receipt");
    char id[37], old_device[sizeof(ui->device)];
    strcpy(old_device, ui->device);
    nvs.fail_settings_commit = true;
    /* Simulate the rotation failing after scoping a legacy pending receipt. */
    assert(pp_settings_new_identity() == ESP_FAIL && pp_settings_refresh(ui));
    assert(!strcmp(ui->device, old_device) && ui->paired);
    assert(pp_pending_read(id, sizeof(id)) == ESP_OK && !strcmp(id, "legacy-receipt"));
    nvs.fail_settings_commit = false;
    assert(pp_settings_new_identity() == ESP_OK && pp_settings_refresh(ui));
    assert(strcmp(ui->device, old_device) && !ui->paired && !ui->binding[0]);
    assert(ui->wifi_count == 1 && ui->volume == 77 && ui->text_size == 18);
    assert(pp_pending_read(id, sizeof(id)) == ESP_OK && !id[0]);
}
int main(void) {
    pp_terminal_settings_t ui = {0};
    assert(pp_settings_load(&ui) == ESP_OK && ui.revision && !ui.paired);
    check_publication(&ui);
    check_cloud_receipt(&ui);
    check_identity_transition(&ui);
    assert(!locked && !nvs.open);
    return 0;
}

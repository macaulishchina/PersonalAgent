#pragma once
#include <stddef.h>

/* The settings target links the real module against this small SDK surface. */
typedef int esp_err_t;
enum {
    ESP_OK, ESP_FAIL, ESP_ERR_INVALID_ARG, ESP_ERR_NO_MEM, ESP_ERR_TIMEOUT,
    ESP_ERR_INVALID_STATE, ESP_ERR_INVALID_SIZE, ESP_ERR_NVS_NOT_FOUND
};
typedef int nvs_handle_t;
enum { NVS_READWRITE, NVS_READONLY };
esp_err_t nvs_open(const char *space, int mode, nvs_handle_t *handle);
esp_err_t nvs_set_str(nvs_handle_t handle, const char *key, const char *value);
esp_err_t nvs_get_str(nvs_handle_t handle, const char *key, char *value, size_t *size);
esp_err_t nvs_commit(nvs_handle_t handle);
esp_err_t nvs_erase_all(nvs_handle_t handle);
void nvs_close(nvs_handle_t handle);
typedef int *SemaphoreHandle_t;
#define pdMS_TO_TICKS(ms) (ms)
#define pdTRUE 1
SemaphoreHandle_t xSemaphoreCreateMutex(void);
int xSemaphoreTake(SemaphoreHandle_t mutex, int ticks);
void xSemaphoreGive(SemaphoreHandle_t mutex);
void esp_fill_random(void *buffer, size_t size);
void bootloader_random_enable(void);
void bootloader_random_disable(void);

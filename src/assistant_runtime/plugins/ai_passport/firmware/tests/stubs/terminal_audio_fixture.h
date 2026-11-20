#pragma once
/* Host declarations for the FreeRTOS and codec operations used by terminal_audio.c. */
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>

typedef void *QueueHandle_t;
typedef void *TaskHandle_t;
typedef unsigned portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0u
#define portENTER_CRITICAL(lock) ((void)(lock))
#define portEXIT_CRITICAL(lock) ((void)(lock))
#define pdTRUE 1
#define pdPASS 1
#define portMAX_DELAY UINT32_MAX
#define pdMS_TO_TICKS(ms) (ms)
#define ESP_OK 0

QueueHandle_t xQueueCreate(unsigned length, size_t size);
int xQueueSend(QueueHandle_t queue, const void *data, unsigned wait);
int xQueueReceive(QueueHandle_t queue, void *data, unsigned wait);
unsigned uxQueueMessagesWaiting(QueueHandle_t queue);
void xQueueReset(QueueHandle_t queue);
void vQueueDelete(QueueHandle_t queue);
int xTaskCreate(void (*entry)(void *), const char *name, unsigned stack, void *arg,
                unsigned priority, TaskHandle_t *worker);
void xTaskNotifyGive(TaskHandle_t worker);
void ulTaskNotifyTake(int clear, unsigned wait);
void vTaskDelay(unsigned wait);
int bsp_audio_init(void);
int bsp_audio_set_volume(unsigned volume);
int bsp_audio_set_format(unsigned rate, unsigned bits, unsigned channels);
int bsp_audio_read(void *pcm, size_t size);
int bsp_audio_write(const void *pcm, size_t size, size_t *written);
uint32_t bsp_audio_tx_position(void);
bool bsp_audio_tx_drained(uint32_t position);

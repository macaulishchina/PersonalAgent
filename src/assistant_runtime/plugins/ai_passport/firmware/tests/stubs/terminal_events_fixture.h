#pragma once
#include <stddef.h>

typedef enum { BSP_BTN_OK, BSP_BTN_UP } bsp_btn_t;
typedef enum { BSP_BTN_PRESS, BSP_BTN_RELEASE } bsp_btn_ev_t;
typedef struct cJSON cJSON;
typedef struct host_queue *QueueHandle_t;
#define pdTRUE 1
#define portMAX_DELAY (~0u)
QueueHandle_t xQueueCreate(size_t slots, size_t item_size);
int xQueueSend(QueueHandle_t q, const void *item, unsigned wait);
int xQueueReceive(QueueHandle_t q, void *item, unsigned wait);
void xQueueReset(QueueHandle_t q);
void vQueueDelete(QueueHandle_t q);

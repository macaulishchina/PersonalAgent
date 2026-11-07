#pragma once
#include <stdbool.h>
#include <stdint.h>

typedef enum {
    LAB_DISPLAY, LAB_KEYS, LAB_AUDIO, LAB_BATTERY, LAB_WIFI, LAB_BLE,
    LAB_STORAGE, LAB_SLEEP, LAB_USB, LAB_NFC, LAB_COUNT
} lab_case_t;
typedef enum { LAB_UNTESTED, LAB_PASS, LAB_FAIL } lab_result_t;
typedef struct {
    uint32_t magic;
    uint32_t revision;
    uint8_t results[LAB_COUNT];
} lab_record_t;

const char *lab_name(lab_case_t item);
const char *lab_result_name(unsigned result);
bool lab_record_valid(const lab_record_t *record);
void lab_record_init(lab_record_t *record);
bool lab_record_set(lab_record_t *record, unsigned item, unsigned result);
unsigned lab_record_passed(const lab_record_t *record);
int lab_wrap(int value, int delta, int count);
bool lab_parse_credentials(const char *body, char ssid[33], char password[65]);

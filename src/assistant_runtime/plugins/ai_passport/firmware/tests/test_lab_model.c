#include "lab_model.h"
#include <assert.h>
#include <string.h>

int main(void)
{
    assert(!strcmp(lab_name((lab_case_t)-1), "UNKNOWN"));
    lab_record_t r;
    memset(&r, 0, sizeof(r)); assert(!lab_record_valid(&r));
    lab_record_init(&r); assert(lab_record_valid(&r)); assert(lab_record_passed(&r) == 0);
    assert(lab_record_set(&r, LAB_AUDIO, LAB_PASS)); assert(lab_record_passed(&r) == 1);
    assert(!lab_record_set(&r, LAB_COUNT, LAB_PASS)); assert(!lab_record_set(&r, 0, 99));
    r.results[0] = 255; assert(!lab_record_valid(&r));
    assert(lab_wrap(0, -1, LAB_COUNT) == LAB_COUNT-1);
    assert(lab_wrap(LAB_COUNT-1, 1, LAB_COUNT) == 0);
    char ssid[33], password[65];
    assert(lab_parse_credentials("ssid=Home+WiFi&password=12345678", ssid, password));
    assert(!strcmp(ssid, "Home WiFi")); assert(!strcmp(password,"12345678"));
    assert(lab_parse_credentials("password=&ssid=%E4%B8%AD%E6%96%87", ssid, password));
    assert(!strcmp(ssid, "中文"));
    assert(!lab_parse_credentials("ssid=a&password=short", ssid, password));
    assert(!lab_parse_credentials("ssid=a%00b&password=12345678", ssid, password));
    assert(!lab_parse_credentials("ssid=%zz&password=12345678", ssid, password));
    assert(!lab_parse_credentials("ssid=a&ssid=b&password=12345678", ssid, password));
    assert(!lab_parse_credentials("ssid=a&password=12345678&other=x", ssid, password));
    assert(!lab_parse_credentials("ssid=&password=12345678", ssid, password));
    assert(!lab_parse_credentials("ssid=abcdefghijklmnopqrstuvwxyz1234567&password=12345678", ssid, password));
    return 0;
}

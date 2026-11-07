#include "lab_model.h"
#include <string.h>

#define LAB_MAGIC 0x50504431u
static const char *names[LAB_COUNT] = {
    "DISPLAY", "BUTTONS", "SOUND / MIC", "BATTERY", "WI-FI", "BLUETOOTH",
    "FLASH / NVS", "SLEEP / WAKE", "USB CONSOLE", "NFC TAG"
};
const char *lab_name(lab_case_t item) { return (unsigned)item < LAB_COUNT ? names[item] : "UNKNOWN"; }
const char *lab_result_name(unsigned result)
{
    return result == LAB_PASS ? "PASS" : result == LAB_FAIL ? "FAIL" : "NOT TESTED";
}
bool lab_record_valid(const lab_record_t *r)
{
    if (!r || r->magic != LAB_MAGIC || r->revision != 1) return false;
    for (unsigned i = 0; i < LAB_COUNT; i++) if (r->results[i] > LAB_FAIL) return false;
    return true;
}
void lab_record_init(lab_record_t *r)
{
    memset(r, 0, sizeof(*r)); r->magic = LAB_MAGIC; r->revision = 1;
}
bool lab_record_set(lab_record_t *r, unsigned item, unsigned result)
{
    if (!lab_record_valid(r) || item >= LAB_COUNT || result > LAB_FAIL) return false;
    r->results[item] = result; return true;
}
unsigned lab_record_passed(const lab_record_t *r)
{
    unsigned n = 0;
    if (lab_record_valid(r)) for (unsigned i = 0; i < LAB_COUNT; i++) n += r->results[i] == LAB_PASS;
    return n;
}
int lab_wrap(int value, int delta, int count)
{
    if (count <= 0) return 0;
    int result = (value + delta) % count;
    return result < 0 ? result + count : result;
}
static int hex(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static bool decode(const char *p, size_t n, char *out, size_t capacity)
{
    size_t used = 0;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = p[i];
        if (c == '%') {
            if (i + 2 >= n || hex(p[i+1]) < 0 || hex(p[i+2]) < 0) return false;
            c = (unsigned char)((hex(p[i+1]) << 4) | hex(p[i+2])); i += 2;
        } else if (c == '+') c = ' ';
        if (!c || c < 32 || c == 127 || used + 1 >= capacity) return false;
        out[used++] = (char)c;
    }
    out[used] = 0; return true;
}
bool lab_parse_credentials(const char *body, char ssid[33], char password[65])
{
    bool found_ssid = false, found_password = false;
    ssid[0] = password[0] = 0;
    for (const char *p = body; p && *p;) {
        const char *end = strchr(p, '&'); size_t n = end ? (size_t)(end-p) : strlen(p);
        if (n >= 5 && !memcmp(p, "ssid=", 5) && !found_ssid) {
            if (!decode(p+5, n-5, ssid, 33)) return false;
            found_ssid = true;
        } else if (n >= 9 && !memcmp(p, "password=", 9) && !found_password) {
            if (!decode(p+9, n-9, password, 65)) return false;
            found_password = true;
        } else return false;
        p = end ? end+1 : NULL;
    }
    return found_ssid && found_password && ssid[0] &&
        (!password[0] || strlen(password) >= 8);
}

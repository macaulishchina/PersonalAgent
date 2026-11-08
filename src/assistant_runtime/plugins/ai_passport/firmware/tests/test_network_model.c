#include "network_model.h"
#include <assert.h>
#include <string.h>
int main(void) {
    pp_wifi_plan_t p = {.count = 5};
    unsigned index, round;
    for (unsigned n = 0; n < 15; n++) {
        assert(pp_wifi_next(&p, &index, &round));
        assert(index == n % 5);
        assert(round == n / 5 + 1);
    }
    assert(!pp_wifi_next(&p, &index, &round));
    p = (pp_wifi_plan_t){.count = 0};
    assert(!pp_wifi_next(&p, &index, &round));
    p = (pp_wifi_plan_t){.count = 6};
    assert(!pp_wifi_next(&p, &index, &round));
    p = (pp_wifi_plan_t){.count = 1};
    for (unsigned n = 1; n <= 3; n++) {
        assert(pp_wifi_next(&p, &index, &round));
        assert(index == 0 && round == n);
    }
    assert(!pp_wifi_next(&p, &index, &round));
    assert(pp_wifi_valid("家里", "password"));
    assert(pp_wifi_valid("open", ""));
    assert(!pp_wifi_valid("", "password"));
    assert(!pp_wifi_valid("home", "short"));
    assert(!pp_wifi_valid("中文中文中文中文中文中文中文中文中文中文中文", "password"));
    assert(pp_server_url_valid("http://47.109.34.254:8047"));
    assert(pp_server_url_valid("https://pa.example.test/"));
    assert(pp_server_url_valid("http://[::1]:8047/"));
    const char *bad[] = {"",
                         "ws://host",
                         "http://",
                         "http://host:0",
                         "http://host:65536",
                         "http://host:x",
                         "http://host/path",
                         "http://user:password@host",
                         "http://host?secret",
                         "http://host#fragment",
                         "http://host\\x",
                         "http://host\r\nX:bad",
                         "http://host:8047/another",
                         "http://[::1",
                         "http://[g::1]",
                         "http://host/ "};
    for (unsigned n = 0; n < sizeof(bad) / sizeof(bad[0]); n++)
        assert(!pp_server_url_valid(bad[n]));
    char url[600];
    assert(pp_server_endpoint("http://47.109.34.254:8047/", "stream", true, url, sizeof(url)));
    assert(!strcmp(url, "ws://47.109.34.254:8047/entrances/ai_passport/stream"));
    assert(pp_server_endpoint("https://example.test", "pair", false, url, sizeof(url)));
    assert(!strcmp(url, "https://example.test/entrances/ai_passport/pair"));
    assert(!pp_server_endpoint("http://example.test", "elsewhere", false, url, sizeof(url)));
    assert(!pp_server_endpoint("http://example.test", "pair", false, url, 8));
    return 0;
}

#include "provision_access.h"
#include <arpa/inet.h>
#include <assert.h>
#include <netinet/in.h>
#include <stddef.h>

int main(void) {
    struct sockaddr_in v4 = {.sin_family = AF_INET};
    struct sockaddr_in6 mapped = {.sin6_family = AF_INET6};
    assert(inet_pton(AF_INET, "192.168.4.1", &v4.sin_addr) == 1);
    assert(inet_pton(AF_INET6, "::ffff:192.168.4.1", &mapped.sin6_addr) == 1);
    const struct sockaddr *addresses[] = {(struct sockaddr *)&v4, (struct sockaddr *)&mapped};
    socklen_t sizes[] = {sizeof(v4), sizeof(mapped)};
    for (unsigned i = 0; i < 2; i++) {
        assert(pp_provision_request_allowed(addresses[i], sizes[i], "192.168.4.1", NULL));
        assert(pp_provision_request_allowed(addresses[i], sizes[i], "192.168.4.1:80",
                                            "http://192.168.4.1"));
        assert(pp_provision_request_allowed(addresses[i], sizes[i], "192.168.4.1",
                                            "http://192.168.4.1:80"));
        assert(!pp_provision_request_allowed(addresses[i], sizes[i] - 1, "192.168.4.1", NULL));
        assert(!pp_provision_request_allowed(addresses[i], sizes[i], "evil.test", NULL));
        assert(
            !pp_provision_request_allowed(addresses[i], sizes[i], "192.168.4.1.evil.test", NULL));
        assert(!pp_provision_request_allowed(addresses[i], sizes[i], "192.168.4.1:8047", NULL));
        assert(!pp_provision_request_allowed(addresses[i], sizes[i], "192.168.4.1", "null"));
        assert(!pp_provision_request_allowed(addresses[i], sizes[i], "192.168.4.1",
                                             "http://evil.test"));
        assert(!pp_provision_request_allowed(addresses[i], sizes[i], NULL, NULL));
    }
    /* LAN, wildcard, native IPv6 and IPv4-compatible IPv6 are not the AP. */
    const char *rejected[] = {"::ffff:110.0.0.82", "::", "::1", "::192.168.4.1", "fe80::1"};
    for (unsigned i = 0; i < sizeof(rejected) / sizeof(*rejected); i++) {
        assert(inet_pton(AF_INET6, rejected[i], &mapped.sin6_addr) == 1);
        assert(!pp_provision_request_allowed((struct sockaddr *)&mapped, sizeof(mapped),
                                             "192.168.4.1", NULL));
    }
    assert(inet_pton(AF_INET, "110.0.0.82", &v4.sin_addr) == 1);
    assert(!pp_provision_request_allowed((struct sockaddr *)&v4, sizeof(v4), "192.168.4.1", NULL));
    assert(!pp_provision_request_allowed(NULL, 0, "192.168.4.1", NULL));
    return 0;
}

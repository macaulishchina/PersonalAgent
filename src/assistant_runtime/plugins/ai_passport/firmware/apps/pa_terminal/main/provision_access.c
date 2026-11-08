#include "provision_access.h"
#include <netinet/in.h>
#include <stddef.h>
#include <string.h>

bool pp_provision_request_allowed(const struct sockaddr *local, socklen_t size, const char *host,
                                  const char *origin) {
    static const unsigned char ap[4] = {192, 168, 4, 1};
    static const unsigned char mapped[12] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255};
    if (!local || size < offsetof(struct sockaddr, sa_family) + sizeof(local->sa_family) || !host ||
        (strcmp(host, "192.168.4.1") && strcmp(host, "192.168.4.1:80")) ||
        (origin && origin[0] && strcmp(origin, "http://192.168.4.1") &&
         strcmp(origin, "http://192.168.4.1:80")))
        return false;
    if (local->sa_family == AF_INET && size >= sizeof(struct sockaddr_in)) {
        const struct sockaddr_in *address = (const struct sockaddr_in *)local;
        return !memcmp(&address->sin_addr, ap, sizeof(ap));
    }
    if (local->sa_family == AF_INET6 && size >= sizeof(struct sockaddr_in6)) {
        const struct sockaddr_in6 *address = (const struct sockaddr_in6 *)local;
        return !memcmp(address->sin6_addr.s6_addr, mapped, sizeof(mapped)) &&
               !memcmp(address->sin6_addr.s6_addr + sizeof(mapped), ap, sizeof(ap));
    }
    return false;
}

#pragma once
#include <stdbool.h>
#include <sys/socket.h>

/* ESP-IDF's dual-stack HTTP listener represents IPv4 peers/local addresses as
 * IPv4-mapped IPv6. Both representations must preserve the same AP-only rule. */
bool pp_provision_request_allowed(const struct sockaddr *local, socklen_t size, const char *host,
                                  const char *origin);

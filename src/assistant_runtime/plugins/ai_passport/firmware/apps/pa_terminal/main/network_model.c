#include "network_model.h"
#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

bool pp_wifi_next(pp_wifi_plan_t *p, unsigned *index, unsigned *round) {
    if (!p || !index || !round || !p->count || p->count > PP_WIFI_MAX ||
        p->attempt >= p->count * PP_WIFI_ROUNDS)
        return false;
    *index = p->attempt % p->count;
    *round = p->attempt / p->count + 1;
    p->attempt++;
    return true;
}
bool pp_wifi_valid(const char *ssid, const char *password) {
    if (!ssid || !password || !ssid[0] || strlen(ssid) > 32)
        return false;
    size_t bytes = strlen(password);
    if (!bytes || (bytes >= 8 && bytes <= 63))
        return true;
    if (bytes != 64)
        return false;
    for (size_t i = 0; i < bytes; i++)
        if (!isxdigit((unsigned char)password[i]))
            return false;
    return true;
}
bool pp_server_url_valid(const char *url) {
    if (!url || strlen(url) > 512)
        return false;
    const char *host = !strncmp(url, "http://", 7)    ? url + 7
                       : !strncmp(url, "https://", 8) ? url + 8
                                                      : NULL;
    if (!host || !*host)
        return false;
    for (const unsigned char *p = (const unsigned char *)host; *p; p++)
        if (*p <= 32 || *p >= 127 || strchr("@?#\\", *p))
            return false;
    const char *end = host;
    if (*host == '[') {
        const char *close = strchr(host + 1, ']');
        if (!close || close == host + 1)
            return false;
        for (const char *p = host + 1; p < close; p++)
            if (!isxdigit((unsigned char)*p) && *p != ':' && *p != '.')
                return false;
        end = close + 1;
    } else {
        while (*end && *end != ':' && *end != '/') {
            if (!isalnum((unsigned char)*end) && *end != '.' && *end != '-')
                return false;
            end++;
        }
        if (end == host)
            return false;
    }
    if (*end == ':') {
        const char *start = ++end;
        while (isdigit((unsigned char)*end))
            end++;
        if (end == start || end - start > 5)
            return false;
        char *tail;
        errno = 0;
        unsigned long port = strtoul(start, &tail, 10);
        if (errno || !port || port > 65535 || tail != end)
            return false;
    }
    return !*end || (*end == '/' && !end[1]);
}
bool pp_server_endpoint(const char *root, const char *route, bool websocket, char *out,
                        size_t cap) {
    if (!pp_server_url_valid(root) || !route || (strcmp(route, "pair") && strcmp(route, "stream")))
        return false;
    const bool secure = !strncmp(root, "https://", 8);
    const char *authority = root + (secure ? 8 : 7);
    size_t length = strlen(authority);
    if (length && authority[length - 1] == '/')
        length--;
    int n = snprintf(out, cap, "%s://%.*s/entrances/ai_passport/%s",
                     websocket ? (secure ? "wss" : "ws") : (secure ? "https" : "http"), (int)length,
                     authority, route);
    return n > 0 && (size_t)n < cap;
}

#include "settings_model.h"
#include <math.h>
#include <stdio.h>
#include <string.h>

static const char *fields[PP_CONFIG_FIELDS] = {
    "volume",      "input_mode",    "keep_online", "sleep_seconds", "speech_mode", "offline_cache",
    "show_images", "wifi_networks", "server_url",  "trusted_ca",    "brightness",  "text_size"};
static bool string_field(const cJSON *obj, const char *name, char *out, size_t cap, bool optional) {
    cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, name);
    if (!item && optional) {
        out[0] = 0;
        return true;
    }
    if (!cJSON_IsString(item) || strlen(item->valuestring) >= cap)
        return false;
    memcpy(out, item->valuestring, strlen(item->valuestring) + 1);
    return true;
}
static bool bounded_string(const char *s, size_t cap) { return memchr(s, 0, cap) != NULL; }
static bool token(const char *s) {
    for (; *s; s++)
        if (!((*s >= 'a' && *s <= 'z') || (*s >= 'A' && *s <= 'Z') || (*s >= '0' && *s <= '9') ||
              *s == '_' || *s == '-'))
            return false;
    return true;
}
bool pp_settings_valid(const pp_settings_t *s) {
    if (s->magic != PP_SETTINGS_MAGIC || s->wifi_count > PP_WIFI_MAX || s->volume > 100 ||
        s->sleep_seconds < 30 || s->sleep_seconds > 3600 || s->speech_mode > 2 ||
        s->brightness < 40 || s->brightness > 100 || (s->text_size != 16 && s->text_size != 18) ||
        !bounded_string(s->url, sizeof(s->url)) || !bounded_string(s->ca, sizeof(s->ca)) ||
        !bounded_string(s->binding, sizeof(s->binding)) ||
        !bounded_string(s->device, sizeof(s->device)) || strlen(s->device) != 32 ||
        !bounded_string(s->credential, sizeof(s->credential)) || strlen(s->credential) != 64 ||
        !token(s->device) || !token(s->credential) ||
        (s->wifi_count && !pp_server_url_valid(s->url)))
        return false;
    for (unsigned i = 0; i < s->wifi_count; i++) {
        if (!bounded_string(s->wifi[i].ssid, sizeof(s->wifi[i].ssid)) ||
            !bounded_string(s->wifi[i].password, sizeof(s->wifi[i].password)) ||
            !pp_wifi_valid(s->wifi[i].ssid, s->wifi[i].password))
            return false;
        for (unsigned k = 0; k < i; k++)
            if (!strcmp(s->wifi[k].ssid, s->wifi[i].ssid))
                return false;
    }
    return !s->ca[0] ||
           (strstr(s->ca, "-----BEGIN CERTIFICATE-----") && !strstr(s->ca, "PRIVATE KEY"));
}
static bool integer(const cJSON *v, unsigned low, unsigned high) {
    return cJSON_IsNumber(v) && isfinite(v->valuedouble) && v->valuedouble >= low &&
           v->valuedouble <= high && floor(v->valuedouble) == v->valuedouble;
}
static bool apply(pp_settings_t *s, unsigned i, const cJSON *v) {
    if (cJSON_IsNull(v))
        return true; /* Release cloud management, retain usable local state. */
    switch (i) {
    case 0:
        if (!integer(v, 0, 100))
            return false;
        s->volume = v->valueint;
        break;
    case 1:
        if (!cJSON_IsString(v) ||
            (strcmp(v->valuestring, "steer") && strcmp(v->valuestring, "queue")))
            return false;
        s->queue = !strcmp(v->valuestring, "queue");
        break;
    case 2:
        if (!cJSON_IsBool(v))
            return false;
        s->keep_online = cJSON_IsTrue(v);
        break;
    case 3:
        if (!integer(v, 30, 3600))
            return false;
        s->sleep_seconds = v->valueint;
        break;
    case 4:
        if (!cJSON_IsString(v))
            return false;
        if (!strcmp(v->valuestring, "brief"))
            s->speech_mode = 0;
        else if (!strcmp(v->valuestring, "full"))
            s->speech_mode = 1;
        else if (!strcmp(v->valuestring, "off"))
            s->speech_mode = 2;
        else
            return false;
        break;
    case 5:
        if (!cJSON_IsBool(v))
            return false;
        s->offline_cache = cJSON_IsTrue(v);
        break;
    case 6:
        if (!cJSON_IsBool(v))
            return false;
        s->show_images = cJSON_IsTrue(v);
        break;
    case 7: {
        if (!cJSON_IsArray(v) || cJSON_GetArraySize(v) < 1 || cJSON_GetArraySize(v) > PP_WIFI_MAX)
            return false;
        s->wifi_count = 0;
        memset(s->wifi, 0, sizeof(s->wifi));
        const cJSON *n;
        cJSON_ArrayForEach(n, v) {
            pp_wifi_t *w = &s->wifi[s->wifi_count++];
            if (!string_field(n, "ssid", w->ssid, sizeof(w->ssid), false) ||
                !string_field(n, "password", w->password, sizeof(w->password), false))
                return false;
        }
        break;
    }
    case 8:
        if (!cJSON_IsString(v) || strlen(v->valuestring) >= sizeof(s->url) ||
            !pp_server_url_valid(v->valuestring))
            return false;
        if (strcmp(s->url, v->valuestring))
            s->paired = false;
        strcpy(s->url, v->valuestring);
        break;
    case 9:
        if (!cJSON_IsString(v) || strlen(v->valuestring) >= sizeof(s->ca))
            return false;
        strcpy(s->ca, v->valuestring);
        break;
    case 10:
        if (!integer(v, 40, 100))
            return false;
        s->brightness = v->valueint;
        break;
    case 11:
        if (!integer(v, 16, 18) || v->valueint == 17)
            return false;
        s->text_size = v->valueint;
        break;
    default:
        return false;
    }
    return true;
}

bool pp_settings_prepare_bootstrap(const pp_settings_t *old, const char *raw, pp_settings_t *out) {
    if (!old || !out || old == out || !raw || strlen(raw) > 6144 || strstr(raw, "\\u0000"))
        return false;
    cJSON *j = cJSON_Parse(raw);
    if (!j || !cJSON_IsObject(j)) {
        cJSON_Delete(j);
        return false;
    }
    memcpy(out, old, sizeof(*out));
    memset(out->wifi, 0, sizeof(out->wifi));
    bool ok =
        string_field(j, "ssid", out->wifi[0].ssid, sizeof(out->wifi[0].ssid), false) &&
        string_field(j, "password", out->wifi[0].password, sizeof(out->wifi[0].password), true) &&
        string_field(j, "server_url", out->url, sizeof(out->url), false) &&
        string_field(j, "trusted_ca", out->ca, sizeof(out->ca), true);
    const cJSON *field;
    cJSON_ArrayForEach(field, j) {
        if (!field->string ||
            (strcmp(field->string, "ssid") && strcmp(field->string, "password") &&
             strcmp(field->string, "server_url") && strcmp(field->string, "trusted_ca")))
            ok = false;
    }
    out->wifi_count = 1;
    out->paired = false;
    cJSON_Delete(j);
    return ok && pp_settings_valid(out);
}
bool pp_settings_prepare_cloud(const pp_settings_t *old, const cJSON *changes, pp_settings_t *out,
                               cJSON *versions, bool *changed, bool *reconnect) {
    if (!old || !out || out == old || !cJSON_IsObject(changes) ||
        cJSON_GetArraySize(changes) > PP_CONFIG_FIELDS)
        return false;
    memcpy(out, old, sizeof(*out));
    *changed = *reconnect = false;
    unsigned seen = 0;
    const cJSON *item;
    cJSON_ArrayForEach(item, changes) {
        unsigned i = 0;
        while (i < PP_CONFIG_FIELDS && (!item->string || strcmp(item->string, fields[i])))
            i++;
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(item, "version"),
                    *value = cJSON_GetObjectItemCaseSensitive(item, "value");
        if (i == PP_CONFIG_FIELDS || !integer(v, 1, UINT32_MAX) || !value)
            return false;
        if (seen & (1u << i))
            return false;
        seen |= 1u << i;
        uint32_t revision = (uint32_t)v->valuedouble;
        if (out->versions[i] != revision) {
            if (!apply(out, i, value))
                return false;
            out->versions[i] = revision;
            *changed = true;
            if (i >= 7 && i <= 9 && !cJSON_IsNull(value))
                *reconnect = true;
        }
        if (!cJSON_AddNumberToObject(versions, fields[i], revision))
            return false;
    }
    return pp_settings_valid(out);
}

char *pp_settings_encode(const pp_settings_t *s) {
    if (!pp_settings_valid(s))
        return NULL;
    cJSON *root = cJSON_CreateObject(), *values = cJSON_CreateObject(),
          *versions = cJSON_CreateArray();
    if (!root || !values || !versions) {
        cJSON_Delete(root);
        cJSON_Delete(values);
        cJSON_Delete(versions);
        return NULL;
    }
    bool ok = cJSON_AddNumberToObject(root, "format", 2) &&
              cJSON_AddStringToObject(root, "device_id", s->device) &&
              cJSON_AddStringToObject(root, "credential", s->credential) &&
              cJSON_AddStringToObject(root, "binding", s->binding) &&
              cJSON_AddBoolToObject(root, "paired", s->paired) &&
              cJSON_AddNumberToObject(values, "volume", s->volume) &&
              cJSON_AddStringToObject(values, "input_mode", s->queue ? "queue" : "steer") &&
              cJSON_AddBoolToObject(values, "keep_online", s->keep_online) &&
              cJSON_AddNumberToObject(values, "sleep_seconds", s->sleep_seconds) &&
              cJSON_AddStringToObject(values, "speech_mode",
                                      s->speech_mode == 0   ? "brief"
                                      : s->speech_mode == 1 ? "full"
                                                            : "off") &&
              cJSON_AddBoolToObject(values, "offline_cache", s->offline_cache) &&
              cJSON_AddBoolToObject(values, "show_images", s->show_images) &&
              cJSON_AddNumberToObject(values, "brightness", s->brightness) &&
              cJSON_AddNumberToObject(values, "text_size", s->text_size);
    for (unsigned i = 0; ok && i < PP_CONFIG_FIELDS; i++) {
        cJSON *n = cJSON_CreateNumber(s->versions[i]);
        if (!n || !cJSON_AddItemToArray(versions, n)) {
            cJSON_Delete(n);
            ok = false;
        }
    }
    if (s->url[0] && !cJSON_AddStringToObject(values, "server_url", s->url))
        ok = false;
    if (s->ca[0] && !cJSON_AddStringToObject(values, "trusted_ca", s->ca))
        ok = false;
    if (s->wifi_count) {
        cJSON *networks = cJSON_AddArrayToObject(values, "wifi_networks");
        if (!networks)
            ok = false;
        for (unsigned i = 0; ok && i < s->wifi_count; i++) {
            cJSON *n = cJSON_CreateObject();
            if (!n) {
                ok = false;
                break;
            }
            if (!cJSON_AddStringToObject(n, "ssid", s->wifi[i].ssid) ||
                !cJSON_AddStringToObject(n, "password", s->wifi[i].password) ||
                !cJSON_AddItemToArray(networks, n)) {
                cJSON_Delete(n);
                ok = false;
                break;
            }
        }
    }
    if (!cJSON_AddItemToObject(root, "values", values)) {
        cJSON_Delete(values);
        ok = false;
    }
    if (!cJSON_AddItemToObject(root, "versions", versions)) {
        cJSON_Delete(versions);
        ok = false;
    }
    char *raw = ok ? cJSON_PrintUnformatted(root) : NULL;
    cJSON_Delete(root);
    return raw;
}
bool pp_settings_decode(const char *raw, pp_settings_t *out) {
    if (!raw || strlen(raw) > 8192 || strstr(raw, "\\u0000"))
        return false;
    cJSON *root = cJSON_Parse(raw);
    if (!root)
        return false;
    memset(out, 0, sizeof(*out));
    out->magic = PP_SETTINGS_MAGIC;
    out->brightness = 65;
    out->text_size = 16;
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(root, "format"),
                *values = cJSON_GetObjectItemCaseSensitive(root, "values"),
                *versions = cJSON_GetObjectItemCaseSensitive(root, "versions"),
                *paired = cJSON_GetObjectItemCaseSensitive(root, "paired");
    bool ok = integer(version, 2, 2) && cJSON_IsObject(values) &&
              cJSON_GetArraySize(values) <= PP_CONFIG_FIELDS && cJSON_IsArray(versions) &&
              cJSON_GetArraySize(versions) >= 10 &&
              cJSON_GetArraySize(versions) <= PP_CONFIG_FIELDS && cJSON_IsBool(paired) &&
              string_field(root, "device_id", out->device, sizeof(out->device), false) &&
              string_field(root, "credential", out->credential, sizeof(out->credential), false) &&
              string_field(root, "binding", out->binding, sizeof(out->binding), false);
    out->paired = cJSON_IsTrue(paired);
    for (unsigned i = 0; ok && i < 7; i++)
        if (!cJSON_GetObjectItemCaseSensitive(values, fields[i]))
            ok = false;
    const cJSON *item;
    cJSON_ArrayForEach(item, values) {
        unsigned i = 0;
        while (i < PP_CONFIG_FIELDS && (!item->string || strcmp(item->string, fields[i])))
            i++;
        if (i == PP_CONFIG_FIELDS || cJSON_IsNull(item) || !apply(out, i, item))
            ok = false;
    }
    /* Newly added display fields default without replacing pairing or local preferences. */
    for (unsigned i = 0; ok && i < (unsigned)cJSON_GetArraySize(versions); i++) {
        const cJSON *n = cJSON_GetArrayItem(versions, i);
        if (!integer(n, 0, UINT32_MAX))
            ok = false;
        else
            out->versions[i] = (uint32_t)n->valuedouble;
    }
    cJSON_Delete(root);
    return ok && pp_settings_valid(out);
}

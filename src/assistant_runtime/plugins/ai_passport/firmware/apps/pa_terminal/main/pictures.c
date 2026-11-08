#include "pictures.h"
#include "image_model.h"
#include "history_cache.h"
#include "network.h"
#include "ui.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha256.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static pp_image_transfer_t transfer;
static unsigned char *pixels;
static char message_id[160], image_id[33];
static unsigned requested_width, requested_height;
static bool loading, shown;
static bool verified, prefetch;
static unsigned cache_serial, cache_ticket;
static uint32_t started;
static void (*status)(const char *);
static cJSON *command(const char *type) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "type", type);
    return j;
}
static const char *string(const cJSON *j, const char *key) {
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsString(value) ? value->valuestring : "";
}
static unsigned number(const cJSON *j, const char *key) {
    const cJSON *value = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsNumber(value) && value->valuedouble >= 0 && value->valuedouble <= UINT32_MAX &&
                   value->valuedouble == (unsigned)value->valuedouble
               ? (unsigned)value->valuedouble
               : 0;
}
void pp_picture_init(void (*callback)(const char *)) { status = callback; }
bool pp_picture_active(void) { return loading || shown; }
bool pp_picture_loading(void) { return loading; }
bool pp_picture_close(void) {
    if (pixels && !pp_ui_image(NULL, 0, 0)) {
        status("屏幕忙，尚未释放图片");
        return false;
    }
    bool active = pp_picture_active();
    char key[256], owner[192];
    if (pixels && verified && pp_history_cache_image_key(message_id, image_id, key) &&
        pp_history_cache_owner(message_id, owner)) {
        unsigned char hash[32];
        if (pp_image_preview(&transfer, pixels, 64, 32) &&
            mbedtls_sha256(pixels, transfer.bytes, hash, 0) == 0) {
            for (unsigned i = 0; i < 32; i++)
                snprintf(transfer.version + i * 2, 3, "%02x", hash[i]);
            void *compact = realloc(pixels, transfer.bytes);
            if (compact)
                pixels = compact;
            pp_disk_write_take(key, owner, false, pixels, transfer.bytes, &transfer,
                               sizeof(transfer));
            pixels = NULL;
        }
    }
    free(pixels);
    pixels = NULL;
    loading = shown = false;
    verified = prefetch = false;
    cache_ticket = 0;
    memset(&transfer, 0, sizeof(transfer));
    message_id[0] = image_id[0] = 0;
    if (active)
        pp_net_send(command("image.close"));
    return true;
}
static void next_chunk(void) {
    cJSON *j = command("image.chunk");
    cJSON_AddStringToObject(j, "version", transfer.version);
    cJSON_AddNumberToObject(j, "offset", transfer.received);
    if (!pp_net_send(j)) {
        pp_picture_close();
        status("图片传输已中断");
    }
}
static void picture_error(const char *reason) {
    bool background = prefetch;
    pp_picture_close();
    if (!background)
        status(reason);
}
static bool remote_open(void) {
    if (!pp_net_connected()) {
        if (!prefetch)
            status("图片未完整缓存，请联网读取");
        return false;
    }
    cJSON *j = command("image.open");
    cJSON_AddStringToObject(j, "message_id", message_id);
    cJSON_AddStringToObject(j, "image_id", image_id);
    cJSON_AddNumberToObject(j, "width", requested_width);
    cJSON_AddNumberToObject(j, "height", requested_height);
    return pp_net_send(j);
}
static bool open_picture(const char *message, const char *image, bool background) {
    if (strlen(message) >= sizeof(message_id) || strlen(image) >= sizeof(image_id) ||
        !pp_picture_close())
        return false;
    unsigned available = heap_caps_get_free_size(MALLOC_CAP_8BIT);
    unsigned largest = heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
    /* Leave room for socket/JSON/audio queues. Never retain the original image. */
    const unsigned dimensions[][2] = {{160, 96}, {128, 80}, {96, 64}, {64, 48}, {64, 32}, {48, 32}};
    requested_width = requested_height = 0;
    for (unsigned i = 0; i < sizeof(dimensions) / sizeof(dimensions[0]); i++) {
        unsigned bytes = dimensions[i][0] * dimensions[i][1] * 2;
        if ((!background || (dimensions[i][0] <= 64 && dimensions[i][1] <= 32)) &&
            available >= bytes + 20000 && largest >= bytes + 1024) {
            requested_width = dimensions[i][0];
            requested_height = dimensions[i][1];
            break;
        }
    }
    if (!requested_width) {
        if (!background)
            status("图片内存暂不足，请稍后查看");
        return false;
    }
    strcpy(message_id, message);
    strcpy(image_id, image);
    prefetch = background;
    loading = true;
    started = (uint32_t)(esp_timer_get_time() / 1000);
    if (!background)
        status("正在读取图片");
    char key[256];
    cache_ticket = (++cache_serial << 2) | 3u;
    if (pp_history_cache_image_key(message, image, key) && pp_disk_read(key, cache_ticket))
        return true;
    cache_ticket = 0;
    if (!remote_open()) {
        loading = false;
        return false;
    }
    return true;
}
bool pp_picture_open(const char *message, const char *image) {
    return open_picture(message, image, false);
}
bool pp_picture_prefetch(const char *message, const char *image) {
    return !pp_picture_active() && open_picture(message, image, true);
}
static bool show_complete(void) {
    unsigned char digest[32];
    char hash[65] = "";
    if (mbedtls_sha256(pixels, transfer.bytes, digest, 0))
        return false;
    for (unsigned i = 0; i < 32; i++)
        snprintf(hash + i * 2, 3, "%02x", digest[i]);
    if (strcmp(hash, transfer.version))
        return false;
    verified = true;
    if (prefetch) {
        pp_picture_close(); /* Transfers this buffer to the storage owner. */
        return true;
    }
    if (!pp_ui_image(pixels, transfer.width, transfer.height))
        return false;
    loading = false;
    shown = true;
    status(pp_net_connected() ? "图片" : "离线图片");
    pp_net_send(command("image.close"));
    return true;
}
void pp_picture_cached(pp_disk_result_t *result) {
    if (!loading || result->ticket != cache_ticket)
        return;
    cache_ticket = 0;
    pp_image_transfer_t saved;
    bool valid = result->hit && result->value.aux_size == sizeof(saved);
    if (valid) {
        memcpy(&saved, result->value.aux, sizeof(saved));
        valid = saved.version[64] == 0 && saved.bytes == result->value.size &&
                pp_image_begin(&transfer, "rgb565le", saved.version, saved.width, saved.height,
                               saved.bytes, requested_width, requested_height);
    }
    if (valid) {
        pixels = result->value.data;
        result->value.data = NULL;
        transfer.received = transfer.bytes;
        if (show_complete())
            return;
        free(pixels);
        pixels = NULL;
        verified = false;
    }
    if (!remote_open())
        pp_picture_close();
}
bool pp_picture_receive(const cJSON *j) {
    const char *type = string(j, "type");
    if (!strcmp(type, "image.open")) {
        if (!loading || strcmp(string(j, "message_id"), message_id) ||
            strcmp(string(j, "image_id"), image_id))
            return false;
        if (pixels ||
            !pp_image_begin(&transfer, string(j, "format"), string(j, "version"),
                            number(j, "width"), number(j, "height"), number(j, "bytes"),
                            requested_width, requested_height) ||
            !(pixels = malloc(transfer.bytes))) {
            picture_error("图片格式或内存不满足要求");
        } else
            next_chunk();
        return true;
    }
    if (strcmp(type, "image.chunk") || !loading || !pixels ||
        strcmp(string(j, "version"), transfer.version))
        return false;
    const char *encoded = string(j, "data");
    size_t length = 0, offset = transfer.received;
    bool valid =
        number(j, "offset") == offset && strlen(encoded) <= 2732 &&
        mbedtls_base64_decode(pixels + offset, transfer.bytes - offset, &length,
                              (const unsigned char *)encoded, strlen(encoded)) == 0 &&
        pp_image_chunk(&transfer, string(j, "version"), offset, length, number(j, "next_offset"));
    if (!valid) {
        picture_error("图片分块不完整，请重新查看");
    } else if (transfer.received < transfer.bytes)
        next_chunk();
    else {
        if (!show_complete()) {
            picture_error("图片校验或显示失败");
        }
    }
    return true;
}
bool pp_picture_poll(uint32_t now) {
    if (!loading || now - started <= 20000)
        return false;
    bool background = prefetch;
    picture_error("图片读取超时，可翻页后重新查看");
    return !background;
}

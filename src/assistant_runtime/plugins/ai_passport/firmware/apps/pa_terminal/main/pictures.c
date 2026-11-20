#include "pictures.h"
#include "image_model.h"
#include "history_cache.h"
#include "network.h"
#include "ui.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mbedtls/sha256.h"
#include "mbedtls/base64.h"
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static pp_image_transfer_t transfer;
static unsigned char *pixels;
static pp_cache_result_t borrowed_pixels;
static double opened_revision;
static char message_id[160], image_id[33], owner[192];
static bool loading, shown, unavailable;
static atomic_uint displayed_bytes;
static bool stream_supported, hd_attempted;
static uint32_t stream_sequence;
static struct {
    enum { STREAM_OFF, STREAM_OPEN, STREAM_METADATA, STREAM_SCREEN, STREAM_NEXT,
           STREAM_RECEIVE, STREAM_DRAW, STREAM_SHOWN } state;
    const char *failure;
    uint32_t request;
    unsigned row, rows;
    bool screen;
    pp_buffer_t strip, preview;
    pp_image_transfer_t raster, thumb;
    mbedtls_sha256_context digest;
} stream;
static bool verified;
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
void pp_picture_stream_supported(bool supported) {
    if (supported && !stream_supported && unavailable)
        hd_attempted = false;
    stream_supported = supported;
}
bool pp_picture_active(void) { return loading || shown || unavailable; }
unsigned pp_picture_displayed_bytes(void) { return displayed_bytes; }
static void release_pixels(void) {
    pp_objects_release(&borrowed_pixels);
    pixels = NULL;
}
static bool stream_command(const char *type, unsigned row) {
    cJSON *j = command(type);
    cJSON_AddNumberToObject(j, "request_id", stream.request);
    if (!strcmp(type, "image.stream.open")) {
        cJSON_AddStringToObject(j, "message_id", message_id);
        cJSON_AddStringToObject(j, "image_id", image_id);
        cJSON_AddNumberToObject(j, "width", PP_IMAGE_VIEW_WIDTH);
        cJSON_AddNumberToObject(j, "height", PP_IMAGE_VIEW_HEIGHT);
    } else if (!strcmp(type, "image.stream.next"))
        cJSON_AddNumberToObject(j, "row", row);
    return pp_net_send(j);
}
static void release_stream(void) {
    pp_net_expect_image(0);
    if (stream.state > STREAM_OPEN && stream.state < STREAM_SHOWN)
        stream_command("image.stream.close", 0);
    pp_buffer_free(&stream.strip);
    pp_buffer_free(&stream.preview);
    mbedtls_sha256_free(&stream.digest);
    memset(&stream, 0, sizeof(stream));
}
static bool cache_preview(pp_buffer_t *buffer, pp_image_transfer_t *image) {
    char key[256];
    unsigned char hash[32];
    if (!pp_history_cache_image_key(message_id, opened_revision, image_id, key) ||
        mbedtls_sha256((const unsigned char *)buffer->data, image->bytes, hash, 0) != 0)
        return false;
    for (unsigned i = 0; i < 32; i++)
        snprintf(image->version + i * 2, 3, "%02x", hash[i]);
    buffer->size = image->received = image->bytes;
    if (buffer->capacity > image->bytes)
        (void)pp_buffer_reserve(buffer, image->bytes, PP_MEMORY_BACKGROUND, PP_MEMORY_IMAGE);
    return pp_history_cache_preview(key, owner, buffer, image);
}
static bool release_view(void) {
    if ((shown || stream.screen || stream.state == STREAM_SCREEN) && !pp_ui_image(NULL, 0, 0)) {
        status("屏幕忙，尚未释放图片");
        return false;
    }
    release_stream();
    release_pixels();
    displayed_bytes = 0;
    loading = shown = unavailable = false;
    hd_attempted = false;
    verified = false;
    cache_ticket = 0;
    memset(&transfer, 0, sizeof(transfer));
    return true;
}
bool pp_picture_close(void) {
    if (!release_view())
        return false;
    message_id[0] = image_id[0] = 0;
    return true;
}
static void picture_error(const char *reason) {
    bool attempted = hd_attempted;
    if (release_view()) {
        hd_attempted = attempted;
        unavailable = true; /* Keep the logical image page navigable, even without pixels. */
        status(reason);
    }
}
static bool start_hd(void);
static bool missing_preview(void) {
    if (start_hd())
        return true;
    picture_error("此图未保存预览");
    return true;
}
bool pp_picture_open(const char *message, const char *image, double revision, const char *group) {
    if (strlen(message) >= sizeof(message_id) || strlen(image) != 32 || strlen(group) >= sizeof(owner) ||
        !pp_picture_close())
        return false;
    strcpy(message_id, message);
    strcpy(image_id, image);
    strcpy(owner, group);
    opened_revision = revision;
    loading = true;
    started = (uint32_t)(esp_timer_get_time() / 1000);
    status("正在读取图片");
    char key[256];
    if (pp_history_cache_image_key(message, revision, image, key)) {
        cache_ticket = (++cache_serial << 2) | 3u;
        if (pp_objects_read(
                key, cache_ticket,
                (pp_object_read_t){.priority = PP_MEMORY_NORMAL, .heap_reserve = pp_image_headroom(false)}))
            return true;
        cache_ticket = 0;
    }
    return missing_preview();
}
static bool start_hd(void) {
    if (stream.state != STREAM_OFF || hd_attempted || !stream_supported || !pp_net_connected())
        return false;
    if (++stream_sequence == 0)
        ++stream_sequence;
    stream.request = stream_sequence;
    stream.state = STREAM_OPEN;
    unavailable = false;
    hd_attempted = loading = true;
    started = (uint32_t)(esp_timer_get_time() / 1000);
    return true;
}
static bool pixels_verified(void) {
    unsigned char digest[32];
    char hash[65] = "";
    if (mbedtls_sha256(pixels, transfer.bytes, digest, 0))
        return false;
    for (unsigned i = 0; i < 32; i++)
        snprintf(hash + i * 2, 3, "%02x", digest[i]);
    return !strcmp(hash, transfer.version);
}
static bool show_complete(void) {
    verified = pixels_verified();
    if (!verified)
        return false;
    if (!pp_ui_image(pixels, transfer.width, transfer.height))
        return false;
    ESP_LOGI("pp_picture", "display %ux%u source=%s heap=%u", transfer.width, transfer.height,
             borrowed_pixels.value.bytes.data ? "cache" : "network",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT));
    loading = false;
    shown = true;
    displayed_bytes = transfer.bytes;
    status("图片预览");
    return true;
}
static void stream_failed(const char *reason) {
    if (!stream.failure) {
        ESP_LOGW("pp_picture", "stream stopped at row=%u", stream.row);
        stream.failure = reason;
    }
    if (!shown || !verified || !pixels) {
        picture_error(reason);
        return;
    }
    /* LVGL may still reference the last strip. Detach it before releasing RAM. */
    if (!pp_ui_image(pixels, transfer.width, transfer.height))
        return;
    release_stream();
    loading = false;
    displayed_bytes = transfer.bytes;
    status(pp_net_connected() ? "高清暂不可用，显示预览" : "离线图片");
}

bool pp_picture_disconnected(void) {
    stream_supported = false;
    if (unavailable)
        return true;
    if (!shown) {
        if (cache_ticket)
            return true;
        if (loading)
            picture_error("图片传输中断");
        return unavailable;
    }
    if (loading)
        stream_failed("图片传输中断");
    hd_attempted = stream.state == STREAM_SHOWN;
    return true;
}

static void stream_metadata(const cJSON *j) {
    if (stream.state != STREAM_METADATA || number(j, "request_id") != stream.request)
        return;
    mbedtls_sha256_init(&stream.digest);
    if (strcmp(string(j, "message_id"), message_id) || strcmp(string(j, "image_id"), image_id) ||
        !pp_image_begin(&stream.raster, string(j, "format"), string(j, "version"),
                        number(j, "width"), number(j, "height"), number(j, "bytes"),
                        PP_IMAGE_VIEW_WIDTH, PP_IMAGE_VIEW_HEIGHT) ||
        mbedtls_sha256_starts(&stream.digest, 0) != 0) {
        stream_failed("高清图片格式异常，可重试");
        return;
    }
    stream.state = STREAM_SCREEN;
    started = (uint32_t)(esp_timer_get_time() / 1000);
}

void pp_picture_receive_strip(pp_buffer_t *frame) {
    if (stream.state != STREAM_RECEIVE || frame->size < 8)
        return;
    const unsigned char *p = (const unsigned char *)frame->data;
    uint32_t request = ((uint32_t)p[4] << 24) | ((uint32_t)p[5] << 16) | ((uint32_t)p[6] << 8) | p[7];
    if (stream.request != request)
        return; /* Already navigated to a different picture. */
    pp_image_strip_t strip;
    unsigned char hash[32];
    if (!pp_image_strip_parse(&strip, p, frame->size, stream.raster.width, stream.raster.height) ||
        strip.row != stream.row ||
        mbedtls_sha256(p + PP_IMAGE_STRIP_HEADER, frame->size - PP_IMAGE_STRIP_HEADER, hash, 0) ||
        memcmp(hash, p + 12, 8)) {
        stream_failed("高清图片校验失败，可重试");
        return;
    }
    stream.strip = pp_buffer_take(frame);
    stream.rows = strip.rows;
    stream.state = STREAM_DRAW;
    started = (uint32_t)(esp_timer_get_time() / 1000);
}

static void stream_poll(void) {
    if (stream.failure) {
        stream_failed(stream.failure);
    } else if (stream.state == STREAM_OPEN) {
        if (stream_command("image.stream.open", 0))
            stream.state = STREAM_METADATA;
        else
            stream_failed("高清图片请求失败，可重试");
    } else if (stream.state == STREAM_SCREEN) {
        if (!pp_ui_stream_begin(stream.raster.width, stream.raster.height))
            return;
        stream.screen = true;
        if (!verified) {
            pp_image_preview_dimensions(stream.raster.width, stream.raster.height,
                                         &stream.thumb.width, &stream.thumb.height);
            stream.thumb.bytes = stream.thumb.width * stream.thumb.height * 2u;
            if (pp_image_has_headroom(heap_caps_get_free_size(MALLOC_CAP_8BIT),
                                       stream.thumb.bytes + PP_IMAGE_STRIP_MAX, false))
                (void)pp_buffer_reserve(&stream.preview, stream.thumb.bytes,
                                        PP_MEMORY_BACKGROUND, PP_MEMORY_IMAGE);
        }
        stream.state = STREAM_NEXT;
    } else if (stream.state == STREAM_NEXT) {
        pp_net_expect_image(stream.request);
        if (stream_command("image.stream.next", stream.row))
            stream.state = STREAM_RECEIVE;
        else
            stream_failed("高清图片传输中断，可重试");
    } else if (stream.state == STREAM_DRAW) {
        const unsigned char *pixels = (const unsigned char *)stream.strip.data + PP_IMAGE_STRIP_HEADER;
        if (!pp_ui_stream_draw(pixels, stream.row, stream.rows))
            return;
        if (mbedtls_sha256_update(&stream.digest, pixels, stream.strip.size - PP_IMAGE_STRIP_HEADER)) {
            stream_failed("高清图片校验失败，可重试");
            return;
        }
        if (stream.preview.data)
            pp_image_preview_rows((unsigned char *)stream.preview.data, stream.thumb.width,
                                   stream.thumb.height, pixels, stream.raster.width, stream.raster.height,
                                   stream.row, stream.rows);
        pp_buffer_free(&stream.strip);
        stream.row += stream.rows;
        displayed_bytes = (unsigned)stream.row * stream.raster.width * 2;
        if (stream.row < stream.raster.height) {
            stream.state = STREAM_NEXT;
            return;
        }
        unsigned char hash[32];
        char hex[65];
        if (mbedtls_sha256_finish(&stream.digest, hash)) {
            stream_failed("高清图片校验失败，可重试");
            return;
        }
        for (unsigned i = 0; i < 32; i++)
            snprintf(hex + i * 2, 3, "%02x", hash[i]);
        if (strcmp(hex, stream.raster.version)) {
            stream_failed("高清图片校验失败，可重试");
            return;
        }
        bool saved = verified || (stream.preview.data && cache_preview(&stream.preview, &stream.thumb));
        pp_buffer_free(&stream.preview);
        stream.state = STREAM_SHOWN;
        loading = false;
        shown = true;
        ESP_LOGI("pp_picture", "display %ux%u source=stream heap=%u", stream.raster.width, stream.raster.height,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT));
        status(saved ? "图片" : "此图预览未保存");
    }
}

void pp_picture_cached(pp_cache_result_t *result) {
    if (!loading || result->ticket != cache_ticket)
        return;
    cache_ticket = 0;
    pp_image_transfer_t saved;
    bool valid = result->status == PP_CACHE_HIT && result->value.aux_size == sizeof(saved);
    if (valid) {
        memcpy(&saved, result->value.aux, sizeof(saved));
        valid = saved.version[64] == 0 && saved.bytes == result->value.bytes.size &&
                pp_image_begin(&transfer, "rgb565le", saved.version, saved.width, saved.height,
                               saved.bytes, PP_IMAGE_PREVIEW_WIDTH, PP_IMAGE_PREVIEW_HEIGHT);
    }
    if (valid) {
        if (result->lease) {
            if (!pp_objects_retain(result, &borrowed_pixels)) {
                picture_error("读取忙，请稍后重试");
                return;
            }
        } else {
            borrowed_pixels = *result;
            *result = (pp_cache_result_t){0};
        }
        pixels = (unsigned char *)borrowed_pixels.value.bytes.data;
        transfer.received = transfer.bytes;
        if (show_complete())
            return;
        if (verified) {
            picture_error("图片校验或显示失败");
            return;
        }
        release_pixels();
        verified = false;
    }
    if (result->status == PP_CACHE_BUSY || result->status == PP_CACHE_NO_MEMORY ||
        result->status == PP_CACHE_IO_ERROR) {
        if (!pp_net_connected()) {
            picture_error(result->status == PP_CACHE_NO_MEMORY
                              ? "本地图片内存暂不足，请稍后查看"
                              : "本地图片读取忙，请稍后重试");
            return;
        }
    }
    missing_preview();
}
static void receive_preview(const cJSON *j) {
    const char *id = string(j, "message_id"), *group = string(j, "group");
    const cJSON *revision = cJSON_GetObjectItemCaseSensitive(j, "updated");
    char key[256];
    if (strlen(id) >= sizeof(message_id) || !*group || strlen(group) >= sizeof(owner) ||
        !cJSON_IsNumber(revision) || revision->valuedouble < 0 ||
        !pp_history_cache_image_key(id, revision->valuedouble, string(j, "image_id"), key))
        return;
    unsigned width = number(j, "width"), height = number(j, "height");
    const char *encoded = string(j, "pixels");
    pp_image_transfer_t meta;
    pp_buffer_t data = {0};
    unsigned char digest[32];
    char hex[65];
    size_t len = strlen(encoded);
    bool ok = pp_image_begin(&meta, "rgb565le", string(j, "version"), width, height,
                             (size_t)width * height * 2, PP_IMAGE_PREVIEW_WIDTH, PP_IMAGE_PREVIEW_HEIGHT) &&
              len == 4 * ((meta.bytes + 2) / 3) &&
              pp_buffer_reserve(&data, meta.bytes, PP_MEMORY_BACKGROUND, PP_MEMORY_IMAGE) &&
              !mbedtls_base64_decode((unsigned char *)data.data, data.capacity, &data.size,
                                     (const unsigned char *)encoded, len) && data.size == meta.bytes &&
              !mbedtls_sha256((unsigned char *)data.data, data.size, digest, 0);
    if (ok) {
        for (unsigned i = 0; i < sizeof(digest); i++)
            snprintf(hex + 2 * i, 3, "%02x", digest[i]);
        meta.received = meta.bytes;
        ok = !strcmp(hex, meta.version) && pp_history_cache_preview(key, group, &data, &meta);
    }
    pp_buffer_free(&data);
    if (!ok) {
        ESP_LOGW("pp_picture", "live preview rejected for image %.32s", string(j, "image_id"));
        status("此图预览未保存");
    }
}
bool pp_picture_receive(const cJSON *j) {
    const char *type = string(j, "type");
    if (!strcmp(type, "message.preview")) {
        receive_preview(j);
        return true;
    }
    if (!strcmp(type, "image.stream.open")) {
        stream_metadata(j);
        return true;
    }
    if (!strcmp(type, "error") && !strncmp(string(j, "operation"), "image.stream.", 13)) {
        if (stream.state > STREAM_OPEN && stream.state < STREAM_SHOWN &&
            number(j, "request_id") == stream.request)
            stream_failed("高清图片暂不可用，可重试");
        return true;
    }
    return false;
}
bool pp_picture_poll(uint32_t now) {
    if (!loading) {
        if (shown || unavailable)
            start_hd();
        return false;
    }
    if (now - started > 20000) {
        if (stream.state != STREAM_OFF)
            stream_failed("高清图片读取超时，可重试");
        else
            picture_error("本地预览读取超时");
    } else if (stream.state != STREAM_OFF) {
        stream_poll();
    }
    return !pp_picture_active();
}

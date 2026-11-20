#include "pictures.h"
#include "image_model.h"
#include "history_cache.h"
#include "network.h"
#include "ui.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "mbedtls/base64.h"
#include "mbedtls/sha256.h"
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static pp_image_transfer_t transfer;
static unsigned char *pixels;
static pp_buffer_t owned_pixels;
static pp_cache_result_t borrowed_pixels;
static unsigned prefetch_write;
static char prefetch_message[160], prefetch_image[33];
static double opened_revision, prefetch_write_revision;
static bool revision_known;
static char message_id[160], image_id[33];
static unsigned requested_width, requested_height;
static bool loading, shown, remote_active;
static atomic_uint displayed_bytes;
static enum { PICTURE_WAITING, PICTURE_REQUEST, PICTURE_ALLOCATE } pending;
static size_t request_limit;
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
void pp_picture_stream_supported(bool supported) { stream_supported = supported; }
bool pp_picture_active(void) { return loading || shown; }
bool pp_picture_loading(void) { return loading && !shown; }
unsigned pp_picture_displayed_bytes(void) { return displayed_bytes; }
static void release_pixels(void) {
    pp_objects_release(&borrowed_pixels);
    pp_buffer_free(&owned_pixels);
    pixels = NULL;
}
static void prefetch_done(bool completed) {
    if (prefetch)
        pp_history_cache_picture_done(message_id, image_id, opened_revision, completed);
}
static void close_remote(void) {
    if (remote_active) {
        remote_active = false;
        pp_net_send(command("image.close"));
    }
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
    char key[256], owner[192];
    double revision;
    unsigned char hash[32];
    if (!revision_known || !pp_history_cache_revision(message_id, &revision) ||
        revision != opened_revision || !pp_history_cache_image_key(message_id, image_id, key) ||
        !pp_history_cache_owner(message_id, owner) ||
        mbedtls_sha256((const unsigned char *)buffer->data, image->bytes, hash, 0) != 0)
        return false;
    for (unsigned i = 0; i < 32; i++)
        snprintf(image->version + i * 2, 3, "%02x", hash[i]);
    buffer->size = image->received = image->bytes;
    if (buffer->capacity > image->bytes)
        (void)pp_buffer_reserve(buffer, image->bytes, PP_MEMORY_BACKGROUND, PP_MEMORY_IMAGE);
    unsigned ticket = prefetch ? (++cache_serial << 2) | 3u : 0;
    if (!pp_objects_write(key, owner, (pp_object_write_t){0}, buffer, image, sizeof(*image), ticket))
        return false;
    if (ticket) {
        prefetch_write = ticket;
        prefetch_write_revision = opened_revision;
        strcpy(prefetch_message, message_id);
        strcpy(prefetch_image, image_id);
    }
    return true;
}
bool pp_picture_close(void) {
    if ((shown || stream.screen || stream.state == STREAM_SCREEN) && !pp_ui_image(NULL, 0, 0)) {
        status("屏幕忙，尚未释放图片");
        return false;
    }
    release_stream();
    if (owned_pixels.data && verified) {
        if (!pp_image_preview(&transfer, pixels, PP_IMAGE_PREVIEW_WIDTH, PP_IMAGE_PREVIEW_HEIGHT) ||
            !cache_preview(&owned_pixels, &transfer))
            prefetch_done(false);
    } else if (prefetch && !borrowed_pixels.value.bytes.data)
        prefetch_done(false);
    release_pixels();
    displayed_bytes = 0;
    loading = shown = false;
    pending = PICTURE_WAITING;
    hd_attempted = false;
    verified = prefetch = false;
    cache_ticket = 0;
    memset(&transfer, 0, sizeof(transfer));
    message_id[0] = image_id[0] = 0;
    close_remote();
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
static bool choose_size(size_t limit, size_t *available, size_t *largest) {
    *available = heap_caps_get_free_size(MALLOC_CAP_8BIT);
    *largest = heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
    return pp_image_request_size(*available, *largest, limit, prefetch,
                                 &requested_width, &requested_height);
}
static void request_remote(size_t limit) {
    const size_t preview_limit = PP_IMAGE_PREVIEW_WIDTH * PP_IMAGE_PREVIEW_HEIGHT * 2u;
    if (limit > preview_limit)
        limit = preview_limit;
    request_limit = limit;
    pending = PICTURE_REQUEST;
}
static bool remote_open(size_t limit) {
    if (!pp_net_connected()) {
        if (!prefetch)
            status("图片未完整缓存，请联网读取");
        return false;
    }
    size_t available, largest;
    bool fits = choose_size(limit, &available, &largest);
    if (!fits && !prefetch) {
        pp_memory_reclaim(PP_CACHE_RESIDENT_BYTES, PP_MEMORY_NORMAL);
        fits = choose_size(limit, &available, &largest);
    }
    if (!fits) {
        if (!prefetch) {
            ESP_LOGW("pp_picture", "no image buffer heap=%u largest=%u limit=%u",
                     (unsigned)available, (unsigned)largest, (unsigned)limit);
            status("图片内存暂不足，请稍后查看");
        }
        return false;
    }
    if (!prefetch)
        ESP_LOGI("pp_picture", "request %ux%u heap=%u largest=%u",
                 requested_width, requested_height, (unsigned)available, (unsigned)largest);
    cJSON *j = command("image.open");
    cJSON_AddStringToObject(j, "message_id", message_id);
    cJSON_AddStringToObject(j, "image_id", image_id);
    cJSON_AddNumberToObject(j, "width", requested_width);
    cJSON_AddNumberToObject(j, "height", requested_height);
    if (pp_net_send(j)) {
        remote_active = true;
        return true;
    }
    if (!prefetch)
        status("图片请求发送失败，可重新查看");
    return false;
}
static bool open_picture(const char *message, const char *image, bool background) {
    if ((background && prefetch_write) || strlen(message) >= sizeof(message_id) || strlen(image) >= sizeof(image_id) ||
        !pp_picture_close())
        return false;
    strcpy(message_id, message);
    strcpy(image_id, image);
    prefetch = background;
    loading = true;
    started = (uint32_t)(esp_timer_get_time() / 1000);
    if (!background)
        status("正在读取图片");
    char key[256];
    revision_known = pp_history_cache_revision(message, &opened_revision);
    if (pp_history_cache_image_key(message, image, key)) {
        cache_ticket = (++cache_serial << 2) | 3u;
        pp_object_read_t policy = {.priority = background ? PP_MEMORY_BACKGROUND : PP_MEMORY_NORMAL,
                                   .heap_reserve = pp_image_headroom(background)};
        if (pp_objects_read(key, cache_ticket, policy))
            return true;
        cache_ticket = 0;
        if (background || !pp_net_connected()) {
            picture_error("本地图片读取忙，请稍后重试");
            return false;
        }
    }
    request_remote(SIZE_MAX);
    return true;
}
bool pp_picture_open(const char *message, const char *image) {
    return open_picture(message, image, false);
}
bool pp_picture_prefetch(const char *message, const char *image) {
    return !pp_picture_active() && open_picture(message, image, true);
}
static bool start_hd(void) {
    if (stream.state != STREAM_OFF || prefetch || hd_attempted || !stream_supported || !pp_net_connected())
        return false;
    close_remote();
    if (++stream_sequence == 0)
        ++stream_sequence;
    stream.request = stream_sequence;
    stream.state = STREAM_OPEN;
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
    if (prefetch) {
        pp_picture_close(); /* Transfers this buffer to the storage owner. */
        return true;
    }
    if (!pp_ui_image(pixels, transfer.width, transfer.height))
        return false;
    ESP_LOGI("pp_picture", "display %ux%u source=%s heap=%u", transfer.width, transfer.height,
             borrowed_pixels.value.bytes.data ? "cache" : "network",
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT));
    loading = false;
    shown = true;
    displayed_bytes = transfer.bytes;
    status(pp_net_connected() ? "图片" : "离线图片");
    close_remote();
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
    if (!shown)
        return false;
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
        if (revision_known && !verified) {
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
        if (stream.preview.data)
            (void)cache_preview(&stream.preview, &stream.thumb);
        pp_buffer_free(&stream.preview);
        stream.state = STREAM_SHOWN;
        loading = false;
        shown = true;
        ESP_LOGI("pp_picture", "display %ux%u source=stream heap=%u", stream.raster.width, stream.raster.height,
                 (unsigned)heap_caps_get_free_size(MALLOC_CAP_8BIT));
        status("图片");
    }
}

void pp_picture_cached(pp_cache_result_t *result) {
    if (result->written) {
        if (prefetch_write && result->ticket == prefetch_write) {
            prefetch_write = 0;
            pp_history_cache_picture_done(prefetch_message, prefetch_image, prefetch_write_revision,
                                          result->status == PP_CACHE_HIT || result->status == PP_CACHE_FULL);
        }
        return;
    }
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
        if (prefetch && pixels_verified()) {
            verified = true;
            prefetch_done(true);
            pp_picture_close();
            return;
        }
        if (!prefetch && show_complete())
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
        if (prefetch || !pp_net_connected()) {
            picture_error(result->status == PP_CACHE_NO_MEMORY
                              ? "本地图片内存暂不足，请稍后查看"
                              : "本地图片读取忙，请稍后重试");
            return;
        }
    }
    request_remote(SIZE_MAX);
}
bool pp_picture_receive(const cJSON *j) {
    const char *type = string(j, "type");
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
    if (!strcmp(type, "error") && (!strcmp(string(j, "operation"), "image.open") ||
                                   !strcmp(string(j, "operation"), "image.chunk"))) {
        if (remote_active) {
            close_remote();
            release_pixels();
            pending = PICTURE_WAITING;
            if (!start_hd())
                picture_error("图片预览暂不可用");
        }
        return true;
    }
    if (!strcmp(type, "image.open")) {
        if (!loading || !remote_active || strcmp(string(j, "message_id"), message_id) ||
            strcmp(string(j, "image_id"), image_id))
            return false;
        if (pixels || pending != PICTURE_WAITING ||
            !pp_image_begin(&transfer, string(j, "format"), string(j, "version"),
                            number(j, "width"), number(j, "height"), number(j, "bytes"),
                            requested_width, requested_height)) {
            picture_error("图片格式或尺寸不符合要求");
        } else
            pending = PICTURE_ALLOCATE;
        return true;
    }
    if (strcmp(type, "image.chunk") || !remote_active || !loading || !pixels ||
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
    if (!loading) {
        if (shown)
            start_hd();
        return false;
    }
    bool background = prefetch;
    if (now - started > 20000) {
        if (stream.state != STREAM_OFF) {
            stream_failed("高清图片读取超时，可重试");
            return !pp_picture_active();
        }
        if (remote_active && start_hd()) {
            release_pixels();
            pending = PICTURE_WAITING;
            return false;
        }
        picture_error("图片读取超时，可翻页后重新查看");
        return !background;
    }
    if (stream.state != STREAM_OFF) {
        stream_poll();
        return !pp_picture_active();
    }
    /* Dispatch copied the metadata. The receive frame and JSON tree have now
     * been released, so neither counts against the image's allocation. */
    if (pending == PICTURE_REQUEST) {
        pending = PICTURE_WAITING;
        if (!remote_open(request_limit) && !start_hd())
            pp_picture_close();
    } else if (pending == PICTURE_ALLOCATE) {
        pending = PICTURE_WAITING;
        size_t available = heap_caps_get_free_size(MALLOC_CAP_8BIT);
        if (!prefetch && !pp_image_has_headroom(available, transfer.bytes, false)) {
            pp_memory_reclaim(PP_CACHE_RESIDENT_BYTES, PP_MEMORY_NORMAL);
            available = heap_caps_get_free_size(MALLOC_CAP_8BIT);
        }
        if (pp_image_has_headroom(available, transfer.bytes, prefetch) &&
            pp_buffer_reserve(&owned_pixels, transfer.bytes,
                              prefetch ? PP_MEMORY_BACKGROUND : PP_MEMORY_NORMAL, PP_MEMORY_IMAGE)) {
            owned_pixels.size = transfer.bytes;
            pixels = (unsigned char *)owned_pixels.data;
            next_chunk();
        } else if (prefetch)
            pp_picture_close();
        else
            request_remote(transfer.bytes - 1);
    }
    return !background && !pp_picture_active();
}

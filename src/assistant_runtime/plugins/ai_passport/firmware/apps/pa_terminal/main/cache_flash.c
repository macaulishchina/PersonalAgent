#include "cache_flash.h"
#include "cache_stream.h"
#include "esp_partition.h"
#include "sdkconfig.h"
#ifndef CONFIG_I2S_ISR_IRAM_SAFE
#error "Enable CONFIG_I2S_ISR_IRAM_SAFE for the PA-Terminal stream cache"
#endif
#include "esp_random.h"
#include "mbedtls/ccm.h"
#include "mbedtls/platform_util.h"

static const esp_partition_t *region;
static mbedtls_ccm_context cipher;
static bool keyed;
static unsigned origin, cursor, prepared;
static unsigned sectors(void) { return region->size / region->erase_size; }
static uint64_t serial;
/* Sectors whose entire contents were observed erased can be programmed
 * without stalling sampling for an unnecessary erase. */
static pp_stream_io_t prepare_sector(size_t address, bool may_erase) {
    uint32_t words[64];
    for (size_t n = 0; n < region->erase_size; n += sizeof(words)) {
        if (esp_partition_read(region, address + n, words, sizeof(words)) != ESP_OK)
            return PP_STREAM_IO_ERROR;
        for (unsigned i = 0; i < sizeof(words) / sizeof(*words); i++)
            if (words[i] != UINT32_MAX)
                return !may_erase ? PP_STREAM_NO_SPACE :
                       esp_partition_erase_range(region, address, region->erase_size) == ESP_OK ?
                       PP_STREAM_IO_OK : PP_STREAM_IO_ERROR;
    }
    return PP_STREAM_IO_OK;
}

/* Only the storage worker visits these sectors. Never start an erase from
 * the live stream path. Stop between sectors for audio, reconnect or sleep. */
bool pp_stream_flash_prepare(void) {
    if (!region || prepared == sectors())
        return false;
    unsigned sector = (cursor + prepared) % sectors();
    if (prepare_sector(sector * region->erase_size, true) != PP_STREAM_IO_OK)
        return false;
    prepared++;
    return true;
}

static void forget(void) {
    mbedtls_ccm_free(&cipher);
    keyed = false;
}

static bool new_key(void) {
    unsigned char key[16];
    esp_fill_random(key, sizeof(key));
    mbedtls_ccm_init(&cipher);
    keyed = mbedtls_ccm_setkey(&cipher, MBEDTLS_CIPHER_ID_AES, key, 128) == 0;
    mbedtls_platform_zeroize(key, sizeof(key));
    origin = cursor;
    serial = 0;
    return keyed;
}

/* The worker is the only caller. The lease key lives only in RAM. Page
 * sequence is authenticated, including when the physical ring wraps. */
static pp_stream_io_t transfer(bool write, unsigned session, unsigned page, void *data) {
    if (!keyed && (!write || !new_key()))
        return false;
    unsigned sector = (origin + page) % sectors();
    size_t address = sector * region->erase_size;
    struct { uint64_t nonce; unsigned char tag[16]; } header;
    uint32_t binding[2] = {session, page};
    if (write) {
        if (sector != cursor)
            return PP_STREAM_IO_ERROR;
        if (!prepared) {
            pp_stream_io_t status = prepare_sector(address, false);
            if (status != PP_STREAM_IO_OK)
                return status;
        }
        /* A failed or interrupted program may dirty the sector as well. */
        cursor = (cursor + 1) % sectors();
        if (prepared)
            prepared--;
        header.nonce = ++serial;
        return !mbedtls_ccm_encrypt_and_tag(&cipher, PP_STREAM_PAGE,
                    (void *)&header.nonce, sizeof(header.nonce), (void *)binding, sizeof(binding),
                    data, data, header.tag, sizeof(header.tag)) &&
               esp_partition_write(region, address, data, PP_STREAM_PAGE) == ESP_OK &&
               esp_partition_write(region, address + PP_STREAM_PAGE, &header, sizeof(header)) == ESP_OK;
    }
    return esp_partition_read(region, address + PP_STREAM_PAGE, &header, sizeof(header)) == ESP_OK &&
           esp_partition_read(region, address, data, PP_STREAM_PAGE) == ESP_OK &&
           !mbedtls_ccm_auth_decrypt(&cipher, PP_STREAM_PAGE, (void *)&header.nonce,
                    sizeof(header.nonce), (void *)binding, sizeof(binding),
                    data, data, header.tag, sizeof(header.tag));
}

bool pp_stream_flash_attach(void (*wake)(void)) {
    region = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, 0x40, "pp_stream");
    if (!region || region->address != 0x400000 || region->size != 0x300000 ||
        region->erase_size != 4096) {
        region = NULL;
        return false;
    }
    cursor = esp_random() % sectors();
    pp_stream_attach((pp_stream_disk_t){region->size / region->erase_size, wake, transfer, forget, true});
    return true;
}

/* Generate a synthetic LittleFS partition for simulator regression only.
 * With the firmware's pinned LittleFS, removing the first cache key from
 * this directory forces a metadata split during the next foreground write.
 * The values intentionally have no valid cache header. No device data is used.
 */
#include "lfs.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

enum { sector_size = 4096, sector_count = 128 };
static unsigned char flash[sector_size * sector_count * 2];

static unsigned char *address(lfs_block_t block, lfs_off_t offset, lfs_size_t size) {
    assert(block < sector_count * 2 && offset <= sector_size && size <= sector_size - offset);
    return flash + block * sector_size + offset;
}
static int read_block(const struct lfs_config *cfg, lfs_block_t block, lfs_off_t offset,
                      void *out, lfs_size_t size) {
    (void)cfg;
    memcpy(out, address(block, offset, size), size);
    return 0;
}
static int write_block(const struct lfs_config *cfg, lfs_block_t block, lfs_off_t offset,
                       const void *in, lfs_size_t size) {
    (void)cfg;
    memcpy(address(block, offset, size), in, size);
    return 0;
}
static int erase_block(const struct lfs_config *cfg, lfs_block_t block) {
    (void)cfg;
    memset(address(block, 0, sector_size), 0xff, sector_size);
    return 0;
}
static int sync_block(const struct lfs_config *cfg) {
    (void)cfg;
    return 0;
}
static void put(lfs_t *fs, unsigned index) {
    char key[65];
    unsigned char value[330] = {0};
    snprintf(key, sizeof(key), "%064x", index);
    lfs_file_t file;
    assert(lfs_file_open(fs, &file, key, LFS_O_CREAT | LFS_O_WRONLY | LFS_O_TRUNC) == 0);
    assert(lfs_file_write(fs, &file, value, sizeof(value)) == sizeof(value));
    assert(lfs_file_close(fs, &file) == 0);
}
int main(int argc, char **argv) {
    if (argc != 2)
        return 2;
    struct lfs_config cfg = {
        .read = read_block, .prog = write_block, .erase = erase_block, .sync = sync_block,
        .read_size = 128, .prog_size = 128, .block_size = sector_size, .block_count = sector_count,
        .block_cycles = 512, .cache_size = 128, .lookahead_size = 16,
    };
    memset(flash, 0xff, sizeof(flash));
    lfs_t fs;
    assert(lfs_format(&fs, &cfg) == 0 && lfs_mount(&fs, &cfg) == 0);
    char first[65];
    snprintf(first, sizeof(first), "%064x", 0u);
    for (unsigned i = 0; i < 32; i++) {
        put(&fs, i);
        if (i == 9) {
            for (uint32_t generation = 0; generation < 8; generation++)
                assert(lfs_setattr(&fs, first, 1, &generation, sizeof(generation)) == 0);
        }
    }
    assert(lfs_unmount(&fs) == 0);
    FILE *file = fopen(argv[1], "wb");
    if (!file)
        return 1;
    bool written = fwrite(flash, sector_size * sector_count, 1, file) == 1;
    if (fclose(file) || !written)
        return 1;
    /* USB copies the old volume to the lower half of the new partition.
     * Its original sectors remain in the upper half until LittleFS reuses them. */
    memcpy(flash + sector_size * sector_count, flash, sector_size * sector_count);
    cfg.block_count = 0;
    assert(lfs_mount(&fs, &cfg) == 0 && fs.block_count == sector_count);
    assert(lfs_fs_grow(&fs, 2 * sector_count) == 0 && lfs_unmount(&fs) == 0);
    assert(lfs_mount(&fs, &cfg) == 0 && fs.block_count == 2 * sector_count);
    for (unsigned i = 0; i < 32; i++) {
        char key[65];
        unsigned char data[330], expected[330] = {0};
        snprintf(key, sizeof(key), "%064x", i);
        lfs_file_t input;
        assert(lfs_file_open(&fs, &input, key, LFS_O_RDONLY) == 0);
        assert(lfs_file_read(&fs, &input, data, sizeof(data)) == sizeof(data));
        assert(!memcmp(data, expected, sizeof(data)) && lfs_file_close(&fs, &input) == 0);
    }
    uint32_t generation = 0;
    assert(lfs_getattr(&fs, first, 1, &generation, sizeof(generation)) == sizeof(generation));
    assert(generation == 7 && lfs_unmount(&fs) == 0);
    memcpy(flash, flash + sector_size * sector_count, sector_count * sector_size);
    cfg.block_count = sector_count;
    /* Removing one file normally frees space. Here a directory split adds
     * a metadata pair. Check the saved starting point still exercises it. */
    assert(lfs_mount(&fs, &cfg) == 0);
    lfs_ssize_t before = lfs_fs_size(&fs);
    assert(before > 0 && lfs_remove(&fs, first) == 0);
    assert(lfs_fs_size(&fs) > before);
    assert(lfs_unmount(&fs) == 0);
    return 0;
}

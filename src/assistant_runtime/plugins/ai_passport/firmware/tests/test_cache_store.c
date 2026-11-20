#define _POSIX_C_SOURCE 200809L
#include "cache_store.h"
#include <assert.h>
#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void key(unsigned n, char value[65]) { snprintf(value, 65, "%064x", n); }
static size_t used(const char *root) {
    DIR *dir = opendir(root);
    assert(dir);
    size_t result = 0;
    char path[192];
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        if (strlen(entry->d_name) != 64)
            continue;
        assert(strlen(root) < 96);
        snprintf(path, sizeof(path), "%.95s/%.64s", root, entry->d_name);
        struct stat info;
        assert(!stat(path, &info));
        result += (info.st_size + 4095u) / 4096u * 4096u;
    }
    closedir(dir);
    return result;
}
static bool before_pause(void *staged_path) {
    return access(staged_path, F_OK) != 0;
}
static bool reserve(pp_buffer_t *buffer, size_t bytes, void *limit) {
    return (!limit || bytes <= *(size_t *)limit) &&
           pp_buffer_reserve(buffer, bytes, PP_MEMORY_NORMAL, PP_MEMORY_CACHE);
}
static pp_cache_status_t read_value(pp_cache_store_t *store, const char *key, pp_cache_value_t *value) {
    pp_cache_status_t status = pp_cache_store_read(store, key, value, reserve, NULL);
    if (status != PP_CACHE_HIT)
        pp_buffer_free(&value->bytes);
    return status;
}
int main(void) {
    char root[] = "/tmp/pp-cache-XXXXXX";
    assert(mkdtemp(root));
    pp_cache_store_t store;
    assert(pp_cache_store_open(&store, root, 8192));
    const unsigned char scope[16] = {1}, other[16] = {2};
    const unsigned char pinned[][16] = {{3}};
    const unsigned char old[16] = {4};
    pp_cache_store_scope(&store, scope);
    char name[65], pinned_key[65], path[192];
    key(1, pinned_key);
    char data[600];
    memset(data, 'x', sizeof(data));
    assert(pp_cache_store_put(&store, pinned_key, pinned[0], false, data, sizeof(data), "aux", 4));
    uint64_t once = store.sequence;
    assert(pp_cache_store_put(&store, pinned_key, pinned[0], false, data, sizeof(data), "aux", 4));
    assert(store.used == used(root));
    assert(store.sequence == once); /* Identical reconnects do not wear Flash. */
    pp_cache_value_t value;
    assert(PP_CACHE_HIT == read_value(&store, pinned_key, &value));
    assert(value.bytes.size == sizeof(data) && value.aux_size == 4 && !memcmp(value.aux, "aux", 4));
    pp_buffer_free(&value.bytes);
    assert(PP_CACHE_NO_MEMORY == pp_cache_store_read(&store, pinned_key, &value, reserve, &(size_t){100}));
    assert(!value.bytes.data);
    key(2, name);
    assert(PP_CACHE_MISSING == read_value(&store, name, &value));
    /* Namespace isolation applies even to a caller using the same storage key. */
    pp_cache_store_scope(&store, other);
    assert(PP_CACHE_MISSING == read_value(&store, pinned_key, &value));
    pp_cache_store_scope(&store, scope);
    /* A torn staged write cannot replace the last committed value. */
    snprintf(path, sizeof(path), "%s/.pending", root);
    FILE *file = fopen(path, "wb");
    assert(file);
    fputs("partial write", file);
    fclose(file);
    pp_cache_store_t reopened;
    assert(pp_cache_store_open(&reopened, root, 8192));
    assert(pp_cache_store_recover(&reopened));
    assert(reopened.used == used(root));
    pp_cache_store_scope(&reopened, scope);
    assert(PP_CACHE_HIT == read_value(&reopened, pinned_key, &value));
    pp_buffer_free(&value.bytes);
    assert(access(path, F_OK));
    /* Deterministic I/O failure leaves the old committed body readable. */
    assert(!mkdir(path, 0700));
    assert(
        !pp_cache_store_put(&reopened, pinned_key, pinned[0], false, "replacement", 11, NULL, 0));
    assert(PP_CACHE_HIT == read_value(&reopened, pinned_key, &value));
    pp_buffer_free(&value.bytes);
    assert(!rmdir(path));
    /* Denying further work during a staged write keeps the committed version. */
    reopened.allowed = before_pause;
    reopened.context = path;
    char replacement[1024];
    memset(replacement, 'y', sizeof(replacement));
    assert(pp_cache_store_write(&reopened, pinned_key, pinned[0], false, replacement,
                                  sizeof(replacement), NULL, 0, 0) == PP_CACHE_BUSY);
    reopened.allowed = NULL;
    assert(read_value(&reopened, pinned_key, &value) == PP_CACHE_HIT);
    assert(value.bytes.size == sizeof(data) && value.bytes.data[0] == 'x');
    pp_buffer_free(&value.bytes);
    /* Corruption must not be delivered; an identical original repairs it. */
    snprintf(path, sizeof(path), "%s/%s", root, pinned_key);
    file = fopen(path, "r+b");
    assert(file && !fseek(file, -1, SEEK_END));
    assert(fputc('!', file) != EOF);
    fclose(file);
    assert(PP_CACHE_CORRUPT == read_value(&reopened, pinned_key, &value));
    assert(
        pp_cache_store_put(&reopened, pinned_key, pinned[0], false, data, sizeof(data), "aux", 4));
    assert(PP_CACHE_HIT == read_value(&reopened, pinned_key, &value));
    pp_buffer_free(&value.bytes);
    char invalid[65] = "../../escape";
    assert(!pp_cache_store_put(&reopened, invalid, old, false, data, sizeof(data), NULL, 0));
    assert(!pp_cache_store_put(&reopened, pinned_key, old, false, data, PP_CACHE_VALUE_MAX + 1,
                               NULL, 0));
    /* A single turn cannot discard its own first page to grow past budget. */
    assert(pp_cache_store_clear(&reopened));
    for (unsigned i = 1; i <= 2; i++) {
        key(i, name);
        assert(pp_cache_store_put(&reopened, name, pinned[0], false, data, sizeof(data), NULL, 0));
    }
    key(99, name);
    assert(pp_cache_store_write(&reopened, name, pinned[0], PP_HISTORY_PART, data, sizeof(data), NULL, 0,
                                0) == PP_CACHE_FULL);
    assert(used(root) == 8192);
    assert(pp_cache_store_clear(&reopened));
    assert(pp_cache_store_open(&reopened, root, 65536));
    pp_cache_store_scope(&reopened, scope);
    /* Four turns with a prompt, reply, continuation and preview apiece.
     * Interrupt deletion and reopen before allowing the incoming fifth turn. */
    for (unsigned turn = 1; turn <= 4; turn++) {
        unsigned char owner[16] = {turn};
        for (unsigned part = 0; part < 4; part++) {
            key(10 * turn + part, name);
            assert(pp_cache_store_write(&reopened, name, owner,
                                        part < 2 ? PP_HISTORY_HEAD : PP_HISTORY_PART, data, sizeof(data),
                                        NULL, 0, 0) == PP_CACHE_HIT);
        }
    }
    key(50, name);
    snprintf(path, sizeof(path), "%s/.prune", root);
    reopened.allowed = before_pause;
    reopened.context = path;
    assert(pp_cache_store_write(&reopened, name, (unsigned char[16]){5}, PP_HISTORY_HEAD, data,
                                sizeof(data), NULL, 0, 0) == PP_CACHE_BUSY);
    assert(access(path, F_OK) == 0);
    assert(pp_cache_store_open(&reopened, root, 65536));
    assert(pp_cache_store_recover(&reopened) && reopened.used == 8 * 4096);
    pp_cache_store_scope(&reopened, scope);
    assert(pp_cache_store_write(&reopened, name, (unsigned char[16]){5}, PP_HISTORY_HEAD, data,
                                sizeof(data), NULL, 0, 0) == PP_CACHE_HIT);
    for (unsigned turn = 1; turn <= 4; turn++) {
        for (unsigned part = 0; part < 4; part++) {
            key(10 * turn + part, name);
            assert(read_value(&reopened, name, &value) == (turn <= 2 ? PP_CACHE_MISSING : PP_CACHE_HIT));
            pp_buffer_free(&value.bytes);
        }
    }
    /* Adopting an old local head after a live write cannot overwrite or reorder it. */
    key(50, name);
    assert(pp_cache_store_write_ordered(&reopened, name, (unsigned char[16]){5}, PP_HISTORY_HEAD, "old", 3,
                                        NULL, 0, 0, 1) == PP_CACHE_HIT);
    assert(read_value(&reopened, name, &value) == PP_CACHE_HIT && value.bytes.size == sizeof(data));
    pp_buffer_free(&value.bytes);
    char expected[65];
    key(50, expected);
    assert(pp_cache_store_seek(&reopened, name, PP_SEEK_LATEST, NULL) == PP_CACHE_HIT && !strcmp(name, expected));
    uint64_t cursor = 0;
    key(41, expected);
    assert(pp_cache_store_seek(&reopened, name, PP_SEEK_PREVIOUS, &cursor) == PP_CACHE_HIT &&
           !strcmp(name, expected));
    /* A selected record whose body cannot supply an ID remains navigable. */
    assert(cursor > 0);
    strcpy(name, "");
    key(50, expected);
    assert(pp_cache_store_seek(&reopened, name, PP_SEEK_NEXT, &cursor) == PP_CACHE_HIT && !strcmp(name, expected));
    pp_cache_store_scope(&reopened, other);
    assert(pp_cache_store_seek(&reopened, name, PP_SEEK_LATEST, NULL) == PP_CACHE_MISSING);
    assert(pp_cache_store_clear(&reopened));
    assert(!rmdir(root));
    return 0;
}

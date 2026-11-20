#include "cache_store.h"
#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CACHE_MAGIC 0x50434331u
typedef struct {
    uint32_t magic, version;
    uint64_t sequence;
    uint32_t size, aux_size, data_crc, index;
    unsigned char scope[16], owner[16], aux[PP_CACHE_AUX_MAX];
    uint32_t header_crc;
} header_t;

static uint32_t crc_step(uint32_t result, const void *data, size_t bytes) {
    const unsigned char *p = data;
    for (size_t i = 0; i < bytes; i++) {
        result ^= p[i];
        for (unsigned b = 0; b < 8; b++)
            result = (result >> 1) ^ (0xedb88320u & (0u - (result & 1u)));
    }
    return result;
}
static uint32_t crc(const void *data, size_t bytes) { return ~crc_step(UINT32_MAX, data, bytes); }
static bool allowed(const pp_cache_store_t *store) {
    return !store->allowed || store->allowed(store->context);
}
static bool valid_body(const pp_cache_store_t *s, FILE *file, const header_t *h) {
    unsigned char block[256];
    size_t remaining = h->size;
    uint32_t sum = UINT32_MAX;
    while (remaining) {
        size_t bytes = remaining < sizeof(block) ? remaining : sizeof(block);
        if (!allowed(s) || fread(block, 1, bytes, file) != bytes)
            return false;
        sum = crc_step(sum, block, bytes);
        remaining -= bytes;
    }
    return ~sum == h->data_crc;
}
static bool key_valid(const char *key) {
    if (!key || strlen(key) != 64)
        return false;
    for (unsigned i = 0; i < 64; i++)
        if (!((key[i] >= '0' && key[i] <= '9') || (key[i] >= 'a' && key[i] <= 'f')))
            return false;
    return true;
}
static bool path_for(const pp_cache_store_t *s, const char *name, char path[192]) {
    return snprintf(path, 192, "%s/%s", s->root, name) < 192;
}
static bool header(FILE *file, header_t *h) {
    if (fread(h, sizeof(*h), 1, file) != 1 || h->magic != CACHE_MAGIC || h->version != 1 ||
        !h->sequence || h->size > PP_CACHE_VALUE_MAX || h->aux_size > PP_CACHE_AUX_MAX ||
        h->index > 1 || h->header_crc != crc(h, offsetof(header_t, header_crc)))
        return false;
    long position = ftell(file);
    return position >= 0 && fseek(file, 0, SEEK_END) == 0 &&
           ftell(file) == (long)(sizeof(*h) + h->size) && fseek(file, position, SEEK_SET) == 0;
}
bool pp_cache_store_open(pp_cache_store_t *s, const char *root, size_t budget) {
    if (!s || !root || strlen(root) >= sizeof(s->root) || budget < 4096 || budget > PP_CACHE_BUDGET)
        return false;
    memset(s, 0, sizeof(*s));
    strcpy(s->root, root);
    s->budget = budget;
    return true;
}
static size_t charge(size_t bytes) { return (bytes + 4095u) / 4096u * 4096u; }
bool pp_cache_store_recover(pp_cache_store_t *s) {
    if (!allowed(s))
        return false;
    DIR *dir = opendir(s->root);
    if (!dir)
        return false;
    size_t used = 0;
    char path[192];
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        if (!key_valid(entry->d_name) || !path_for(s, entry->d_name, path))
            continue;
        if (!allowed(s)) {
            closedir(dir);
            return false;
        }
        FILE *file = fopen(path, "rb");
        header_t h;
        bool valid = file && header(file, &h);
        bool io_error = !file || ferror(file);
        if (file)
            fclose(file);
        if (io_error) {
            closedir(dir);
            return false;
        }
        if (valid)
            used += charge(sizeof(h) + h.size);
        if (valid && h.sequence > s->sequence)
            s->sequence = h.sequence;
    }
    closedir(dir);
    if (!allowed(s))
        return false;
    if (path_for(s, ".pending", path) && unlink(path) && errno != ENOENT)
        return false;
    s->used = used;
    return true;
}
void pp_cache_store_scope(pp_cache_store_t *s, const unsigned char scope[16],
                          const unsigned char pins[][16], unsigned count) {
    memcpy(s->scope, scope, sizeof(s->scope));
    s->pin_count = count > PP_CACHE_PIN_LIMIT ? PP_CACHE_PIN_LIMIT : count;
    if (s->pin_count)
        memcpy(s->pins, pins, s->pin_count * 16);
}
pp_cache_status_t pp_cache_store_read(pp_cache_store_t *s, const char key[65],
                                     pp_cache_value_t *v,
                                     bool (*reserve)(pp_buffer_t *, size_t, void *), void *context) {
    char path[192];
    *v = (pp_cache_value_t){0};
    if (!allowed(s))
        return PP_CACHE_BUSY;
    if (!key_valid(key) || !path_for(s, key, path))
        return PP_CACHE_IO_ERROR;
    FILE *file = fopen(path, "rb");
    if (!file)
        return errno == ENOENT ? PP_CACHE_MISSING : PP_CACHE_IO_ERROR;
    header_t h;
    pp_cache_status_t result = PP_CACHE_HIT;
    if (!header(file, &h))
        result = ferror(file) ? PP_CACHE_IO_ERROR : PP_CACHE_CORRUPT;
    else if (memcmp(h.scope, s->scope, sizeof(h.scope)))
        result = PP_CACHE_MISSING;
    else if (!allowed(s))
        result = PP_CACHE_BUSY;
    else if (!reserve(&v->bytes, h.size ? h.size : 1, context))
        result = PP_CACHE_NO_MEMORY;
    else if (fread(v->bytes.data, 1, h.size, file) != h.size || crc(v->bytes.data, h.size) != h.data_crc)
        result = ferror(file) ? PP_CACHE_IO_ERROR : PP_CACHE_CORRUPT;
    fclose(file);
    if (result == PP_CACHE_HIT) {
        v->bytes.size = h.size;
        v->aux_size = h.aux_size;
        memcpy(v->aux, h.aux, h.aux_size);
    }
    /* The caller releases any allocation even after a failed body read. */
    return result;
}
static bool protected(const pp_cache_store_t *s, const header_t *h) {
    if (memcmp(s->scope, h->scope, sizeof(h->scope)))
        return false;
    if (h->index)
        return true;
    for (unsigned i = 0; i < s->pin_count; i++)
        if (!memcmp(s->pins[i], h->owner, 16))
            return true;
    return false;
}
static pp_cache_status_t room(pp_cache_store_t *s, const char *replaced, size_t bytes, size_t limit) {
    if (charge(bytes) > limit)
        return PP_CACHE_FULL;
    for (unsigned attempt = 0; attempt <= PP_CACHE_OBJECTS; attempt++) {
        if (!allowed(s))
            return PP_CACHE_BUSY;
        DIR *dir = opendir(s->root);
        if (!dir)
            return PP_CACHE_IO_ERROR;
        size_t used = 0;
        unsigned count = 0;
        bool replacing = false;
        size_t victim_bytes = 0;
        uint64_t oldest = UINT64_MAX;
        char victim[65] = "", path[192];
        struct dirent *entry;
        while ((entry = readdir(dir))) {
            if (!key_valid(entry->d_name) || !path_for(s, entry->d_name, path))
                continue;
            if (!allowed(s)) {
                closedir(dir);
                return PP_CACHE_BUSY;
            }
            FILE *file = fopen(path, "rb");
            header_t h;
            bool valid = file && header(file, &h);
            bool io_error = !file || ferror(file);
            if (file)
                fclose(file);
            if (io_error) {
                closedir(dir);
                return PP_CACHE_IO_ERROR;
            }
            if (!valid) {
                if (allowed(s))
                    unlink(path);
                continue;
            }
            used += charge(sizeof(h) + h.size);
            count++;
            replacing = replacing || !strcmp(replaced, entry->d_name);
            if (strcmp(replaced, entry->d_name) && !protected(s, &h) && h.sequence < oldest) {
                strcpy(victim, entry->d_name);
                oldest = h.sequence;
                victim_bytes = charge(sizeof(h) + h.size);
            }
        }
        closedir(dir);
        s->used = used;
        /* Include the staged replacement too. Half of the physical partition
         * stays outside the data budget for filesystem metadata and COW. */
        if (used + charge(bytes) <= limit &&
            (count < PP_CACHE_OBJECTS || (replacing && count == PP_CACHE_OBJECTS)))
            return PP_CACHE_HIT;
        if (!allowed(s))
            return PP_CACHE_BUSY;
        if (!victim[0])
            return PP_CACHE_FULL;
        if (!path_for(s, victim, path) || unlink(path))
            return PP_CACHE_IO_ERROR;
        s->used -= victim_bytes;
    }
    return PP_CACHE_FULL;
}
pp_cache_status_t pp_cache_store_write(pp_cache_store_t *s, const char key[65], const unsigned char owner[16],
                        bool index, const void *data, size_t size, const void *aux,
                        size_t aux_size, size_t reserve) {
    if (!key_valid(key) || !data || size > PP_CACHE_VALUE_MAX || aux_size > PP_CACHE_AUX_MAX ||
        (aux_size && !aux) || s->sequence == UINT64_MAX)
        return PP_CACHE_IO_ERROR;
    if (!allowed(s))
        return PP_CACHE_BUSY;
    char path[192], staged[192];
    if (!path_for(s, key, path) || !path_for(s, ".pending", staged))
        return PP_CACHE_IO_ERROR;
    header_t h = {.magic = CACHE_MAGIC,
                  .version = 1,
                  .sequence = s->sequence + 1,
                  .size = size,
                  .aux_size = aux_size,
                  .data_crc = crc(data, size),
                  .index = index};
    memcpy(h.scope, s->scope, sizeof(h.scope));
    memcpy(h.owner, owner, sizeof(h.owner));
    if (aux_size)
        memcpy(h.aux, aux, aux_size);
    size_t prior_bytes = 0;
    FILE *old = fopen(path, "rb");
    if (old) {
        header_t prior;
        bool valid = header(old, &prior);
        if (valid)
            prior_bytes = charge(sizeof(prior) + prior.size);
        bool same = valid && prior.size == size && prior.data_crc == h.data_crc &&
                    prior.aux_size == aux_size && !memcmp(prior.scope, h.scope, sizeof(h.scope)) &&
                    !memcmp(prior.aux, h.aux, aux_size);
        same = same && valid_body(s, old, &prior);
        fclose(old);
        /* No duplicate pixel allocation just to avoid an identical Flash write. */
        if (same)
            return PP_CACHE_HIT;
    }
    size_t limit = reserve < s->budget ? s->budget - reserve : 0;
    pp_cache_status_t capacity = room(s, key, sizeof(h) + size, limit);
    if (capacity != PP_CACHE_HIT)
        return capacity;
    if (!allowed(s))
        return PP_CACHE_BUSY;
    h.header_crc = crc(&h, offsetof(header_t, header_crc));
    FILE *file = fopen(staged, "wb");
    if (!file)
        return PP_CACHE_IO_ERROR;
    bool ok = fwrite(&h, sizeof(h), 1, file) == 1;
    for (size_t offset = 0; ok && offset < size;) {
        size_t chunk = size - offset > 512 ? 512 : size - offset;
        ok = allowed(s) && fwrite((const char *)data + offset, 1, chunk, file) == chunk;
        offset += chunk;
    }
    ok = ok && allowed(s) && fflush(file) == 0 && allowed(s) && fsync(fileno(file)) == 0;
    if (fclose(file))
        ok = false;
    bool commit = allowed(s);
    if (ok && commit)
        ok = rename(staged, path) == 0;
    else
        ok = false;
    if (!ok)
        unlink(staged);
    else {
        s->sequence = h.sequence;
        s->used = s->used - prior_bytes + charge(sizeof(h) + size);
    }
    return ok ? PP_CACHE_HIT : (commit ? PP_CACHE_IO_ERROR : PP_CACHE_BUSY);
}
bool pp_cache_store_put(pp_cache_store_t *s, const char key[65], const unsigned char owner[16],
                        bool index, const void *data, size_t size, const void *aux, size_t aux_size) {
    return pp_cache_store_write(s, key, owner, index, data, size, aux, aux_size, 0) == PP_CACHE_HIT;
}
bool pp_cache_store_clear(pp_cache_store_t *s) {
    DIR *dir = opendir(s->root);
    if (!dir)
        return false;
    bool ok = true;
    char path[192];
    struct dirent *entry;
    while ((entry = readdir(dir)))
        if ((key_valid(entry->d_name) || !strcmp(entry->d_name, ".pending")) &&
            path_for(s, entry->d_name, path) && unlink(path))
            ok = false;
    closedir(dir);
    if (ok)
        s->used = 0;
    return ok;
}

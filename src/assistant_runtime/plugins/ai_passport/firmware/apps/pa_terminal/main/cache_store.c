#include "cache_store.h"
#include <dirent.h>
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
static bool valid_body(FILE *file, const header_t *h) {
    unsigned char block[256];
    size_t remaining = h->size;
    uint32_t sum = UINT32_MAX;
    while (remaining) {
        size_t bytes = remaining < sizeof(block) ? remaining : sizeof(block);
        if (fread(block, 1, bytes, file) != bytes)
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
    DIR *dir = opendir(root);
    if (!dir)
        return false;
    char path[192];
    struct dirent *entry;
    while ((entry = readdir(dir))) {
        if (!key_valid(entry->d_name) || !path_for(s, entry->d_name, path))
            continue;
        FILE *file = fopen(path, "rb");
        header_t h;
        bool valid = file && header(file, &h);
        if (file)
            fclose(file);
        if (!valid)
            unlink(path);
        else if (h.sequence > s->sequence)
            s->sequence = h.sequence;
    }
    closedir(dir);
    if (path_for(s, ".pending", path))
        unlink(path);
    return true;
}
void pp_cache_store_scope(pp_cache_store_t *s, const unsigned char scope[16],
                          const unsigned char pins[][16], unsigned count) {
    memcpy(s->scope, scope, sizeof(s->scope));
    s->pin_count = count > PP_CACHE_RECENT ? PP_CACHE_RECENT : count;
    if (s->pin_count)
        memcpy(s->pins, pins, s->pin_count * 16);
}
void pp_cache_value_free(pp_cache_value_t *v) {
    free(v->data);
    memset(v, 0, sizeof(*v));
}
bool pp_cache_store_get_limit(pp_cache_store_t *s, const char key[65], size_t limit,
                              pp_cache_value_t *v) {
    char path[192];
    memset(v, 0, sizeof(*v));
    if (!key_valid(key) || !path_for(s, key, path))
        return false;
    FILE *file = fopen(path, "rb");
    if (!file)
        return false;
    header_t h;
    bool valid = header(file, &h) && h.size <= limit && !memcmp(h.scope, s->scope, sizeof(h.scope));
    if (valid) {
        v->data = malloc(h.size + 1);
        valid = v->data && fread(v->data, 1, h.size, file) == h.size &&
                crc(v->data, h.size) == h.data_crc;
    }
    fclose(file);
    if (!valid) {
        pp_cache_value_free(v);
        return false; /* Allocation failure must not destroy useful data. */
    }
    ((char *)v->data)[h.size] = 0;
    v->size = h.size;
    v->aux_size = h.aux_size;
    memcpy(v->aux, h.aux, h.aux_size);
    return true;
}
bool pp_cache_store_get(pp_cache_store_t *s, const char key[65], pp_cache_value_t *v) {
    return pp_cache_store_get_limit(s, key, PP_CACHE_VALUE_MAX, v);
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
static size_t charge(size_t bytes) { return (bytes + 4095u) / 4096u * 4096u; }
static bool room(pp_cache_store_t *s, const char *replaced, size_t bytes, size_t limit) {
    for (unsigned attempt = 0; attempt <= PP_CACHE_OBJECTS; attempt++) {
        DIR *dir = opendir(s->root);
        if (!dir)
            return false;
        size_t used = 0;
        unsigned count = 0;
        bool replacing = false;
        uint64_t oldest = UINT64_MAX;
        char victim[65] = "", path[192];
        struct dirent *entry;
        while ((entry = readdir(dir))) {
            if (!key_valid(entry->d_name) || !path_for(s, entry->d_name, path))
                continue;
            FILE *file = fopen(path, "rb");
            header_t h;
            bool valid = file && header(file, &h);
            if (file)
                fclose(file);
            if (!valid) {
                unlink(path);
                continue;
            }
            used += charge(sizeof(h) + h.size);
            count++;
            replacing = replacing || !strcmp(replaced, entry->d_name);
            if (strcmp(replaced, entry->d_name) && !protected(s, &h) && h.sequence < oldest) {
                strcpy(victim, entry->d_name);
                oldest = h.sequence;
            }
        }
        closedir(dir);
        /* Include the staged replacement too. Half of the physical partition
         * stays outside the data budget for filesystem metadata and COW. */
        if (used + charge(bytes) <= limit &&
            (count < PP_CACHE_OBJECTS || (replacing && count == PP_CACHE_OBJECTS)))
            return true;
        if (!victim[0] || !path_for(s, victim, path) || unlink(path))
            return false;
    }
    return false;
}
bool pp_cache_store_put(pp_cache_store_t *s, const char key[65], const unsigned char owner[16],
                        bool index, const void *data, size_t size, const void *aux,
                        size_t aux_size) {
    if (!key_valid(key) || !data || size > PP_CACHE_VALUE_MAX || aux_size > PP_CACHE_AUX_MAX ||
        (aux_size && !aux) || s->sequence == UINT64_MAX)
        return false;
    char path[192], staged[192];
    if (!path_for(s, key, path) || !path_for(s, ".pending", staged))
        return false;
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
    FILE *old = fopen(path, "rb");
    if (old) {
        header_t prior;
        bool same = header(old, &prior) && prior.size == size && prior.data_crc == h.data_crc &&
                    prior.aux_size == aux_size && !memcmp(prior.scope, h.scope, sizeof(h.scope)) &&
                    !memcmp(prior.aux, h.aux, aux_size);
        same = same && valid_body(old, &prior);
        fclose(old);
        /* No duplicate pixel allocation just to avoid an identical Flash write. */
        if (same)
            return true;
    }
    /* Small LittleFS files may still occupy one whole Flash block. Charge
     * conservatively, not merely by payload length, and reserve previews space
     * so a long text reply cannot consume the entire offline image allowance. */
    size_t limit = s->budget;
    if (!index && !aux_size && limit >= 65536)
        limit -= 32768;
    if (!room(s, key, sizeof(h) + size, limit))
        return false;
    h.header_crc = crc(&h, offsetof(header_t, header_crc));
    FILE *file = fopen(staged, "wb");
    if (!file)
        return false;
    bool ok = fwrite(&h, sizeof(h), 1, file) == 1 && fwrite(data, 1, size, file) == size &&
              fflush(file) == 0 && fsync(fileno(file)) == 0;
    if (fclose(file))
        ok = false;
    if (ok)
        ok = rename(staged, path) == 0;
    if (!ok)
        unlink(staged);
    else
        s->sequence = h.sequence;
    return ok;
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
    return ok;
}

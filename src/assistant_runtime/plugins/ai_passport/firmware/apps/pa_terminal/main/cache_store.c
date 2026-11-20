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
    if (fread(h, sizeof(*h), 1, file) != 1 || h->magic != CACHE_MAGIC || h->version != 1 || !h->sequence ||
        h->size > PP_CACHE_VALUE_MAX || h->aux_size > PP_CACHE_AUX_MAX || h->index > PP_HISTORY_PART ||
        h->header_crc != crc(h, offsetof(header_t, header_crc)))
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
typedef struct {
    unsigned char scope[16], owner[16];
    uint64_t first;
} group_t;

static bool same_group(const group_t *group, const header_t *h) {
    return !memcmp(group->scope, h->scope, 16) && !memcmp(group->owner, h->owner, 16);
}
static bool read_header(const char *path, header_t *h) {
    FILE *file = fopen(path, "rb");
    bool ok = file && header(file, h);
    if (file) {
        bool corrupt = !ok && !ferror(file);
        fclose(file);
        if (corrupt)
            errno = EBADMSG;
    }
    return ok;
}
static uint32_t key_tag(const char *key) {
    uint32_t tag = 2166136261u;
    for (unsigned i = 0; i < 64; i++)
        tag = (tag ^ (unsigned char)key[i]) * 16777619u;
    return tag;
}
static void index_head(pp_cache_store_t *s, const char *key, const header_t *h, bool remove) {
    if (!s->indexed || s->collision || h->index != PP_HISTORY_HEAD ||
        memcmp(s->scope, h->scope, sizeof(s->scope)))
        return;
    if (h->sequence > UINT32_MAX) {
        s->collision = true; /* Very old volumes can still use the full-width scan. */
        return;
    }
    uint32_t tag = key_tag(key);
    for (unsigned i = 0; i < s->heads; i++) {
        if (s->head_tag[i] != tag)
            continue;
        if (s->head_order[i] != h->sequence) {
            s->collision = true;
        } else if (remove) {
            s->heads--;
            s->head_order[i] = s->head_order[s->heads];
            s->head_tag[i] = s->head_tag[s->heads];
        }
        return;
    }
    if (!remove && s->heads < PP_CACHE_OBJECTS) {
        s->head_order[s->heads] = h->sequence;
        s->head_tag[s->heads++] = tag;
    } else if (!remove) {
        s->collision = true;
    }
}
/* Finish exactly the groups selected before the first deletion, including
 * after loss of power. Repeating unlink on the remaining members is safe. */
static bool finish_prune(pp_cache_store_t *s) {
    char plan[192], path[192];
    if (!path_for(s, ".prune", plan))
        return false;
    FILE *file = fopen(plan, "rb");
    if (!file)
        return errno == ENOENT;
    if (fseek(file, 0, SEEK_END)) {
        fclose(file);
        return false;
    }
    long bytes = ftell(file);
    if (bytes <= 0 || bytes > (long)(PP_CACHE_OBJECTS * sizeof(group_t)) ||
        (size_t)bytes % sizeof(group_t) || fseek(file, 0, SEEK_SET)) {
        fclose(file);
        return false;
    }
    size_t count = (size_t)bytes / sizeof(group_t);
    group_t *groups = malloc((size_t)bytes);
    bool ok = groups && fread(groups, sizeof(group_t), count, file) == count;
    fclose(file);
    DIR *dir = ok ? opendir(s->root) : NULL;
    ok = dir != NULL;
    struct dirent *entry;
    while (ok && (entry = readdir(dir))) {
        if (!key_valid(entry->d_name))
            continue;
        header_t h;
        ok = allowed(s) && path_for(s, entry->d_name, path);
        if (!ok)
            break;
        if (!read_header(path, &h)) {
            ok = errno == EBADMSG && !unlink(path);
            continue;
        }
        for (size_t i = 0; i < count; i++) {
            if (!same_group(&groups[i], &h))
                continue;
            ok = unlink(path) == 0;
            size_t n = charge(sizeof(h) + h.size);
            if (ok)
                index_head(s, entry->d_name, &h, true);
            if (ok && s->used >= n)
                s->used -= n;
            break;
        }
    }
    if (dir)
        closedir(dir);
    free(groups);
    return ok && unlink(plan) == 0;
}
static int group_order(const void *a, const void *b) {
    uint64_t x = ((const group_t *)a)->first, y = ((const group_t *)b)->first;
    return (x > y) - (x < y);
}
static pp_cache_status_t prune_half(pp_cache_store_t *s, const unsigned char keep[16]) {
    group_t *groups = calloc(PP_CACHE_OBJECTS, sizeof(*groups));
    if (!groups)
        return PP_CACHE_NO_MEMORY;
    char path[192], plan[192];
    DIR *dir = opendir(s->root);
    struct dirent *entry;
    unsigned count = 0;
    bool ok = dir != NULL;
    while (ok && (entry = readdir(dir))) {
        if (!key_valid(entry->d_name))
            continue;
        header_t h;
        ok = allowed(s) && path_for(s, entry->d_name, path);
        if (!ok)
            break;
        if (!read_header(path, &h)) {
            ok = errno == EBADMSG && !unlink(path);
            continue;
        }
        if (!memcmp(s->scope, h.scope, 16) && !memcmp(keep, h.owner, 16))
            continue;
        unsigned i = 0;
        while (i < count && !same_group(&groups[i], &h))
            i++;
        if (i == count) {
            if (count == PP_CACHE_OBJECTS) {
                ok = false;
                break;
            }
            memcpy(groups[i].scope, h.scope, 16);
            memcpy(groups[i].owner, h.owner, 16);
            groups[count++].first = h.sequence;
        } else if (h.sequence < groups[i].first)
            groups[i].first = h.sequence;
    }
    if (dir)
        closedir(dir);
    if (!ok || !count) {
        free(groups);
        return ok ? PP_CACHE_FULL : PP_CACHE_IO_ERROR;
    }
    qsort(groups, count, sizeof(*groups), group_order);
    unsigned remove = (count + 1) / 2;
    path_for(s, ".pending", path);
    path_for(s, ".prune", plan);
    FILE *file = fopen(path, "wb");
    ok = file && fwrite(groups, sizeof(*groups), remove, file) == remove && fflush(file) == 0 &&
         fsync(fileno(file)) == 0;
    free(groups);
    if (file && fclose(file))
        ok = false;
    if (!ok || !allowed(s) || rename(path, plan))
        return PP_CACHE_IO_ERROR;
    return finish_prune(s) ? PP_CACHE_HIT : PP_CACHE_IO_ERROR;
}
pp_cache_status_t pp_cache_store_seek(pp_cache_store_t *s, char key[65], pp_cache_seek_t seek, uint64_t *cursor) {
    char path[192], found[65] = "";
    uint64_t anchor = cursor ? *cursor : 0, best = 0;
    header_t h;
    if (!finish_prune(s))
        return allowed(s) ? PP_CACHE_IO_ERROR : PP_CACHE_BUSY;
    if (seek == PP_SEEK_EXACT)
        return PP_CACHE_HIT;
    if (!anchor && seek != PP_SEEK_LATEST) {
        if (!key_valid(key) || !path_for(s, key, path) || !read_header(path, &h) ||
            h.index != PP_HISTORY_HEAD || memcmp(h.scope, s->scope, 16))
            return PP_CACHE_MISSING;
        anchor = h.sequence;
    }
    DIR *dir = opendir(s->root);
    if (!dir)
        return PP_CACHE_IO_ERROR;
    struct dirent *entry;
    bool build = !s->indexed, fast = s->indexed && !s->collision;
    uint32_t target = 0;
    if (fast) {
        for (unsigned i = 0; i < s->heads; i++) {
            uint64_t n = s->head_order[i];
            bool eligible = seek == PP_SEEK_LATEST || (seek == PP_SEEK_NEXT ? n > anchor : n < anchor);
            if (eligible && (!best || (seek == PP_SEEK_NEXT ? n < best : n > best))) {
                best = n;
                target = s->head_tag[i];
            }
        }
    } else if (build) {
        s->heads = 0;
        s->indexed = true;
    }
    while ((!fast || best) && (entry = readdir(dir))) {
        if (!allowed(s)) {
            if (build)
                s->indexed = false;
            closedir(dir);
            return PP_CACHE_BUSY;
        }
        if (!key_valid(entry->d_name) || (fast && key_tag(entry->d_name) != target))
            continue;
        if (!path_for(s, entry->d_name, path) || !read_header(path, &h)) {
            if (build)
                s->indexed = false;
            continue;
        }
        if (h.index != PP_HISTORY_HEAD || memcmp(h.scope, s->scope, 16))
            continue;
        if (build)
            index_head(s, entry->d_name, &h, false);
        if (fast) {
            if (h.sequence != best)
                continue;
            strcpy(found, entry->d_name);
            break;
        }
        bool eligible =
            seek == PP_SEEK_LATEST || (seek == PP_SEEK_NEXT ? h.sequence > anchor : h.sequence < anchor);
        if (eligible && (!best || (seek == PP_SEEK_NEXT ? h.sequence < best : h.sequence > best))) {
            strcpy(found, entry->d_name);
            best = h.sequence;
        }
    }
    closedir(dir);
    if (!found[0])
        return PP_CACHE_MISSING;
    if (cursor)
        *cursor = best;
    strcpy(key, found);
    return PP_CACHE_HIT;
}
bool pp_cache_store_recover(pp_cache_store_t *s) {
    if (!allowed(s) || !finish_prune(s))
        return false;
    DIR *dir = opendir(s->root);
    if (!dir)
        return false;
    s->heads = 0;
    s->indexed = true;
    s->collision = false;
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
        if (!valid && (!allowed(s) || unlink(path))) {
            closedir(dir);
            return false;
        }
        if (valid) {
            used += charge(sizeof(h) + h.size);
            index_head(s, entry->d_name, &h, false);
        }
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
void pp_cache_store_scope(pp_cache_store_t *s, const unsigned char scope[16]) {
    if (memcmp(s->scope, scope, sizeof(s->scope))) {
        s->indexed = false;
        s->heads = 0;
        s->collision = false;
    }
    memcpy(s->scope, scope, sizeof(s->scope));
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
        v->sequence = h.sequence;
        v->bytes.size = h.size;
        v->aux_size = h.aux_size;
        memcpy(v->aux, h.aux, h.aux_size);
    }
    /* The caller releases any allocation even after a failed body read. */
    return result;
}
static pp_cache_status_t commit_object(const pp_cache_store_t *s, const char *path, const char *staged,
                                       const header_t *h, const void *data) {
    if (!allowed(s))
        return PP_CACHE_BUSY;
    FILE *file = fopen(staged, "wb");
    if (!file)
        return errno == ENOSPC ? PP_CACHE_FULL : PP_CACHE_IO_ERROR;
    bool ok = fwrite(h, sizeof(*h), 1, file) == 1;
    size_t offset = 0;
    while (ok && offset < h->size && allowed(s)) {
        size_t n = h->size - offset;
        if (n > 512)
            n = 512;
        ok = fwrite((const char *)data + offset, 1, n, file) == n;
        offset += n;
    }
    ok = ok && offset == h->size && allowed(s) && fflush(file) == 0 && fsync(fileno(file)) == 0;
    int error = ok ? 0 : errno;
    if (fclose(file) && !error) {
        ok = false;
        error = errno;
    }
    bool permitted = allowed(s);
    if (ok && permitted) {
        if (rename(staged, path) == 0)
            return PP_CACHE_HIT;
        error = errno;
    }
    unlink(staged);
    if (!permitted)
        return PP_CACHE_BUSY;
    return error == ENOSPC ? PP_CACHE_FULL : PP_CACHE_IO_ERROR;
}
pp_cache_status_t pp_cache_store_write_ordered(pp_cache_store_t *s, const char key[65],
                                               const unsigned char owner[16], pp_object_kind_t kind,
                                               const void *data, size_t size, const void *aux,
                                               size_t aux_size, size_t reserve, uint64_t import_order) {
    if (!key_valid(key) || !data || size > PP_CACHE_VALUE_MAX || aux_size > PP_CACHE_AUX_MAX ||
        (aux_size && !aux) || s->sequence == UINT64_MAX)
        return PP_CACHE_IO_ERROR;
    if (!allowed(s))
        return PP_CACHE_BUSY;
    char path[192], staged[192];
    if (!path_for(s, key, path) || !path_for(s, ".pending", staged))
        return PP_CACHE_IO_ERROR;
    if (!finish_prune(s))
        return allowed(s) ? PP_CACHE_IO_ERROR : PP_CACHE_BUSY;
    header_t h = {.magic = CACHE_MAGIC,
                  .version = 1,
                  .sequence = import_order ? import_order : s->sequence + 1,
                  .size = size,
                  .aux_size = aux_size,
                  .data_crc = crc(data, size),
                  .index = kind};
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
        if (valid && kind == PP_HISTORY_HEAD && prior.index == kind) {
            if (import_order) {
                fclose(old);
                return PP_CACHE_HIT;
            }
            h.sequence = prior.sequence;
        }
        if (valid)
            memcpy(h.owner, prior.owner, sizeof(h.owner));
        bool same = valid && prior.index == kind && prior.size == size && prior.data_crc == h.data_crc &&
                    prior.aux_size == aux_size && !memcmp(prior.scope, h.scope, sizeof(h.scope)) &&
                    !memcmp(prior.aux, h.aux, aux_size);
        same = same && valid_body(s, old, &prior);
        fclose(old);
        /* No duplicate pixel allocation just to avoid an identical Flash write. */
        if (same)
            return PP_CACHE_HIT;
    }
    size_t limit = reserve < s->budget ? s->budget - reserve : 0;
    pp_cache_status_t capacity = PP_CACHE_HIT;
    if (charge(sizeof(h) + size) > limit)
        return PP_CACHE_FULL;
    size_t staged_bytes = charge(sizeof(h) + size);
    bool reclaim_physical = false;
    if (s->available && s->used + staged_bytes > limit * 3 / 4) {
        size_t free_bytes;
        if (!s->available(&free_bytes))
            return PP_CACHE_IO_ERROR;
        /* Leave enough actual blocks for the plan and directory splits even
         * when metadata costs exceed the conservative per-object estimate. */
        reclaim_physical = free_bytes < staged_bytes + 32u * 1024u;
    }
    while (reclaim_physical || s->used + staged_bytes > limit + prior_bytes) {
        reclaim_physical = false;
        capacity = prune_half(s, h.owner);
        if (capacity != PP_CACHE_HIT)
            return allowed(s) ? capacity : PP_CACHE_BUSY;
    }
    if (!allowed(s))
        return PP_CACHE_BUSY;
    h.header_crc = crc(&h, offsetof(header_t, header_crc));
    pp_cache_status_t written = commit_object(s, path, staged, &h, data);
    if (written == PP_CACHE_FULL) {
        /* The filesystem may run out of metadata before our charged budget. */
        capacity = prune_half(s, h.owner);
        if (capacity != PP_CACHE_HIT)
            return allowed(s) ? capacity : PP_CACHE_BUSY;
        written = commit_object(s, path, staged, &h, data);
    }
    if (written == PP_CACHE_HIT) {
        if (s->sequence < h.sequence)
            s->sequence = h.sequence;
        s->used = s->used - prior_bytes + charge(sizeof(h) + size);
        index_head(s, key, &h, false);
    }
    return written;
}
pp_cache_status_t pp_cache_store_write(pp_cache_store_t *s, const char key[65],
                                       const unsigned char owner[16], pp_object_kind_t kind,
                                       const void *data, size_t size, const void *aux, size_t aux_size,
                                       size_t reserve) {
    return pp_cache_store_write_ordered(s, key, owner, kind, data, size, aux, aux_size, reserve, 0);
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
        if ((key_valid(entry->d_name) || !strcmp(entry->d_name, ".pending") ||
             !strcmp(entry->d_name, ".prune")) &&
            path_for(s, entry->d_name, path) && unlink(path))
            ok = false;
    closedir(dir);
    if (ok) {
        s->used = 0;
        s->heads = 0;
        s->indexed = true;
        s->collision = false;
    }
    return ok;
}

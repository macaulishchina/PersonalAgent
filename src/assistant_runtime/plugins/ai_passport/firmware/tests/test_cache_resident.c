#include "cache_resident.h"
#include <assert.h>
#include <string.h>

static pp_cache_value_t object(size_t bytes) {
    pp_cache_value_t value = {0};
    assert(pp_buffer_reserve(&value.bytes, bytes, PP_MEMORY_NORMAL, PP_MEMORY_CACHE));
    memset(value.bytes.data, 'x', bytes);
    value.bytes.size = bytes;
    return value;
}
int main(void) {
    pp_cache_resident_t cache = {0};
    pp_cache_value_t preview = object(PP_CACHE_RESIDENT_BYTES);
    assert(pp_resident_insert(&cache, "preview", &preview, PP_MEMORY_NORMAL));
    assert(!preview.bytes.data);
    pp_cache_result_t first = {0}, second = {0};
    assert(pp_resident_borrow(&cache, "preview", &first));
    assert(pp_resident_retain(&cache, first.lease, &second));
    assert(second.lease != first.lease && second.value.bytes.data == first.value.bytes.data);
    pp_resident_release(&cache, first.lease);
    pp_resident_release(&cache, first.lease); /* Cannot retire the other reader. */
    assert(pp_resident_recoverable(&cache) == 0);
    assert(!pp_resident_remove(&cache, PP_MEMORY_CRITICAL, 0).data);
    assert(second.value.bytes.data[0] == 'x');
    pp_resident_invalidate(&cache, "preview");
    assert(!pp_resident_borrow(&cache, "preview", &first));
    pp_resident_release(&cache, second.lease);
    assert(pp_resident_recoverable(&cache) == PP_CACHE_RESIDENT_BYTES);
    assert(!pp_resident_remove(&cache, PP_MEMORY_CRITICAL, 1024).data);
    pp_buffer_t reused = pp_resident_remove(&cache, PP_MEMORY_CRITICAL, PP_CACHE_RESIDENT_BYTES);
    assert(reused.data == second.value.bytes.data);
    pp_buffer_free(&reused);

    pp_cache_value_t foreground = object(1024), background = object(2048);
    assert(pp_resident_insert(&cache, "foreground", &foreground, PP_MEMORY_NORMAL));
    assert(pp_resident_insert(&cache, "background", &background, PP_MEMORY_BACKGROUND));
    reused = pp_resident_remove(&cache, PP_MEMORY_BACKGROUND, 0);
    assert(reused.capacity == 2048);
    pp_buffer_free(&reused);
    assert(!pp_resident_remove(&cache, PP_MEMORY_BACKGROUND, 0).data);
    reused = pp_resident_remove(&cache, PP_MEMORY_CRITICAL, 0);
    pp_buffer_free(&reused);
    assert(pp_resident_recoverable(&cache) == 0);
}

#include "settings_model.h"
#include <assert.h>
#include <string.h>

static pp_settings_t original, prepared;
static cJSON *changes, *versions;
static bool changed, reconnect;
static const char release_url[] = "https://release.example:8047";
static const char release_ca[] = "-----BEGIN CERTIFICATE-----\nY2E=\n-----END CERTIFICATE-----\n";
static bool prepare(const char *text) {
    cJSON_Delete(changes);
    cJSON_Delete(versions);
    changes = cJSON_Parse(text);
    versions = cJSON_CreateObject();
    return pp_settings_prepare_cloud(&original, changes, &prepared, versions, &changed, &reconnect);
}
static void test_terminal_snapshot(const pp_settings_t *source) {
    _Static_assert(sizeof(pp_terminal_settings_t) <= 704,
                   "Rendering must not duplicate the 4 KiB CA or Wi-Fi credentials");
    pp_terminal_settings_t view;
    pp_settings_project(source, 9, &view);
    assert(view.revision == 9 && !strcmp(view.url, source->url) &&
           !strcmp(view.device, source->device) && !strcmp(view.binding, source->binding));
    assert(view.wifi_count == source->wifi_count && view.volume == source->volume &&
           view.brightness == source->brightness && view.sleep_seconds == source->sleep_seconds &&
           view.text_size == source->text_size && view.queue == source->queue &&
           view.keep_online == source->keep_online && view.paired == source->paired &&
           view.offline_cache == source->offline_cache && view.show_images == source->show_images);
    pp_settings_t edit = *source;
    unsigned previous, ignored;
    assert(pp_settings_edit_preference(&edit, PP_PREF_VOLUME, 70, &previous));
    assert(previous == source->volume && view.volume == source->volume);
    assert(pp_settings_edit_preference(&edit, PP_PREF_VOLUME, previous, &ignored));
    assert(ignored == 70 && !memcmp(&edit, source, sizeof(edit)));
    assert(!pp_settings_edit_preference(&edit, PP_PREF_VOLUME, 101, &previous));
    assert(!pp_settings_edit_preference(&edit, PP_PREF_KEEP_ONLINE, 2, &previous));
    assert(!memcmp(&edit, source, sizeof(edit)));
    assert(pp_settings_edit_preference(&edit, PP_PREF_PAIRED, !source->paired, &previous));
    assert(view.paired == source->paired); /* Published views are stable until refreshed. */
    pp_settings_project(&edit, 10, &view);
    assert(view.paired == edit.paired && view.revision == 10);
}
int main(void) {
    _Static_assert(sizeof(pp_settings_t) < 6144,
                   "Settings must remain a bounded internal-memory object");
    original.magic = PP_SETTINGS_MAGIC;
    original.volume = 45;
    original.sleep_seconds = 120;
    original.brightness = 65;
    original.text_size = 16;
    memset(original.device, 'a', 32);
    memset(original.credential, 'b', 64);
    original.offline_cache = original.show_images = true;
    assert(pp_settings_valid(&original));
    const pp_settings_t first = original;
    assert(pp_settings_prepare_bootstrap(&original, "{\"ssid\":\"Home\",\"password\":\"password\"}",
                                         release_url, release_ca, &prepared));
    assert(!memcmp(&original, &first, sizeof(first)));
    assert(prepared.wifi_count == 1 && !prepared.paired);
    assert(!strcmp(prepared.url, release_url) && !strcmp(prepared.ca, release_ca));
    assert(!strcmp(prepared.device, original.device) &&
           !strcmp(prepared.credential, original.credential));
    test_terminal_snapshot(&prepared);
    char *encoded = pp_settings_encode(&prepared);
    assert(encoded && strlen(encoded) < 1024);
    pp_settings_t restored;
    assert(pp_settings_decode(encoded, &restored));
    assert(!memcmp(&prepared, &restored, sizeof(restored)));
    cJSON_free(encoded);
    restored.paired = true;
    /* Pairing is saved before the first WebSocket has supplied a binding. */
    encoded = pp_settings_encode(&restored);
    assert(encoded && pp_settings_decode(encoded, &prepared));
    assert(prepared.paired && !prepared.binding[0]);
    cJSON_free(encoded);
    strcpy(restored.binding, "saved-session");
    encoded = pp_settings_encode(&restored);
    assert(encoded && pp_settings_decode(encoded, &prepared));
    assert(!memcmp(&prepared, &restored, sizeof(restored)));
    cJSON_free(encoded);
    original = restored;
    assert(prepare("{\"server_url\":{\"version\":1,\"value\":\"https://release.example:8047\"}}"));
    assert(prepared.paired);
    assert(prepare("{\"server_url\":{\"version\":1,\"value\":\"https://another.example:8047\"}}"));
    assert(changed && reconnect && !prepared.paired);
    assert(pp_settings_prepare_bootstrap(&restored, "{\"ssid\":\"Cafe\"}", release_url, release_ca,
                                         &prepared));
    assert(prepared.paired && !strcmp(prepared.wifi[0].ssid, "Cafe") &&
           !strcmp(prepared.credential, restored.credential));
    assert(pp_settings_prepare_bootstrap(&restored, "{\"ssid\":\"Cafe\"}",
                                         "https://another.example:8047", release_ca, &prepared));
    assert(!prepared.paired && !strcmp(prepared.device, restored.device));
    prepared = restored;
    strcpy(prepared.binding, "old-session");
    prepared.versions[0] = 42;
    pp_settings_forget_binding(&prepared);
    assert(!prepared.paired && !prepared.binding[0] && !prepared.versions[0]);
    assert(prepared.wifi_count == restored.wifi_count &&
           !memcmp(prepared.wifi, restored.wifi, sizeof(prepared.wifi)));
    assert(!strcmp(prepared.ca, restored.ca) && !strcmp(prepared.url, restored.url));
    assert(pp_settings_valid(&prepared));
    original = prepared;
    assert(prepare("{\"brightness\":{\"version\":1,\"value\":85},"
                   "\"text_size\":{\"version\":1,\"value\":18}}"));
    assert(changed && !reconnect && prepared.brightness == 85 && prepared.text_size == 18);
    original = prepared;
    assert(prepare("{\"volume\":{\"version\":1,\"value\":55},\"keep_online\":{\"version\":1,"
                   "\"value\":true}}"));
    assert(changed && !reconnect && prepared.volume == 55 && prepared.keep_online);
    original = prepared;
    original.volume = 80; /* Local preference, cloud version unchanged. */
    assert(prepare("{\"volume\":{\"version\":1,\"value\":55}}"));
    assert(!changed && prepared.volume == 80);
    assert(prepare("{\"sleep_seconds\":{\"version\":2,\"value\":180}}"));
    assert(changed && prepared.volume == 80 && prepared.sleep_seconds == 180);
    assert(prepare("{\"wifi_networks\":{\"version\":1,\"value\":[{\"ssid\":\"Home\",\"password\":"
                   "\"password\"},{\"ssid\":\"Phone\",\"password\":\"\"}]}}"));
    assert(reconnect && prepared.wifi_count == 2 && !strcmp(prepared.wifi[1].ssid, "Phone"));
    original = prepared;
    assert(prepare("{\"wifi_networks\":{\"version\":2,\"value\":null}}"));
    assert(changed && !reconnect && prepared.wifi_count == 2 &&
           !strcmp(prepared.wifi[0].ssid, "Home"));
    encoded = pp_settings_encode(&prepared);
    assert(encoded && pp_settings_decode(encoded, &restored));
    assert(!memcmp(&prepared, &restored, sizeof(restored)));
    cJSON *saved = cJSON_Parse(encoded);
    cJSON *saved_values = cJSON_GetObjectItemCaseSensitive(saved, "values");
    cJSON *saved_versions = cJSON_GetObjectItemCaseSensitive(saved, "versions");
    cJSON_DeleteItemFromObjectCaseSensitive(saved_values, "brightness");
    cJSON_DeleteItemFromObjectCaseSensitive(saved_values, "text_size");
    cJSON_DeleteItemFromArray(saved_versions, 11);
    cJSON_DeleteItemFromArray(saved_versions, 10);
    char *prior = cJSON_PrintUnformatted(saved);
    assert(pp_settings_decode(prior, &restored));
    assert(restored.brightness == 65 && restored.text_size == 16);
    assert(restored.volume == prepared.volume && restored.queue == prepared.queue);
    assert(!strcmp(restored.device, prepared.device) &&
           !strcmp(restored.credential, prepared.credential));
    cJSON_free(prior);
    cJSON_Delete(saved);
    cJSON_free(encoded);
    const pp_settings_t before = original;
    const char *bad[] = {
        "{\"volume\":{\"version\":2,\"value\":101}}",
        "{\"brightness\":{\"version\":2,\"value\":0}}",
        "{\"text_size\":{\"version\":2,\"value\":17}}",
        "{\"volume\":{\"version\":true,\"value\":50}}",
        "{\"volume\":{\"version\":-1,\"value\":50}}",
        "{\"volume\":{\"version\":1.2,\"value\":50}}",
        "{\"sleep_seconds\":{\"version\":2,\"value\":true}}",
        "{\"unrecognized\":{\"version\":1,\"value\":true}}",
        "{\"volume\":{\"version\":2,\"value\":20},\"keep_online\":{\"version\":2,\"value\":"
        "\"true\"}}",
        "{\"server_url\":{\"version\":1,\"value\":\"http://user:secret@host\"}}",
        "{\"wifi_networks\":{\"version\":3,\"value\":[{\"ssid\":\"a\",\"password\":\"password\"},{"
        "\"ssid\":\"a\",\"password\":\"password\"}]}}"};
    for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
        assert(!prepare(bad[i]));
        assert(!memcmp(&original, &before, sizeof(original)));
    }
    assert(!pp_settings_prepare_bootstrap(&original, "{\"ssid\":\"Home\",\"password\":\"short\"}",
                                          release_url, release_ca, &prepared));
    assert(!pp_settings_prepare_bootstrap(
        &original, "{\"ssid\":\"Home\",\"server_url\":\"https://other.example\"}", release_url,
        release_ca, &prepared));
    assert(!pp_settings_prepare_bootstrap(&original, "{\"ssid\":\"Home\",\"trusted_ca\":\"\"}",
                                          release_url, release_ca, &prepared));
    assert(!pp_settings_prepare_bootstrap(&original, "{\"ssid\":\"Home\",\"ssid\":\"Other\"}",
                                          release_url, release_ca, &prepared));
    cJSON_Delete(changes);
    cJSON_Delete(versions);
    return 0;
}

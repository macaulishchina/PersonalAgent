idf_component_register(
    SRCS
        "main.c" "terminal_events.c" "diagnostics.c"
        "terminal_model.c" "navigation.c" "notification_model.c"
        "terminal_audio.c" "playback_buffer.c"
        "ui.c" "text_layout.c" "timeline.c" "image_model.c" "pictures.c"
        "network.c" "network_model.c" "provision.c" "provision_access.c" "byte_buffer.c"
        "settings.c" "settings_model.c" "cache_store.c" "cache_disk.c" "cache_resident.c" "history_cache.c"
    INCLUDE_DIRS "."
    REQUIRES bsp pp_board esp_event esp_timer esp_wifi esp_netif esp_http_client esp_websocket_client esp_pm
             nvs_flash json esp_app_format esp_hw_support mbedtls esp_http_server bootloader_support joltwallet__littlefs)
target_sources(${COMPONENT_LIB} PRIVATE "fonts/terminal_zh_16.c" "fonts/terminal_zh_18.c")
target_add_binary_data(${COMPONENT_LIB} "assets/cat_idle_open.rgb565a8" BINARY)
target_add_binary_data(${COMPONENT_LIB} "assets/cat_idle_blink.rgb565a8" BINARY)
target_add_binary_data(${COMPONENT_LIB} "assets/cat_input_open.rgb565a8" BINARY)
target_add_binary_data(${COMPONENT_LIB} "assets/cat_input_blink.rgb565a8" BINARY)

if(NOT PP_PA_URL MATCHES "^https?://([A-Za-z0-9.-]+|\\[[A-Fa-f0-9:.]+\\])(:[0-9]+)?/?$")
    message(FATAL_ERROR "Set PP_PA_URL to this build's PA business root URL")
endif()
target_compile_definitions(${COMPONENT_LIB} PRIVATE PP_PA_URL="${PP_PA_URL}")
if(PP_PA_CA_FILE)
    if(NOT PP_PA_URL MATCHES "^https://")
        message(FATAL_ERROR "PP_PA_CA_FILE requires an HTTPS PA URL")
    endif()
    file(SIZE "${PP_PA_CA_FILE}" ca_size)
    if(ca_size GREATER 4096)
        message(FATAL_ERROR "PP_PA_CA_FILE exceeds the device's certificate size limit")
    endif()
    file(READ "${PP_PA_CA_FILE}" ca_pem)
    string(STRIP "${ca_pem}" ca_pem)
    if(NOT ca_pem MATCHES "^-----BEGIN CERTIFICATE-----[\r\n]+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----$")
        message(FATAL_ERROR "PP_PA_CA_FILE must contain one public PEM certificate")
    endif()
    configure_file("${PP_PA_CA_FILE}" "${CMAKE_CURRENT_BINARY_DIR}/pp_release_ca.pem" COPYONLY)
    target_add_binary_data(${COMPONENT_LIB} "${CMAKE_CURRENT_BINARY_DIR}/pp_release_ca.pem" TEXT)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE PP_HAS_RELEASE_CA=1)
endif()

# The VFS collapses all mount errors to ESP_FAIL. Its pinned LittleFS headers
# allow a read-only probe to distinguish corruption from I/O and allocation errors.
idf_component_get_property(pp_littlefs_dir joltwallet__littlefs COMPONENT_DIR)
target_include_directories(${COMPONENT_LIB} PRIVATE "${pp_littlefs_dir}/src/littlefs" "${pp_littlefs_dir}/src")

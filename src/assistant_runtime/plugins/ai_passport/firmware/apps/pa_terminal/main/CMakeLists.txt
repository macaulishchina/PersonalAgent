idf_component_register(SRCS "main.c" "terminal_model.c" "navigation.c" "notification_model.c" "settings.c" "settings_model.c" "ui.c" "text_layout.c" "timeline.c" "image_model.c" "pictures.c" "network.c" "network_model.c" "provision.c" "provision_access.c" "byte_buffer.c" "cache_store.c" "cache_disk.c" "history_cache.c"
    INCLUDE_DIRS "."
    REQUIRES bsp esp_event esp_timer esp_wifi esp_netif esp_http_client esp_websocket_client
             nvs_flash json esp_app_format esp_hw_support mbedtls esp_http_server bootloader_support joltwallet__littlefs)
target_sources(${COMPONENT_LIB} PRIVATE "fonts/terminal_zh_16.c" "fonts/terminal_zh_18.c")
target_add_binary_data(${COMPONENT_LIB} "assets/cat_idle_open.rgb565a8" BINARY)
target_add_binary_data(${COMPONENT_LIB} "assets/cat_idle_blink.rgb565a8" BINARY)
target_add_binary_data(${COMPONENT_LIB} "assets/cat_input_open.rgb565a8" BINARY)
target_add_binary_data(${COMPONENT_LIB} "assets/cat_input_blink.rgb565a8" BINARY)

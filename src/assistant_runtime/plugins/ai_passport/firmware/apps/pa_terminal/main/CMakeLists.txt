idf_component_register(SRCS "main.c" "terminal_model.c" "settings.c" "ui.c" "network.c"
    INCLUDE_DIRS "."
    REQUIRES bsp esp_event esp_timer esp_wifi esp_netif esp_http_client esp_websocket_client
             nvs_flash json esp_app_format esp_hw_support)
target_sources(${COMPONENT_LIB} PRIVATE "fonts/terminal_zh_16.c")

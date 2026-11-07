idf_component_register(
    SRCS "main.c" "lab_model.c" "lab_ui.c" "lab_wifi.c" "lab_ble.c"
    INCLUDE_DIRS "."
    REQUIRES bsp bt esp_app_format esp_event esp_hw_support esp_http_server esp_netif
             esp_timer esp_wifi nvs_flash)

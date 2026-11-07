idf_component_register(
    SRCS "main.c" "lab_model.c" "lab_language.c" "lab_ui.c" "lab_wifi.c" "lab_ble.c"
    INCLUDE_DIRS "."
    REQUIRES bsp bt esp_app_format esp_event esp_hw_support esp_http_server esp_netif
             esp_timer esp_wifi nvs_flash)

if(CONFIG_PPDEMO_LANGUAGE_ZH)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE PPDEMO_ZH=1)
    target_sources(${COMPONENT_LIB} PRIVATE
        "fonts/ppdemo_zh_14.c" "fonts/ppdemo_zh_16.c" "fonts/ppdemo_zh_20.c")
endif()

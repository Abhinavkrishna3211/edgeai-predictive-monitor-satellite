idf_component_register(
    SRCS "main.c" "rgb_led_task.c" "mic_task.c" "dsp_task.c" "imu_task.c" "wifi_task.c"
    INCLUDE_DIRS "."
    REQUIRES mic_capture espressif__esp-dsp esp_wifi nvs_flash esp_netif lwip driver
            espressif__mdns mbedtls esp_pm
)

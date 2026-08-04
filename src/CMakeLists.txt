idf_component_register(
    SRCS "main.c" "mic_task.c" "dsp_task.c" "imu_task.c" "wifi_task.c"
         "threads/net_task.c" "threads/led_task.c"
    INCLUDE_DIRS "."
    REQUIRES espressif__esp-dsp esp_wifi nvs_flash esp_netif lwip driver
            mbedtls esp_pm epm_hal epm_drivers epm_codec epm_dsp
)

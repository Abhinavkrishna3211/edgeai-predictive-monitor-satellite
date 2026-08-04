idf_component_register(
    SRCS "main.c" "wifi_task.c"
         "threads/net_task.c" "threads/led_task.c"
         "threads/mic_task.c" "threads/dsp_task.c" "threads/imu_task.c"
    INCLUDE_DIRS "."
    REQUIRES espressif__esp-dsp esp_wifi nvs_flash esp_netif lwip driver
            mbedtls esp_pm epm_hal epm_drivers epm_codec epm_dsp
)

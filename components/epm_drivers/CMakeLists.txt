set(epm_drivers_srcs "link_mqtt.c" "mic_inmp441_i2s.c")

if(CONFIG_EPM_DISPLAY_USE_LEDC)
    list(APPEND epm_drivers_srcs "display_ledc.c")
else()
    list(APPEND epm_drivers_srcs "display_neopixel.c")
endif()

if(CONFIG_EPM_ACCEL_USE_STUB)
    list(APPEND epm_drivers_srcs "accel_stub.c")
endif()

idf_component_register(
    SRCS ${epm_drivers_srcs}
    INCLUDE_DIRS "include"
    REQUIRES epm_hal epm_codec mqtt esp_wifi esp_event log freertos driver esp_common espressif__led_strip
)

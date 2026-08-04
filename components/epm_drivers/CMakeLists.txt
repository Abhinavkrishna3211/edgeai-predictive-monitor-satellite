set(epm_drivers_srcs "link_mqtt.c" "display_ledc.c" "mic_inmp441_i2s.c")

if(CONFIG_EPM_ACCEL_USE_STUB)
    list(APPEND epm_drivers_srcs "accel_stub.c")
endif()

idf_component_register(
    SRCS ${epm_drivers_srcs}
    INCLUDE_DIRS "include"
    REQUIRES epm_hal epm_codec mqtt esp_wifi esp_event log freertos driver esp_common
)

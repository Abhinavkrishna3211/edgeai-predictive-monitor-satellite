idf_component_register(
    SRCS "link_mqtt.c"
    INCLUDE_DIRS "include"
    REQUIRES epm_hal epm_codec mqtt esp_wifi esp_event log freertos
)

# Shared ESP32 (ESP-IDF) code lives in firmware/common/esp32/ and is compiled
# directly into this firmware's `main` component (absolute-path SRCS) rather
# than as a separate IDF component — this keeps the shared code inside `main`'s
# REQUIRES set and avoids per-component dependency wiring. seeedstudio_e1004
# compiles the same files the same way.
set(HOKKU_COMMON     "${CMAKE_CURRENT_LIST_DIR}/../../common/esp32")
set(HOKKU_COMMON_ALL "${CMAKE_CURRENT_LIST_DIR}/../../common/all")

idf_component_register(
    SRCS "main.c"
         "${HOKKU_COMMON}/config.c"
         "${HOKKU_COMMON}/text_render.c"
         "${HOKKU_COMMON}/state.c"
         "${HOKKU_COMMON}/scheduler.c"
         "${HOKKU_COMMON}/log.c"
         "${HOKKU_COMMON}/wifi.c"
         "${HOKKU_COMMON}/net.c"
         "${HOKKU_COMMON}/ota.c"
         "${HOKKU_COMMON_ALL}/firmware_url.c"
         "${HOKKU_COMMON_ALL}/frame_state.c"
         "${HOKKU_COMMON_ALL}/json_util.c"
         "${HOKKU_COMMON_ALL}/logbuf.c"
         "${HOKKU_COMMON_ALL}/backoff.c"
    INCLUDE_DIRS "." "${HOKKU_COMMON}" "${HOKKU_COMMON_ALL}" "${CMAKE_BINARY_DIR}"
)

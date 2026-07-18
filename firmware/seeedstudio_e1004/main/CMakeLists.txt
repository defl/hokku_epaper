# Shared firmware code compiled directly into this firmware's `main` component
# (absolute-path SRCS) — the same modules huessen_epf1301 uses. common/esp32 is
# ESP-IDF-specific (shared by the two ESP32 boards); common/all is pure C
# (shared by all three firmwares, incl. the XR872 F7).
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
    INCLUDE_DIRS "." "${HOKKU_COMMON}" "${HOKKU_COMMON_ALL}" "${CMAKE_BINARY_DIR}"
)

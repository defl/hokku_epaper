#pragma once
#include <stdint.h>

typedef void *esp_http_client_handle_t;

typedef enum {
    HTTP_EVENT_ERROR = 0,
    HTTP_EVENT_ON_CONNECTED,
    HTTP_EVENT_HEADERS_SENT,
    HTTP_EVENT_ON_HEADER,
    HTTP_EVENT_ON_DATA,
    HTTP_EVENT_ON_FINISH,
    HTTP_EVENT_DISCONNECTED,
} esp_http_client_event_id_t;

typedef struct {
    esp_http_client_event_id_t  event_id;
    esp_http_client_handle_t    client;
    void   *data;
    int     data_len;
    char   *header_key;
    char   *header_value;
    void   *user_data;
} esp_http_client_event_t;

typedef int (*http_event_handle_cb)(esp_http_client_event_t *);

typedef struct {
    const char           *url;
    http_event_handle_cb  event_handler;
    void                 *user_data;
    int                   timeout_ms;
    int                   buffer_size;
} esp_http_client_config_t;

static inline esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *c) {
    (void)c; return (void *)1;
}
static inline int esp_http_client_set_header(esp_http_client_handle_t c, const char *k, const char *v) {
    (void)c; (void)k; (void)v; return 0;
}
static inline int esp_http_client_perform(esp_http_client_handle_t c) { (void)c; return -1; }
static inline int esp_http_client_get_status_code(esp_http_client_handle_t c) { (void)c; return 0; }
static inline int esp_http_client_cleanup(esp_http_client_handle_t c) { (void)c; return 0; }

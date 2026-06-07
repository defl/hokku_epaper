/*
 * Hokku EPaper firmware for Bigme F7 (XR872AT + EK79655 7-color EPD)
 *
 * Flow:
 *   platform_init() auto-connects WiFi from sysinfo flash credentials.
 *   On NETWORK_UP: init EPD, then loop:
 *     GET /hokku/screen/ → stream 192000-byte 4bpp response body to EPD → refresh
 *     sleep X-Sleep-Seconds (from response header)
 *
 * WiFi provisioning via UART console:
 *   net sta config <ssid> <password>
 *   net sta enable
 */

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

#include "kernel/os/os.h"
#include "common/framework/platform_init.h"
#include "common/framework/net_ctrl.h"
#include "net/HTTPClient/HTTPCUsr_api.h"
#include "net/HTTPClient/API/HTTPClient.h"

#include "epd.h"

#define HOKKU_SERVER_URL        "http://hokku.local/hokku/screen/"
#define SCREEN_NAME             "bigme-f7"
#define FIRMWARE_VERSION        "1.0.0"

#define EPD_IMAGE_BYTES         192000U  /* 800 x 480 x 4bpp / 8 */
#define DEFAULT_SLEEP_SECONDS   300
#define HTTP_TIMEOUT_S          90       /* covers 192KB DL + EPD streaming time */

#define REFRESH_THREAD_STACK    (8 * 1024)
#define REFRESH_THREAD_PRIO     OS_THREAD_PRIO_APP

static OS_Thread_t g_refresh_thread;
static int         g_epd_ready = 0;

/*
 * Fetch one image from the server and stream it byte-by-byte to the EPD.
 * Returns the number of seconds to sleep before the next refresh, or -1 on
 * any error (caller should retry after a short backoff).
 */
static int do_refresh(void)
{
    HTTPParameters params;
    HTTP_CLIENT    info;
    char           hdr_val[32];
    uint32_t       hdr_len;
    char           buf[512];
    uint32_t       bytes_streamed = 0;
    int            sleep_sec = DEFAULT_SLEEP_SECONDS;
    int            ret;

    memset(&params, 0, sizeof(params));
    strncpy(params.Uri, HOKKU_SERVER_URL, sizeof(params.Uri) - 1);
    params.HttpVerb  = VerbGet;
    params.nTimeout  = HTTP_TIMEOUT_S;

    ret = HTTPC_open(&params);
    if (ret != HTTP_CLIENT_SUCCESS) {
        printf("hokku: HTTP open failed (%d)\n", ret);
        return -1;
    }

    /* Request headers — server uses these for logging / OTA checks */
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Screen-Name",     SCREEN_NAME,      1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Firmware-Version", FIRMWARE_VERSION, 1);
    HTTPClientAddRequestHeaders(params.pHTTP, "X-Frame-State",     "{\"ver\":1}",     1);

    ret = HTTPC_request(&params, NULL);
    if (ret != HTTP_CLIENT_SUCCESS) {
        printf("hokku: HTTP request failed (%d)\n", ret);
        HTTPC_close(&params);
        return -1;
    }

    /* Check HTTP status code */
    if (HTTPC_get_request_info(&params, &info) != HTTP_CLIENT_SUCCESS) {
        HTTPC_close(&params);
        return -1;
    }
    if (info.HTTPStatusCode != 200) {
        printf("hokku: server returned %u\n", (unsigned)info.HTTPStatusCode);
        HTTPC_close(&params);
        /* For 503/404 (no image ready), use a short retry */
        return (info.HTTPStatusCode == 503 || info.HTTPStatusCode == 404) ? 30 : -1;
    }

    /* Capture sleep time from response header before consuming body */
    hdr_len = sizeof(hdr_val);
    if (HTTPClientFindFirstHeader(params.pHTTP, "X-Sleep-Seconds",
                                  hdr_val, &hdr_len) == HTTP_CLIENT_SUCCESS) {
        int s = atoi(hdr_val);
        if (s > 0)
            sleep_sec = s;
    }

    /* Stream response body to EPD (CMD 0x10 was not sent yet — do it now) */
    epd_send_cmd(0x10);  /* DTM: data start transmission */

    while (bytes_streamed < EPD_IMAGE_BYTES) {
        uint32_t to_read = sizeof(buf);
        uint32_t n = 0;

        ret = HTTPC_read(&params, buf, to_read, &n);
        if (n > 0) {
            for (uint32_t i = 0; i < n; i++)
                epd_send_data((uint8_t)buf[i]);
            bytes_streamed += n;
        }
        if (ret != HTTP_CLIENT_SUCCESS)
            break;
    }

    HTTPC_close(&params);

    if (bytes_streamed != EPD_IMAGE_BYTES) {
        printf("hokku: incomplete image: %u / %u bytes\n",
               (unsigned)bytes_streamed, (unsigned)EPD_IMAGE_BYTES);
        return -1;
    }

    printf("hokku: image received, refreshing display...\n");
    epd_refresh();  /* ~30 s */
    printf("hokku: refresh done, sleeping %d s\n", sleep_sec);

    return sleep_sec;
}

static void refresh_thread_fn(void *arg)
{
    (void)arg;

    if (!g_epd_ready) {
        printf("hokku: EPD init\n");
        epd_init();
        g_epd_ready = 1;
    }

    while (1) {
        int sleep_sec = do_refresh();
        if (sleep_sec < 0) {
            /* Error — short backoff before retry */
            OS_MSleep(30 * 1000);
        } else {
            OS_MSleep((uint32_t)sleep_sec * 1000);
        }
    }
}

static void net_cb(uint32_t event, uint32_t data, void *arg)
{
    uint16_t type = EVENT_SUBTYPE(event);

    switch (type) {
    case NET_CTRL_MSG_NETWORK_UP:
        printf("hokku: network up\n");
        if (!OS_ThreadIsValid(&g_refresh_thread)) {
            OS_ThreadCreate(&g_refresh_thread,
                            "hokku_refresh",
                            refresh_thread_fn,
                            NULL,
                            REFRESH_THREAD_PRIO,
                            REFRESH_THREAD_STACK);
        }
        break;
    case NET_CTRL_MSG_NETWORK_DOWN:
        printf("hokku: network down\n");
        break;
    default:
        break;
    }
}

int main(void)
{
    observer_base *net_ob;

    platform_init();

    printf("\nhokku bigme-f7 firmware\n");
    printf("WiFi provisioning: net sta config <ssid> <password>, then: net sta enable\n\n");

    net_ob = sys_callback_observer_create(CTRL_MSG_TYPE_NETWORK,
                                          NET_CTRL_MSG_ALL,
                                          net_cb,
                                          NULL);
    if (net_ob == NULL)
        return -1;
    if (sys_ctrl_attach(net_ob) != 0)
        return -1;

    return 0;
}

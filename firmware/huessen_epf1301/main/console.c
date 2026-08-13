#include "console.h"

#include <stdio.h>
#include <string.h>

#include "driver/usb_serial_jtag.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "frame_proto.h"
#include "interactive.h"

/* Not `TAG`: the host tests compile main.c and this file into ONE translation
 * unit with `static` #defined away, so a second file-scope `TAG` would be a
 * redefinition of main.c's. */
static const char *CONSOLE_TAG = "console";

/* Longest command line we will assemble. Commands are single words today; the
 * margin is for arguments a later command might take. Anything longer is a host
 * that has lost sync, and is discarded rather than grown into. */
#define CONSOLE_LINE_MAX 96

/* Idle read slice. Long enough that the task costs nothing while waiting,
 * short enough that a command typed by hand still feels immediate. */
#define CONSOLE_IDLE_MS 200

static volatile bool s_busy;
static bool s_started;

bool hokku_console_busy(void)
{
    return s_busy;
}

void hokku_console_frame_begin(void)
{
    s_busy = true;
}

void hokku_console_frame_end(void)
{
    s_busy = false;
}

/* ── Raw I/O ──────────────────────────────────────────────────────────────
 *
 * Everything goes through usb_serial_jtag_read_bytes/write_bytes rather than
 * stdio. printf on this console is line-buffered through the VFS layer, which is
 * fine for log lines and wrong for a protocol where the host is waiting on an
 * exact byte before it sends the next 4 KB. */

int hokku_console_read(uint8_t *buf, uint32_t len, uint32_t timeout_ms)
{
    uint32_t got = 0;
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(timeout_ms);

    while (got < len) {
        TickType_t now = xTaskGetTickCount();
        if (now >= deadline)
            break;
        /* Loop rather than trusting one call: the driver returns what it has,
         * and a 4 KB chunk crosses several USB packets. */
        int n = usb_serial_jtag_read_bytes(buf + got, len - got, deadline - now);
        if (n <= 0)
            break;
        got += (uint32_t)n;
    }
    return (int)got;
}

void hokku_console_write(const void *buf, uint32_t len)
{
    usb_serial_jtag_write_bytes(buf, len, pdMS_TO_TICKS(1000));
}

void hokku_console_printf_line(const char *line)
{
    hokku_console_write(line, (uint32_t)strlen(line));
    hokku_console_write("\r\n", 2);
}

/* ── Command loop ─────────────────────────────────────────────────────────── */

static void handle_line(char *line)
{
    if (line[0] == '\0')
        return;

    if (strcmp(line, "frame") == 0) {
        hokku_frame_receive();
        return;
    }
    bool on = (strcmp(line, "interactive on") == 0);
    if (on || strcmp(line, "interactive off") == 0) {
        hokku_interactive_set(on);
        /* Echo the state back rather than just acknowledging: a host that
         * re-asserts the mode before every upload (the right thing to do, since
         * a crash reboot clears it silently) can then verify in one round trip. */
        hokku_console_printf_line(on ? "INTERACTIVE on" : "INTERACTIVE off");
        return;
    }
    if (strcmp(line, "ping") == 0) {
        /* Lets the host prove it has a live console before committing to a
         * 960 KB upload, prove which firmware it is talking to, and see whether
         * interactive mode survived — a reset clears it, and a reset is exactly
         * what a host would otherwise not notice. */
        hokku_console_printf_line(hokku_interactive_requested()
                                      ? "PONG hokku huessen_epf1301 interactive=on"
                                      : "PONG hokku huessen_epf1301 interactive=off");
        return;
    }
    if (strcmp(line, "help") == 0) {
        hokku_console_printf_line("commands: frame ping help interactive on|off");
        return;
    }
    hokku_console_printf_line("ERR unknown command");
}

static void console_task(void *arg)
{
    char line[CONSOLE_LINE_MAX];
    size_t len = 0;
    bool overflowed = false;

    (void)arg;
    ESP_LOGI(CONSOLE_TAG, "console ready on USB Serial/JTAG (frame ping help interactive)");

    for (;;) {
        uint8_t ch;
        int n = usb_serial_jtag_read_bytes(&ch, 1, pdMS_TO_TICKS(CONSOLE_IDLE_MS));
        if (n <= 0)
            continue;

        if (ch == '\r' || ch == '\n') {
            if (len == 0 && !overflowed)
                continue;               /* bare newline, or the partner of CRLF */
            line[len] = '\0';
            if (overflowed) {
                /* Do not act on the tail of a line whose head was dropped —
                 * "…frame" would look exactly like "frame". */
                hokku_console_printf_line("ERR line too long");
            } else {
                handle_line(line);
            }
            len = 0;
            overflowed = false;
            continue;
        }

        if (len + 1 >= sizeof(line)) {
            overflowed = true;
            continue;
        }
        line[len++] = (char)ch;
    }
}

void hokku_console_start(void)
{
    if (s_started)
        return;

    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    /* RX has to absorb a full protocol chunk plus whatever the host pipelines
     * behind it; the default (256 B) would drop bytes between our reads. */
    cfg.rx_buffer_size = FRAME_PROTO_CHUNK_BYTES * 2;
    cfg.tx_buffer_size = 1024;

    esp_err_t err = usb_serial_jtag_driver_install(&cfg);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(CONSOLE_TAG, "usb_serial_jtag_driver_install failed: %s", esp_err_to_name(err));
        return;
    }

    if (xTaskCreate(console_task, "hokku_console", 4096, NULL, 5, NULL) != pdPASS) {
        ESP_LOGE(CONSOLE_TAG, "failed to create console task");
        return;
    }
    s_started = true;
}

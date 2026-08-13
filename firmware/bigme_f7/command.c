#include <stdio.h>
#include <string.h>

#include "common/cmd/cmd_util.h"
#include "common/cmd/cmd.h"

#include "hokku_config.h"
#include "interactive.h"
#include "led.h"

/* Defined in main.c. */
extern int      hokku_wifi_provision(const char *ssid, const char *psk);
extern uint32_t hokku_battery_mv(void);
extern void     hokku_ota_manual(void);
extern int      hokku_frame_receive(void);

static const struct cmd_data g_net_cmds[] = {
    { "sta", cmd_wlan_sta_exec },
};

static enum cmd_status cmd_net_exec(char *cmd)
{
    return cmd_exec(cmd, g_net_cmds, cmd_nitems(g_net_cmds));
}

/*
 * `wifi <ssid> <password>` — provision WiFi and persist it across reboots.
 * Unlike `net sta config` (runtime-only), this writes the creds to sysinfo so
 * the firmware auto-connects on every cold boot.
 */
static enum cmd_status cmd_wifi_exec(char *cmd)
{
    char *argv[2];
    int argc = cmd_parse_argv(cmd, argv, cmd_nitems(argv));

    if (argc != 2)
        return CMD_STATUS_INVALID_ARG;   /* usage: wifi <ssid> <password> */

    cmd_write_respond(CMD_STATUS_OK, "OK");
    hokku_wifi_provision(argv[0], argv[1]);
    return CMD_STATUS_ACKED;
}

/*
 * `cfg ...` — inspect/change persistent app config (server URL, screen name,
 * static IP / DHCP, default sleep). Changes are in-RAM until `cfg save`.
 *   cfg show
 *   cfg server <url>
 *   cfg name <screen_name>
 *   cfg ip <ip> <gw> <nm>      (also selects static)
 *   cfg dhcp | cfg static
 *   cfg sleep <seconds>
 *   cfg save
 */
static enum cmd_status cmd_cfg_exec(char *cmd)
{
    char           *argv[5];
    int             argc = cmd_parse_argv(cmd, argv, cmd_nitems(argv));
    hokku_config_t *c = hokku_config_get();

    if (argc == 0 || cmd_strcmp(argv[0], "show") == 0) {
        cmd_write_respond(CMD_STATUS_OK, "OK");
        printf("cfg: name='%s'\n", c->screen_name);
        printf("cfg: url='%s'\n", c->server_url);
        printf("cfg: net=%s ip=%s gw=%s nm=%s\n",
               c->use_dhcp ? "dhcp" : "static", c->ip, c->gw, c->nm);
        printf("cfg: power=%s default_sleep_s=%u cfg_ver=%u\n",
               c->power_mode == HOKKU_PWR_SLEEP ? "sleep" :
               c->power_mode == HOKKU_PWR_AWAKE ? "awake" : "auto",
               (unsigned)c->default_sleep_s, (unsigned)c->version);
        /* diagnostics: confirm PA20 USB-detect polarity (bench=USB should read 1)
         * and the best-effort VBAT reading before trusting AUTO/sleep. */
        printf("cfg: usb_present=%d bat_mv=%u\n",
               led_usb_present(), (unsigned)hokku_battery_mv());
        return CMD_STATUS_ACKED;
    }
    if (cmd_strcmp(argv[0], "power") == 0 && argc == 2) {
        if (cmd_strcmp(argv[1], "auto") == 0)
            c->power_mode = HOKKU_PWR_AUTO;
        else if (cmd_strcmp(argv[1], "sleep") == 0)
            c->power_mode = HOKKU_PWR_SLEEP;
        else if (cmd_strcmp(argv[1], "awake") == 0)
            c->power_mode = HOKKU_PWR_AWAKE;
        else
            return CMD_STATUS_INVALID_ARG;
        return CMD_STATUS_OK;
    }
    if (cmd_strcmp(argv[0], "server") == 0 && argc == 2) {
        strncpy(c->server_url, argv[1], HOKKU_URL_MAX - 1);
        c->server_url[HOKKU_URL_MAX - 1] = 0;
        return CMD_STATUS_OK;
    }
    if (cmd_strcmp(argv[0], "name") == 0 && argc == 2) {
        strncpy(c->screen_name, argv[1], HOKKU_NAME_MAX - 1);
        c->screen_name[HOKKU_NAME_MAX - 1] = 0;
        return CMD_STATUS_OK;
    }
    if (cmd_strcmp(argv[0], "ip") == 0 && argc == 4) {
        c->use_dhcp = 0;
        strncpy(c->ip, argv[1], HOKKU_IP_MAX - 1); c->ip[HOKKU_IP_MAX - 1] = 0;
        strncpy(c->gw, argv[2], HOKKU_IP_MAX - 1); c->gw[HOKKU_IP_MAX - 1] = 0;
        strncpy(c->nm, argv[3], HOKKU_IP_MAX - 1); c->nm[HOKKU_IP_MAX - 1] = 0;
        return CMD_STATUS_OK;
    }
    if (cmd_strcmp(argv[0], "dhcp") == 0) {
        c->use_dhcp = 1;
        return CMD_STATUS_OK;
    }
    if (cmd_strcmp(argv[0], "static") == 0) {
        c->use_dhcp = 0;
        return CMD_STATUS_OK;
    }
    if (cmd_strcmp(argv[0], "sleep") == 0 && argc == 2) {
        c->default_sleep_s = (uint32_t)cmd_atoi(argv[1]);
        return CMD_STATUS_OK;
    }
    if (cmd_strcmp(argv[0], "save") == 0) {
        cmd_write_respond(CMD_STATUS_OK, "OK");
        printf(hokku_config_save() == 0 ? "cfg: saved to flash\n" : "cfg: SAVE FAILED\n");
        return CMD_STATUS_ACKED;
    }
    return CMD_STATUS_INVALID_ARG;
}

/*
 * `ota` — trigger an A/B firmware update from the configured server right now.
 * Downloads /hokku/firmware.bin into the inactive slot, flips the boot cfg, and
 * reboots into it (rollback watchdog guards the new image). Used to test the OTA
 * path deterministically without waiting for a server-signalled update.
 */
static enum cmd_status cmd_hokku_ota_exec(char *cmd)
{
    (void)cmd;
    cmd_write_respond(CMD_STATUS_OK, "OK");
    hokku_ota_manual();                  /* reboots on success; returns on failure */
    return CMD_STATUS_ACKED;
}

/*
 * `frame` — upload one ready-made panel buffer over this console and display it.
 * No server, no WiFi, no render pipeline: the host sends exact bytes, the device
 * shows them. For bring-up and colour measurement, where the picture on the
 * glass has to be known precisely. Protocol in firmware/common/all/frame_proto.h;
 * host side is tools/f7_send_frame.py.
 */
static enum cmd_status cmd_frame_exec(char *cmd)
{
    (void)cmd;
    /* No cmd_write_respond here: hokku_frame_receive prints READY itself and
     * then takes the UART, so an extra ACK line would land mid-handshake. */
    hokku_frame_receive();
    return CMD_STATUS_ACKED;
}

/*
 * `interactive on|off` — hand the screen to a host driving it over USB.
 *
 * While on, the refresh thread stops fetching and the device stops hibernating,
 * so the console stays where the host left it. Without this, host-driven work is
 * a race: the refresh loop can hibernate between two uploads and take the console
 * with it, and a host can only poll and hope to land in a gap.
 *
 * Deliberately not persisted. Any reset clears it, so a screen cannot be left
 * mute by a host that crashed or forgot to turn it off — power-cycling is always
 * the way out. It also only takes effect on USB (see interactive.h), so pulling
 * the cable restores normal behaviour rather than draining the battery.
 */
static enum cmd_status cmd_interactive_exec(char *cmd)
{
    char *argv[1];
    int argc = cmd_parse_argv(cmd, argv, cmd_nitems(argv));

    if (argc != 1)
        return CMD_STATUS_INVALID_ARG;

    if (cmd_strcmp(argv[0], "on") == 0) {
        hokku_interactive_set(1);
    } else if (cmd_strcmp(argv[0], "off") == 0) {
        hokku_interactive_set(0);
    } else {
        return CMD_STATUS_INVALID_ARG;
    }

    /* Report engaged state, not just the request: on battery the mode is set but
     * inert, and a host that saw a bare "OK" would think it had the screen. */
    cmd_write_respond(CMD_STATUS_OK, hokku_interactive_engaged(led_usb_present())
                                         ? "OK interactive engaged"
                                         : "OK interactive requested (no USB — inert)");
    return CMD_STATUS_ACKED;
}

static const struct cmd_data g_main_cmds[] = {
    { "net",     cmd_net_exec },
    { "wifi",    cmd_wifi_exec },
    { "cfg",     cmd_cfg_exec },
    { "ota",     cmd_hokku_ota_exec },
    { "frame",   cmd_frame_exec },
    { "upgrade", cmd_upgrade_exec },
    { "interactive", cmd_interactive_exec },
};

void main_cmd_exec(char *cmd)
{
    cmd_main_exec(cmd, g_main_cmds, cmd_nitems(g_main_cmds));
}

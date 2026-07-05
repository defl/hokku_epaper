#include "common/cmd/cmd_util.h"
#include "common/cmd/cmd.h"

/* Defined in main.c: persist WiFi creds to sysinfo and connect. */
extern int hokku_wifi_provision(const char *ssid, const char *psk);

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

static const struct cmd_data g_main_cmds[] = {
    { "net",     cmd_net_exec },
    { "wifi",    cmd_wifi_exec },
    { "upgrade", cmd_upgrade_exec },
};

void main_cmd_exec(char *cmd)
{
    cmd_main_exec(cmd, g_main_cmds, cmd_nitems(g_main_cmds));
}

#include "common/cmd/cmd_util.h"
#include "common/cmd/cmd.h"

static const struct cmd_data g_net_cmds[] = {
    { "sta", cmd_wlan_sta_exec },
};

static enum cmd_status cmd_net_exec(char *cmd)
{
    return cmd_exec(cmd, g_net_cmds, cmd_nitems(g_net_cmds));
}

static const struct cmd_data g_main_cmds[] = {
    { "net",     cmd_net_exec },
    { "upgrade", cmd_upgrade_exec },
};

void main_cmd_exec(char *cmd)
{
    cmd_main_exec(cmd, g_main_cmds, cmd_nitems(g_main_cmds));
}

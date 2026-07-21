#include "clock.h"

#include "kernel/os/os.h"   /* OS_GetTime() — seconds since boot */

/* Anchored to the server epoch captured at the last sync. base==0 => never
 * synced. Good enough for the loop cadence; can later be backed by the RTC
 * across hibernation. */
static uint32_t g_clk_epoch_base;    /* server epoch captured at last sync */
static uint32_t g_clk_uptime_base;   /* OS_GetTime() (secs) at last sync */

void hokku_clock_set(uint32_t server_epoch)
{
    g_clk_epoch_base  = server_epoch;
    g_clk_uptime_base = OS_GetTime();
}

uint32_t hokku_clock_now(void)
{
    if (g_clk_epoch_base == 0)
        return 0;
    return g_clk_epoch_base + (OS_GetTime() - g_clk_uptime_base);
}

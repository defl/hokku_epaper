#include "pm.h"

#include "kernel/os/os.h"
#include "driver/chip/hal_wakeup.h"
#include "driver/chip/hal_wdg.h"
#include "pm/pm.h"

#include "log.h"

void hokku_hibernate(uint32_t sleep_s)
{
    if (sleep_s < HOKKU_HIBERNATE_MIN_S)
        sleep_s = HOKKU_HIBERNATE_MIN_S;
    if (sleep_s > HOKKU_HIBERNATE_MAX_S)
        sleep_s = HOKKU_HIBERNATE_MAX_S;
    hlog("hokku: battery mode — hibernating %u s (wake -> full reboot)\n", (unsigned)sleep_s);
    OS_MSleep(60);                             /* let the log line flush over UART */
    HAL_Wakeup_SetTimer_Sec(sleep_s);
    pm_enter_mode(PM_MODE_HIBERNATION);
    HAL_WDG_Reboot();                          /* unreached on success */
}

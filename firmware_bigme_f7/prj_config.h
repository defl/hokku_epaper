#ifndef _PRJ_CONFIG_H_
#define _PRJ_CONFIG_H_

#ifdef __cplusplus
extern "C" {
#endif

/* main thread stack — needs to handle HTTP + EPD bit-bang */
#define PRJCONF_MAIN_THREAD_STACK_SIZE  (4 * 1024)
#define PRJCONF_MAIN_THREAD_PRIO        OS_THREAD_PRIO_APP

#define PRJCONF_SYS_CTRL_EN             1
#define PRJCONF_SYS_CTRL_PRIO           OS_THREAD_PRIO_SYS_CTRL
#define PRJCONF_SYS_CTRL_STACK_SIZE     (2 * 1024)
#define PRJCONF_SYS_CTRL_QUEUE_LEN      6

#define PRJCONF_IMG_FLASH               0
#define PRJCONF_IMG_ADDR                0x00000000

/* WiFi credentials + server URL stored here */
#define PRJCONF_SYSINFO_SAVE_TO_FLASH   1
#define PRJCONF_SYSINFO_FLASH           0
#define PRJCONF_SYSINFO_ADDR            ((1024 - 4) * 1024)
#define PRJCONF_SYSINFO_SIZE            (4 * 1024)
#define PRJCONF_SYSINFO_CHECK_OVERLAP   1

#define PRJCONF_MAC_ADDR_SOURCE         SYSINFO_MAC_ADDR_CHIPID

/* Watchdog disabled — EPD refresh can take ~30 s */
#define PRJCONF_WDG_EN                  0

/* Hardware */
#define PRJCONF_UART_EN                 1
#define PRJCONF_CE_EN                   1
/* SPI hardware not used (EPD uses GPIO bit-bang) */
#define PRJCONF_SPI_EN                  0
#define PRJCONF_MMC_EN                  0
#define PRJCONF_INTERNAL_SOUNDCARD_EN   0
#define PRJCONF_AC107_SOUNDCARD_EN      0

/* Services */
#define PRJCONF_CONSOLE_EN              1
#define PRJCONF_PM_EN                   0
#define PRJCONF_NET_EN                  1
#define PRJCONF_NET_PM_EN               0
#define PRJCONF_ENV_TZ                  "TZ=UTC"
#define PRJCONF_SWD_EN                  0

#ifdef __cplusplus
}
#endif

#endif /* _PRJ_CONFIG_H_ */

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

/*
 * WiFi credentials (MAC / wlan_mode / sta params) live in the sysinfo fdcm block.
 *
 * The SDK default (1020 KB = 0xFF000) targets a small single-image layout; on this
 * device it lands INSIDE slot 0's image area ([0, img_max_size*1K) = [0, 0x17F000)),
 * so sysinfo_init()'s overlap check returns -1 and sysinfo stays uninitialized —
 * WLAN never starts and `net sta config` can't persist ("sysinfo uninitialized, hdl 0").
 *
 * 0x300000 is the vendor-designated sysinfo partition: the first address past slot 1
 * ([0x181000, 0x300000) with img_max_size 0x5fc K) and clear of the OTA area, so the
 * overlap check passes. The OEM keeps its own sysinfo fdcm here too.
 */
#define PRJCONF_SYSINFO_SAVE_TO_FLASH   1
#define PRJCONF_SYSINFO_FLASH           0
#define PRJCONF_SYSINFO_ADDR            0x300000
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

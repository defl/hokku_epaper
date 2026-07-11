#
# Bigme F7 local build config
#
# HOSC = 40 MHz crystal (confirmed from Bigme F7 hardware analysis)
# XIP  = y  (execute-in-place, reduces SRAM pressure)
#

export __CONFIG_CHIP_TYPE := xr872
export __CONFIG_HOSC_TYPE := 40
export __CONFIG_XIP := y

# lwIP 2.1.2 (SDK default is 1.4.1) — needed for mDNS `.local` resolution
# (LWIP_DNS_SUPPORT_MDNS_QUERIES exists only in lwIP >= 2.0).
export __CONFIG_LWIP_VER := 20102

#
# Bigme F7 local build config
#
# HOSC = 40 MHz crystal (confirmed from Bigme F7 hardware analysis)
# XIP  = y  (execute-in-place, reduces SRAM pressure)
#

export __CONFIG_HOSC_TYPE := 40
export __CONFIG_XIP := y

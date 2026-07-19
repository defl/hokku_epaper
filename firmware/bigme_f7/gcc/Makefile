#
# Hokku Bigme F7 firmware build
#
# Build from this directory with:
#   make [XR872_SDK=/path/to/xr872_sdk]
#
# XR872_SDK defaults to the sibling checkout at ../../../../xr872_sdk
# (i.e. next to hokku_epaper in the workspace).
#

# Path to the xr872_sdk checkout
XR872_SDK ?= $(abspath ../../../../xr872_sdk)

# ----------------------------------------------------------------------------
# project local config (HOSC type, XIP, etc.)
# ----------------------------------------------------------------------------
include localconfig.mk

# ----------------------------------------------------------------------------
# SDK root and common rules
# ----------------------------------------------------------------------------
ROOT_PATH := $(XR872_SDK)

include $(ROOT_PATH)/gcc.mk

# ----------------------------------------------------------------------------
# project identity and source root
# ----------------------------------------------------------------------------
PROJECT      := hokku_bigme_f7
PRJ_ROOT_PATH := $(abspath ..)

# ----------------------------------------------------------------------------
# board: use the generic XR872 EVB AI board config
# (provides board_config.h with clock and UART settings)
# ----------------------------------------------------------------------------
PRJ_BOARD := $(ROOT_PATH)/project/common/board/xr872_evb_ai

# ----------------------------------------------------------------------------
# source collection
# ----------------------------------------------------------------------------
INCLUDE_PATHS += -I$(PRJ_ROOT_PATH)

# SoC-agnostic shared code (firmware/common/all) — pure C, compiled into the F7
# firmware alongside the ESP32 boards. See firmware/common/all/README.md.
HOKKU_COMMON_ALL := $(abspath $(PRJ_ROOT_PATH)/../common/all)
INCLUDE_PATHS    += -I$(HOKKU_COMMON_ALL)

# SoC-shared XR872 code (firmware/common/xr872) — reusable across XR872/XR872AT
# screens (analogous to common/esp32 for the ESP32 boards). Compiled against the
# real XR872 SDK here; host tests compile it separately via cmake mocks.
HOKKU_COMMON_XR872 := $(abspath $(PRJ_ROOT_PATH)/../common/xr872)
INCLUDE_PATHS      += -I$(HOKKU_COMMON_XR872)

DIRS_IGNORE := $(PRJ_ROOT_PATH)/gcc%
DIRS_IGNORE += $(PRJ_ROOT_PATH)/image%
# Host-side unit tests (test_logic.c etc.) — compiled by cmake against XR872
# SDK mocks, NOT by this Makefile against the real SDK; must never be swept
# into the firmware's own source discovery below. This covers both the F7's own
# test dir AND common/all/test (whose files each define main() and #include the
# .c under test — linking them into the firmware would be fatal).
DIRS_IGNORE += $(PRJ_ROOT_PATH)/test%
DIRS_IGNORE += $(HOKKU_COMMON_ALL)/test%
DIRS_IGNORE += $(HOKKU_COMMON_XR872)/test%
DIRS_IGNORE += $(ROOT_PATH)/project/common/board/%
DIRS_IGNORE += $(PRJ_BOARD)

DIRS_ALL := $(shell find $(PRJ_ROOT_PATH) $(HOKKU_COMMON_ALL) $(HOKKU_COMMON_XR872) $(ROOT_PATH)/project/common -type d)
DIRS      := $(filter-out $(DIRS_IGNORE),$(DIRS_ALL))
DIRS      += $(PRJ_BOARD)

SRCS := $(basename $(foreach dir,$(DIRS),$(wildcard $(dir)/*.[csS])))
OBJS := $(addsuffix .o,$(SRCS))

# ----------------------------------------------------------------------------
# image paths — override the project.mk defaults which assume ROOT_PATH is
# relative (the ../$(ROOT_PATH) idiom breaks when ROOT_PATH is absolute)
# IMAGE_CFG_PATH uses ?= so must be set before including project.mk.
# IMAGE_TOOL uses := so is overridden after.
# ----------------------------------------------------------------------------
IMAGE_CFG_PATH := $(ROOT_PATH)/project/image_cfg

# ----------------------------------------------------------------------------
# project rules
# ----------------------------------------------------------------------------
include $(PRJ_MAKE_RULES)

# Override IMAGE_TOOL now that MKIMAGE is defined (set in gcc.mk above).
IMAGE_TOOL := $(ROOT_PATH)/tools/$(MKIMAGE)

# Strip debug info — DWARF is stripped by objcopy anyway (not in .bin),
# but removing it speeds up compilation and shrinks intermediate artifacts.
# DBG_FLAG uses recursive (=) assignment in gcc.mk so overriding here works.
DBG_FLAG :=

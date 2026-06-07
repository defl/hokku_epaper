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

DIRS_IGNORE := $(PRJ_ROOT_PATH)/gcc%
DIRS_IGNORE += $(PRJ_ROOT_PATH)/image%
DIRS_IGNORE += $(ROOT_PATH)/project/common/board/%
DIRS_IGNORE += $(PRJ_BOARD)

DIRS_ALL := $(shell find $(PRJ_ROOT_PATH) $(ROOT_PATH)/project/common -type d)
DIRS      := $(filter-out $(DIRS_IGNORE),$(DIRS_ALL))
DIRS      += $(PRJ_BOARD)

SRCS := $(basename $(foreach dir,$(DIRS),$(wildcard $(dir)/*.[csS])))
OBJS := $(addsuffix .o,$(SRCS))

# ----------------------------------------------------------------------------
# project rules
# ----------------------------------------------------------------------------
include $(PRJ_MAKE_RULES)

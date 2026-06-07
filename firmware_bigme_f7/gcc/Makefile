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

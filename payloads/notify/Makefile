# ---------------------------------------------------------------------------
# PS4 Manager - build the "connected" notification payload
#
#   PS4_PAYLOAD_SDK=/path/to/ps4-payload-sdk make          # -> notify.elf
#
# The SDK (a single download, no installation) and the compiler it needs:
#
#   https://github.com/ps4-payload-dev/sdk/releases/latest/download/ps4-payload-sdk.zip
#   sudo apt install clang-18 lld-18 llvm-18               # the SDK's wrappers
#                                                          # look for LLVM 18+
# Unzip it, then either set PS4_PAYLOAD_SDK or keep it at ~/ps4-payload-sdk.
#
# The flags keep the payload small: -Oz, one section per function/data so the
# linker can drop whatever is unused, no unwind tables (nothing unwinds inside
# a payload, and they were a quarter of the file), and everything stripped.
# ---------------------------------------------------------------------------

PS4_PAYLOAD_SDK ?= $(HOME)/ps4-payload-sdk

ifeq ($(wildcard $(PS4_PAYLOAD_SDK)/toolchain/orbis.mk),)
$(error PS4_PAYLOAD_SDK does not look like the payload SDK: set it to the \
folder holding toolchain/orbis.mk (default: ~/ps4-payload-sdk))
endif

include $(PS4_PAYLOAD_SDK)/toolchain/orbis.mk

# The SDK's linker wrapper reads $PS4_PAYLOAD_SDK from the environment to find
# ldscripts/elf_x86_64.x; orbis.mk only sets a make variable, so export it.
export PS4_PAYLOAD_SDK

# -nodefaultlibs drops the C library and the extra Sce libs the wrapper links
# by default; -lkernel is the stub table that resolves the kernel call on the
# console (and stops the wrapper from adding its own kernel lib).
# The app sends ../notify.elf (next to this folder), so building it here puts the
# payload exactly where the manager looks for it.
PAYLOAD := ../notify.elf
CFLAGS  := -Oz -Wall -Wextra -ffunction-sections -fdata-sections \
           -fno-asynchronous-unwind-tables -fno-unwind-tables
LDFLAGS := -nodefaultlibs -lkernel -Wl,--gc-sections -Wl,--strip-all

all: $(PAYLOAD)

$(PAYLOAD): main.c
	$(CC) $(CFLAGS) main.c -o $@ $(LDFLAGS)
	@printf '%s: %s byte(s)\n' "$(PAYLOAD)" "$$(wc -c < $(PAYLOAD))"

clean:
	rm -f $(PAYLOAD)
	@ls -l $(PAYLOAD) 2>/dev/null || echo "$(PAYLOAD): removed"

.PHONY: all clean

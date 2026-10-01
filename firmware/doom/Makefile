# ChromatiX Doom (--with-doom build, LiteX bare metal app).
#   make BUILD_DIR=../../build
#   ../../scripts/chromatic.py run doom.bin --wad doom1.wad

BUILD_DIR?=../../build/

include $(BUILD_DIR)/software/include/generated/variables.mak
include $(SOC_DIRECTORY)/software/common.mak
include sources.mak

# Libraries (compiled by chromatix.py --with-doom --build: no BIOS).
LIBS = libc libcompiler_rt libbase

OBJDIR  = build
OBJECTS = $(OBJDIR)/crt0.o $(addprefix $(OBJDIR)/, $(addsuffix .o, $(DOOM_SRCS) $(PORT_SRCS) hal_litex syscalls console))

# Doom: -O2 (common.mak: -Os), builtins (memcpy/memset inlining), no warnings for the vendored code.
DOOM_OPT = -O2 -fbuiltin -w

all: doom.bin

%.bin: %.elf
	$(OBJCOPY) -O binary $< $@
	chmod -x $@

doom.elf: $(OBJECTS)
	$(CC) $(LDFLAGS) -T ../common/main_ram.ld -N -o $@ \
		$(OBJECTS) \
		$(PACKAGES:%=-L$(BUILD_DIR)/software/%) \
		-Wl,--start-group $(LIBS:lib%=-l%) -Wl,--end-group \
		-Wl,--gc-sections \
		-Wl,-Map,$@.map
	chmod -x $@

vpath %.c doomgeneric . ../common

$(OBJDIR)/crt0.o: $(CPU_DIRECTORY)/crt0.S | $(OBJDIR)
	$(CC) -c $(CFLAGS) -o $@ $<

$(OBJDIR)/%.o: %.c sources.mak Makefile | $(OBJDIR)
	$(CC) -c $(CFLAGS) $(DOOM_CFLAGS) $(DOOM_OPT) -o $@ $<

$(OBJDIR):
	mkdir -p $@

-include $(OBJECTS:.o=.d)

clean:
	rm -rf $(OBJDIR) doom.elf doom.elf.map doom.bin

.PHONY: all clean

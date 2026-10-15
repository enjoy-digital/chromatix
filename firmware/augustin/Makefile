# ChromatiX Augustin game (--with-voxel bitstream, LiteX bare metal app).
#   make BUILD_DIR=../../build
#   ../../scripts/chromatic.py run augustin.bin

BUILD_DIR?=../../build/

include $(BUILD_DIR)/software/include/generated/variables.mak
include $(SOC_DIRECTORY)/software/common.mak
include sources.mak

# Libraries (compiled by chromatix.py --with-app --build: no BIOS).
LIBS = libc libcompiler_rt libbase

OBJDIR  = build
OBJECTS = $(OBJDIR)/crt0.o $(addprefix $(OBJDIR)/, $(addsuffix .o, $(AUGUSTIN_SRCS) hal_litex syscalls console))

# -O2 (common.mak: -Os), builtins (memcpy/memset inlining).
AUGUSTIN_OPT = -O2 -fbuiltin

all: augustin.bin

%.bin: %.elf
	$(OBJCOPY) -O binary $< $@
	chmod -x $@

augustin.elf: $(OBJECTS)
	$(CC) $(LDFLAGS) -T ../common/main_ram.ld -N -o $@ \
		$(OBJECTS) \
		$(PACKAGES:%=-L$(BUILD_DIR)/software/%) \
		-Wl,--start-group $(LIBS:lib%=-l%) -Wl,--end-group \
		-Wl,--gc-sections \
		-Wl,-Map,$@.map
	chmod -x $@

vpath %.c . ../voxel ../common

$(OBJDIR)/crt0.o: $(CPU_DIRECTORY)/crt0.S | $(OBJDIR)
	$(CC) -c $(CFLAGS) -o $@ $<

$(OBJDIR)/%.o: %.c sources.mak Makefile | $(OBJDIR)
	$(CC) -c $(CFLAGS) $(AUGUSTIN_CFLAGS) $(AUGUSTIN_OPT) -o $@ $<

$(OBJDIR):
	mkdir -p $@

-include $(OBJECTS:.o=.d)

clean:
	rm -rf $(OBJDIR) augustin.elf augustin.elf.map augustin.bin

.PHONY: all clean

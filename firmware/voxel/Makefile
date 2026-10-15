# ChromatiX voxel game (--with-app/--with-voxel build, LiteX bare metal app).
#   make BUILD_DIR=../../build
#   ../../scripts/chromatic.py run voxel.bin

BUILD_DIR?=../../build/

include $(BUILD_DIR)/software/include/generated/variables.mak
include $(SOC_DIRECTORY)/software/common.mak
include sources.mak

# Libraries (compiled by chromatix.py --with-app --build: no BIOS).
LIBS = libc libcompiler_rt libbase

OBJDIR  = build
OBJECTS = $(OBJDIR)/crt0.o $(addprefix $(OBJDIR)/, $(addsuffix .o, $(VOXEL_SRCS) hal_litex syscalls console))

# -O2 (common.mak: -Os), builtins (memcpy/memset inlining).
VOXEL_OPT = -O2 -fbuiltin

all: voxel.bin

%.bin: %.elf
	$(OBJCOPY) -O binary $< $@
	chmod -x $@

voxel.elf: $(OBJECTS)
	$(CC) $(LDFLAGS) -T ../common/main_ram.ld -N -o $@ \
		$(OBJECTS) \
		$(PACKAGES:%=-L$(BUILD_DIR)/software/%) \
		-Wl,--start-group $(LIBS:lib%=-l%) -Wl,--end-group \
		-Wl,--gc-sections \
		-Wl,-Map,$@.map
	chmod -x $@

vpath %.c . ../common

$(OBJDIR)/crt0.o: $(CPU_DIRECTORY)/crt0.S | $(OBJDIR)
	$(CC) -c $(CFLAGS) -o $@ $<

$(OBJDIR)/%.o: %.c sources.mak Makefile | $(OBJDIR)
	$(CC) -c $(CFLAGS) $(VOXEL_CFLAGS) $(VOXEL_OPT) -o $@ $<

$(OBJDIR):
	mkdir -p $@

-include $(OBJECTS:.o=.d)

clean:
	rm -rf $(OBJDIR) voxel.elf voxel.elf.map voxel.bin

.PHONY: all clean

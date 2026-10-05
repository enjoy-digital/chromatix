# ChromatiX SDR application (--with-app build + ESP-SDR on the ESP32, LiteX bare metal app).
#   make BUILD_DIR=../../build (ChromatiX --with-app build directory)

BUILD_DIR?=../../build/

include $(BUILD_DIR)/software/include/generated/variables.mak
include $(SOC_DIRECTORY)/software/common.mak

CFLAGS += -I../common

# Libraries (compiled by chromatix.py --with-app --build: no BIOS).
LIBS = libc libcompiler_rt libbase

OBJECTS = crt0.o main.o esp32sdr.o lcd.o dsp.o usblink.o console.o lte.o ble.o classify.o dect.o zigbee.o \
	tool_cell.o tool_ble.o tool_signals.o tool_dect.o tool_zigbee.o

COMMA = ,
REGULAR_LIBS = $(filter-out $(ALWAYS_LINK_LIBS),$(LIBS))
ALWAYS_LINK_LIBFLAGS = $(if $(ALWAYS_LINK_LIBS),-Wl$(COMMA)--whole-archive $(ALWAYS_LINK_LIBS:lib%=-l%) -Wl$(COMMA)--no-whole-archive)

all: sdr.bin

%.bin: %.elf
	$(OBJCOPY) -O binary $< $@
	chmod -x $@

vpath %.a $(PACKAGES:%=../%)

sdr.elf: $(OBJECTS)
	$(CC) $(LDFLAGS) -T ../common/main_ram.ld -N -o $@ \
		$(OBJECTS) \
		$(PACKAGES:%=-L$(BUILD_DIR)/software/%) \
		-Wl,--start-group \
		$(REGULAR_LIBS:lib%=-l%) \
		$(ALWAYS_LINK_LIBFLAGS) \
		-Wl,--end-group \
		-Wl,--gc-sections \
		-Wl,-Map,$@.map
	chmod -x $@

-include $(OBJECTS:.o=.d)

VPATH = $(BIOS_DIRECTORY):$(BIOS_DIRECTORY)/cmds:$(CPU_DIRECTORY):../common

# DSP: optimized for speed (the default -Os stalls on the load/multiply latencies).
DSP_OBJECTS = dsp.o lte.o ble.o classify.o dect.o zigbee.o
$(DSP_OBJECTS): CFLAGS += -O3 -funroll-loops

%.o: %.c
	$(compile)

%.o: %.S
	$(assemble)

clean:
	$(RM) $(OBJECTS) $(OBJECTS:.o=.d) sdr.elf sdr.elf.map sdr.bin

.PHONY: all clean

include ../../variables.mak
CFLAGS  += $(shell sdl2-config --cflags)
LDFLAGS += -Wl,--no-as-needed $(shell sdl2-config --libs) -ljson-c
include $(SRC_DIR)/modules/rules.mak

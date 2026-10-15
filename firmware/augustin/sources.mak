# Augustin game sources, shared by Makefile (SoC) and Makefile.host. The renderer model, HALs and
# syscalls are the voxel game's (firmware/voxel).

AUGUSTIN_SRCS   = main world art sound font vr
AUGUSTIN_CFLAGS = -I. -I../voxel -I../common

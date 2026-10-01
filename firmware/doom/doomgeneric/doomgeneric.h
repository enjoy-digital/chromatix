#ifndef DOOM_GENERIC
#define DOOM_GENERIC

#include <stdlib.h>
#include <stdint.h>

#ifndef DOOMGENERIC_RESX
#define DOOMGENERIC_RESX 640
#endif  // DOOMGENERIC_RESX

#ifndef DOOMGENERIC_RESY
#define DOOMGENERIC_RESY 400
#endif  // DOOMGENERIC_RESY


#ifdef CMAP256

typedef uint8_t pixel_t;

#else  // CMAP256

typedef uint32_t pixel_t;

#endif  // CMAP256


extern pixel_t* DG_ScreenBuffer;

#ifdef __cplusplus
extern "C" {
#endif

void doomgeneric_Create(int argc, char **argv);
void doomgeneric_Tick();


//Implement below functions for your platform
void DG_Init();
void DG_DrawFrame();
void DG_SleepMs(uint32_t ms);
uint32_t DG_GetTicksMs();
int DG_GetKey(int* pressed, unsigned char* key);
void DG_SetWindowTitle(const char * title);

#ifdef DOOMGENERIC_DIRECT
// Direct mode: DG_DrawFrame reads I_VideoBuffer (320x200 indexed, no DG_ScreenBuffer copy) and
// the palette (256 x RGB, gamma applied) is given to DG_SetPalette.
void DG_SetPalette(const uint8_t *rgb);
#endif

#ifdef DOOMGENERIC_TIMEDEMO_HOOK
// Timedemo results (benchmark), called before the report/exit.
void DG_TimedemoDone(int gametics, int realtics);
#endif

#ifdef DOOMGENERIC_MEMWAD
// Memory WADs: DG_MemWAD returns the WAD data (and its size) for a path, or NULL.
const uint8_t *DG_MemWAD(const char *path, unsigned int *size);
#endif

#ifdef __cplusplus
}
#endif

#endif //DOOM_GENERIC

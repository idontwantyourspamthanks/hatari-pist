/*
  hatari-pist - media channel (spike)
  Copyright (C) 2026 PiST contributors

  This program is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 2 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.
*/

/*
  The PiST media channel: with --pist-media <port>, Hatari runs windowless
  and pushes each converted frame over a localhost TCP connection to the IDE
  (which listens; Hatari connects, like --control-socket), and receives input
  back on the same socket.
  Spike protocol (little-endian):
    Hatari->IDE FRAME: u32 magic 'FRM1', u32 width, u32 height, u32 pitch,
                       u32 seq, u32 bpp, u32 rmask, u32 gmask, u32 bmask,
                       then pitch*height bytes of pixels.
    IDE->Hatari KEY:   u8 'K', u8 ST scancode, u8 down (1) / up (0).
*/
#ifndef HATARI_PISTMEDIA_H
#define HATARI_PISTMEDIA_H

#include <SDL.h>
#include <stdbool.h>
#include <stdint.h>

extern const char *PistMedia_SetPort(const char *port);
extern bool PistMedia_Enabled(void);
/* call after the frame is converted: w/h crop to the ST screen area
 * (excludes the statusbar strip), changed = bScreenContentsChanged */
extern void PistMedia_PushFrame(SDL_Surface *surface, int w, int h, bool changed);
/* call once per VBL after Sound_Update(): the span of newly mixed stereo
 * s16 samples in the ring is streamed as an AUDIO message. indexReset =
 * Sound_BufferIndexNeedReset: a reset makes any span stale, so it resyncs. */
extern void PistMedia_PushAudio(const int16_t (*ring)[2], int writePos, int ringPow2, bool indexReset);
extern void PistMedia_PollInput(void);
extern void PistMedia_Quit(void);

#endif /* HATARI_PISTMEDIA_H */

/*
 * Hatari - paltrace.h
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 */

#ifndef HATARI_PALTRACE_H
#define HATARI_PALTRACE_H

#include <stdbool.h>
#include <stdint.h>

extern const char PalTrace_Description[];
extern bool PalTrace_Active;

extern char *PalTrace_Match(const char *text, int state);
extern int PalTrace_Command(int nArgc, char *psArgs[]);

/* Called from Video_ColorReg_WriteWord once the masked color is known. */
extern void PalTrace_Note(uint32_t addr, uint16_t color);

#endif /* HATARI_PALTRACE_H */

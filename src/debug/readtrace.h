/*
 * Hatari - readtrace.h
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 */

#ifndef HATARI_READTRACE_H
#define HATARI_READTRACE_H

#include <stdbool.h>
#include <stdint.h>

extern const char ReadTrace_Description[];
extern bool ReadTrace_Active;

extern char *ReadTrace_Match(const char *text, int state);
extern int ReadTrace_Command(int nArgc, char *psArgs[]);
extern void ReadTrace_NoteSlow(uint32_t addr, int width, uint32_t value, uint32_t pc);

/*
 * Hot path for a CPU data read. Inactive is one load and a return.
 * Instruction fetches do not call this. The PC is the instruction's,
 * passed by the caller, never regs.pc filled in afterwards.
 */
static inline void ReadTrace_Note(uint32_t addr, int width, uint32_t value, uint32_t pc)
{
	if (!ReadTrace_Active)
		return;
	ReadTrace_NoteSlow(addr, width, value, pc);
}

extern void ReadTrace_DebugEnter(void);
extern void ReadTrace_DebugLeave(void);

#endif /* HATARI_READTRACE_H */

/*
 * Hatari - writetrace.h
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 */

#ifndef HATARI_WRITETRACE_H
#define HATARI_WRITETRACE_H

#include <stdbool.h>
#include <stdint.h>

extern const char WriteTrace_Description[];
extern bool WriteTrace_Active;

extern char *WriteTrace_Match(const char *text, int state);
extern int WriteTrace_Command(int nArgc, char *psArgs[]);

/* Origin is a tag the writer pushes. A missing tag is unknown and has no PC. */
extern void WriteTrace_PushCpu(uint32_t pc);
extern void WriteTrace_PushBlitter(void);
extern void WriteTrace_Pop(void);
extern void WriteTrace_ResetTags(void);

extern void WriteTrace_NoteSlow(uint32_t addr, int width, uint32_t value);

/*
 * Hot path for every store. Inactive is one load and a return; the
 * record itself is WriteTrace_NoteSlow.
 */
static inline void WriteTrace_Note(uint32_t addr, int width, uint32_t value)
{
	if (!WriteTrace_Active)
		return;
	WriteTrace_NoteSlow(addr, width, value);
}

/* Host time of one debugger continue, off then on, for the cost line. */
extern void WriteTrace_DebugEnter(void);
extern void WriteTrace_DebugLeave(void);

#endif /* HATARI_WRITETRACE_H */

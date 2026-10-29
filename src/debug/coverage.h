/*
 * Hatari - coverage.h
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 */

#ifndef HATARI_COVERAGE_H
#define HATARI_COVERAGE_H

#include <stdbool.h>

extern const char Coverage_Description[];

extern char *Coverage_Match(const char *text, int state);
extern int Coverage_Command(int nArgc, char *psArgs[]);

/* True while 'coverage on' is recording. Counts survive 'coverage off'. */
extern bool Coverage_IsRecording(void);

/*
 * Count the instruction that just finished. Called from DebugCpu_Check
 * after the PC has advanced, same timing as Profile_CpuUpdate.
 */
extern void Coverage_Update(void);

#endif /* HATARI_COVERAGE_H */

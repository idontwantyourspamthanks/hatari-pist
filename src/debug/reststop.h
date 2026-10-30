/*
 * Hatari - reststop.h
 *
 * This file is distributed under the GNU General Public License, version 2
 * or at your option any later version. Read the file gpl.txt for details.
 */

#ifndef HATARI_RESTSTOP_H
#define HATARI_RESTSTOP_H

#include <stdbool.h>

extern const char RestStop_Description[];

extern int RestStop_Command(int nArgc, char *psArgs[]);

/*
 * True when reststop has scheduled a restore that the CPU loop must
 * leave before another instruction. Does not clear the request.
 */
extern bool RestStop_LeaveLoop(void);

/*
 * After that restore, and before the CPU runs: enter the debugger.
 * Returns true when this prompt scheduled another restore or quit, so
 * the caller must not execute the restored instruction.
 */
extern bool RestStop_EnterPrompt(void);

#endif /* HATARI_RESTSTOP_H */

/*
  Hatari - remotedebug.h

  This file is distributed under the GNU General Public License, version 2
  or at your option any later version. Read the file gpl.txt for details.
*/

#ifndef HATARI_REMOTE_H
#define HATARI_REMOTE_H

#include <stdbool.h>

extern void RemoteDebug_Init(void);
extern void RemoteDebug_UnInit(void);
extern bool RemoteDebug_Update(void);
// Read the flag to see if remote break was requested
extern void RemoteDebug_CheckRemoteBreak(void);
#endif /* HATARI_REMOTE_H */

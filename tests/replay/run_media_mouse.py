#!/usr/bin/env python3
"""Record one media-channel mouse message and replay it.

The recording is stopped at vertical blank 25, after the IKBD has started
sending relative packets, and the message is sent at that prompt. The next
screen poll stamps it on frame 26. The fixture reads the packet into D1
(dx), D2 (dy) and D3 (button bits). Replay matches. A different dx diverges.
"""

import os
import re
import sys
import tempfile

import run_media_key as media
import run_stop_recording as recording

STOP_VBL = 25
FRAME = STOP_VBL + 1
DX = 4
DY = -2
BUTTONS = 1  # left: the IKBD header puts that in bit 1, so D3 becomes 2


def saw_packet(lines):
    needle_d1 = "d1 %08x" % (DX & 0xFF)
    needle_d2 = "d2 %08x" % (DY & 0xFF)
    needle_d3 = "d3 %08x" % 2
    return any(needle_d1 in line and needle_d2 in line and needle_d3 in line for line in lines)


def main():
    if len(sys.argv) != 3:
        recording.fail("usage: run_media_mouse.py <hatari> <mouse.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        recording.fail("not executable: %s" % hatari)

    with tempfile.TemporaryDirectory(prefix="hatari-replay-mouse-") as home:
        rec = os.path.join(home, "rec.txt")
        other = os.path.join(home, "other.txt")
        stop_parse = os.path.join(home, "stop.txt")
        keep_parse = os.path.join(home, "keep.txt")
        with open(stop_parse, "w", encoding="ascii") as fh:
            fh.write("b pc = $1\n")
            fh.write("b VBL = %d :once :quiet\n" % STOP_VBL)
        with open(keep_parse, "w", encoding="ascii") as fh:
            fh.write("b pc = $1\n")

        channel = media.Media()
        port = recording.hold_hrdb_port()
        try:
            session = media.Session(hatari, prg, home, stop_parse, rec, channel.port)
            try:
                session.wait_prompt()
                vbl = media.vbl_of(session.cmd("e VBL"))
                if vbl != STOP_VBL:
                    recording.fail("stopped at VBL %d, expected %d" % (vbl, STOP_VBL),
                                   session.stdout, session.stderr)
                channel.send_mouse(DX, DY, BUTTONS)
                code = session.continue_to_end()
                if code != 0 or b"replay recorded\n" not in session.stdout:
                    recording.fail("recording did not complete (exit %s)" % code,
                                   session.stdout, session.stderr)
            finally:
                session.close()
        finally:
            port.close()
            channel.close()

        expect = "mouse %d %d %d %d" % (FRAME, DX, DY, BUTTONS)
        got = [line for line in open(rec, encoding="ascii") if line.startswith("mouse ")]
        got = [line.rstrip("\n") for line in got]
        if got != [expect]:
            recording.fail("mouse was logged as %s, expected %s" % (got, expect))
        traces = recording.trace_lines(rec)
        if not saw_packet(traces):
            recording.fail("guest did not read dx=%d dy=%d left button" % (DX, DY))

        matched = recording.run_batch(
            hatari, prg, home,
            ["--replay", rec, "--parse", keep_parse],
            False)
        if matched.returncode != 0 or b"replay matched\n" not in matched.stdout:
            recording.fail("replay of the mouse message did not match", matched.stdout, matched.stderr)

        lines = open(rec, encoding="ascii").read().splitlines()
        replaced = [("mouse %d %d %d %d" % (FRAME, DX + 1, DY, BUTTONS)) if line == expect else line
                    for line in lines]
        with open(other, "w", encoding="ascii") as fh:
            fh.write("\n".join(replaced) + "\n")
        changed = recording.run_batch(
            hatari, prg, home,
            ["--replay", other, "--parse", keep_parse],
            False)
        text = changed.stdout.decode("latin-1", errors="replace")
        found = re.findall(r"^replay diverged at frame (\d+)$", text, re.M)
        if changed.returncode == 0 or len(found) != 1:
            recording.fail("changed dx did not diverge", changed.stdout, changed.stderr)
        frame = int(found[0])
        if frame <= FRAME or frame > recording.RUN_VBLS:
            recording.fail("changed dx diverged at frame %d" % frame, changed.stdout, changed.stderr)

    print("pass media-mouse frame=%d dx=%d dy=%d buttons=%d diverged=%d" % (FRAME, DX, DY, BUTTONS, frame))


if __name__ == "__main__":
    main()

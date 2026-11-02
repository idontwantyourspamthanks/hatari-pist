#!/usr/bin/env python3
"""Record from the entry stop of a real TOS boot, then replay that snapshot.

The image is the one PiST would pick for an ST. No floppy, no GEMDOS host
time, fast boot left off. The debugger stops the way PiST does, on
`pc = TEXT && pc < $e00000`, with one breakpoint that cannot match so the
debugger flag stays set. replayrecord snapshots that unexecuted first
instruction and records the next vertical blanks. reststop then replayplay
must match. A different ROM hash is refused.
"""

import hashlib
import os
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FRAMES = 40
SKIP = 77
SPIN_TAIL = 5
ENTRY_OP = 0x7201  # moveq #1,d1
PROMPT = b"> "
PROMPT_QUIET = 0.05

TRACE_D = re.compile(
    r"^trace \d+ pc ([0-9a-f]{8}) sr [0-9a-f]{4}"
    r"(?: d\d [0-9a-f]{8})* d1 ([0-9a-f]{8}) d2 ([0-9a-f]{8}) d3 ([0-9a-f]{8})"
)


def fail(msg, out=b"", err=b""):
    print("FAILED: " + msg, file=sys.stderr)
    if out:
        sys.stderr.buffer.write(b"---- stdout ----\n" + out[-4000:] + b"\n")
    if err:
        sys.stderr.buffer.write(b"---- stderr ----\n" + err[-4000:] + b"\n")
    sys.exit(1)


def rom_header(path):
    data = path.read_bytes()
    if len(data) < 0x30:
        return None
    version = int.from_bytes(data[2:4], "big")
    emutos = data[0x2C:0x30] == b"ETOS"
    return version, emutos


def st_accepts(version, emutos):
    if emutos:
        return True
    return 0x0100 <= version < 0x0106


def find_rom():
    dirs = []
    override = os.environ.get("PIST_TOS_DIR")
    if override:
        dirs.append(Path(override))
    dirs.append(Path.home() / ".local" / "share" / "PiST" / "roms")
    xdg = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    for base in xdg.split(":"):
        if base:
            dirs.append(Path(base) / "hatari")
    best = None
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.suffix.lower() not in {".img", ".rom", ".tos"}:
                continue
            header = rom_header(path)
            if header is None:
                continue
            version, emutos = header
            if version < 0x0104 or not st_accepts(version, emutos):
                continue
            if best is None or version > best[0]:
                best = (version, path)
    return best


def hold_hrdb_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 56001))
    except OSError as exc:
        fail("cannot hold 127.0.0.1:56001 (%s)" % exc)
    sock.listen(1)
    return sock


class Session:
    def __init__(self, hatari, tos, prg, home, parse_path):
        self.stdout = b""
        self.stderr = b""
        self.mark = 0
        env = os.environ.copy()
        env["HOME"] = str(home)
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["HATARI_TEST"] = "replay"
        args = [
            hatari,
            "--log-level", "error",
            "--sound", "off",
            "--fast-forward", "on",
            "--confirm-quit", "off",
            "--machine", "st",
            "--cpulevel", "0",
            "--cpuclock", "8",
            "--compatible", "on",
            "--cpu-exact", "on",
            "--data-cache", "off",
            "--addr24", "on",
            "--fpu", "none",
            "--mmu", "off",
            "--blitter", "off",
            "--memsize", "1024",
            "--ttram", "0",
            "--video-timing", "ws3",
            "--monitor", "rgb",
            "--borders", "off",
            "--frameskips", "0",
            "--gemdos-time", "atari",
            "--tos", str(tos),
            "--joy0", "none",
            "--joy1", "none",
            "--joy2", "none",
            "--joy3", "none",
            "--joy4", "none",
            "--joy5", "none",
            "--parse", str(parse_path),
            str(prg),
        ]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, bufsize=0,
        )

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def _read(self, timeout):
        streams = []
        if self.proc.stdout is not None:
            streams.append(self.proc.stdout)
        if self.proc.stderr is not None:
            streams.append(self.proc.stderr)
        if not streams:
            return False
        ready, _, _ = select.select(streams, [], [], timeout)
        got = False
        for stream in ready:
            chunk = os.read(stream.fileno(), 65536)
            got = True
            if stream is self.proc.stdout:
                self.stdout += chunk
            else:
                self.stderr += chunk
            if chunk == b"":
                if stream is self.proc.stdout:
                    self.proc.stdout.close()
                    self.proc.stdout = None
                else:
                    self.proc.stderr.close()
                    self.proc.stderr = None
        return got

    def wait_prompt(self, timeout=90):
        deadline = time.monotonic() + timeout
        quiet_at = None
        while time.monotonic() < deadline:
            if b"Remote Debug Listening" in self.stdout or b"Remote Debug Listening" in self.stderr:
                fail("HRDB is listening; it must not be what stops the CPU", self.stdout, self.stderr)
            remain = deadline - time.monotonic()
            got = self._read(min(PROMPT_QUIET, max(0.0, remain)))
            if self.stderr.endswith(PROMPT):
                if got or quiet_at is None:
                    quiet_at = time.monotonic()
                elif time.monotonic() - quiet_at >= PROMPT_QUIET:
                    text = self.stderr[self.mark:]
                    self.mark = len(self.stderr)
                    if text.endswith(PROMPT):
                        text = text[:-len(PROMPT)]
                    return text.decode("latin-1", "replace")
            else:
                quiet_at = None
            if self.proc.poll() is not None and self.proc.stderr is None:
                break
        fail("debugger prompt did not return", self.stdout, self.stderr)

    def cmd(self, line, timeout=30):
        if self.proc.poll() is not None:
            fail("hatari exited before command %r" % line, self.stdout, self.stderr)
        self.proc.stdin.write((line + "\n").encode("ascii"))
        self.proc.stdin.flush()
        return self.wait_prompt(timeout)

    def continue_to_end(self, timeout=60):
        if self.proc.poll() is not None:
            fail("hatari exited before continue", self.stdout, self.stderr)
        self.proc.stdin.write(b"c\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._read(0.05)
            if self.proc.poll() is not None:
                while self._read(0.05):
                    pass
                return self.proc.returncode
        fail("recording did not finish after continue", self.stdout, self.stderr)


def parse_stop(text):
    regs = {}
    for name, value in re.findall(r"\b([DA][0-7]) ([0-9A-F]{8})\b", text):
        regs[name] = int(value, 16)
    dis = re.findall(r"^([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{4})\s+(.*)$", text, re.M)
    if not dis:
        raise RuntimeError("stop has no disassembly\n" + text)
    pc, opcode, insn = dis[-1]
    return {
        "pc": int(pc, 16),
        "opcode": int(opcode, 16),
        "insn": insn.strip(),
        "d1": regs.get("D1"),
        "d2": regs.get("D2"),
    }


def trace_lines(path):
    lines = []
    for line in Path(path).read_text(encoding="ascii", errors="replace").splitlines():
        if line.startswith("trace "):
            lines.append(line)
    return lines


def parse_spin(line):
    match = TRACE_D.match(line)
    if not match:
        return None
    return (match.group(1), int(match.group(2), 16), int(match.group(3), 16), int(match.group(4), 16))


def main():
    if len(sys.argv) != 3:
        fail("usage: run_tos_entry.py <hatari> <tosboot.prg>")
    hatari = sys.argv[1]
    source = Path(sys.argv[2])
    if not os.access(hatari, os.X_OK):
        fail("hatari is not executable")
    if not source.is_file() or source.stat().st_size != 36:
        fail("tosboot.prg is missing or not 36 bytes")

    found = find_rom()
    if found is None:
        print("skipped: no autostart-capable ST TOS image")
        sys.exit(SKIP)
    version, tos = found
    digest = hashlib.sha256(tos.read_bytes()).hexdigest()

    home = Path(tempfile.mkdtemp(prefix="replay-tos-entry-"))
    port = hold_hrdb_port()
    try:
        prg_dir = home / "prg"
        prg_dir.mkdir()
        prg = prg_dir / "tosboot.prg"
        shutil.copyfile(source, prg)
        parse = home / "dbg.ini"
        parse.write_text("b pc = $1\nb pc = TEXT && pc < $e00000 :once :quiet\n")
        rec = home / "rec.txt"
        sav = home / "rec.txt.sav"

        recorded = Session(hatari, tos, prg, home, parse)
        try:
            recorded.wait_prompt(120)
            stopped = parse_stop(recorded.cmd("r"))
            if stopped["pc"] >= 0xE00000 or stopped["opcode"] != ENTRY_OP:
                fail("entry stop is not the unexecuted moveq: pc=%08X op=%04X insn=%s"
                     % (stopped["pc"], stopped["opcode"], stopped["insn"]),
                     recorded.stdout, recorded.stderr)
            armed = recorded.cmd("replayrecord %s %d" % (rec, FRAMES), timeout=30)
            if "replayrecord: %d frames" % FRAMES not in armed:
                fail("replayrecord did not arm\n" + armed, recorded.stdout, recorded.stderr)
            code = recorded.continue_to_end()
            if code != 0 or b"replay recorded\n" not in recorded.stdout:
                fail("record exited %s" % code, recorded.stdout, recorded.stderr)
        finally:
            recorded.close()

        if not sav.is_file():
            fail("snapshot was not written beside the recording")
        text = rec.read_text(encoding="ascii", errors="replace")
        lines = text.splitlines()
        for line in ("tos " + digest, "floppy no", "gemdos_hosttime no",
                     "gemdos_hd yes", "debugger yes", "frames %d" % FRAMES):
            if line not in lines:
                fail("recording is missing %s" % line)
        traces = trace_lines(rec)
        if len(traces) != FRAMES:
            fail("trace count is %d" % len(traces))
        tail = [parse_spin(line) for line in traces[-SPIN_TAIL:]]
        if any(row is None or row[1] != 1 or row[2] != 2 for row in tail):
            fail("program did not stay in the spin\n" + traces[-1])
        if any(tail[i][3] >= tail[i + 1][3] for i in range(SPIN_TAIL - 1)):
            fail("spin counter did not advance\n" + "\n".join(traces[-SPIN_TAIL:]))
        if any(int(row[0], 16) >= 0xE00000 for row in tail):
            fail("spin pc is still in ROM\n" + traces[-1])

        replayed = Session(hatari, tos, prg, home, parse)
        try:
            replayed.wait_prompt(120)
            replayed.cmd("reststop %s" % sav, timeout=30)
            restored = parse_stop(replayed.cmd("r"))
            if restored["pc"] != stopped["pc"] or restored["opcode"] != ENTRY_OP:
                fail("reststop is not the same unexecuted moveq: pc=%08X op=%04X"
                     % (restored["pc"], restored["opcode"]),
                     replayed.stdout, replayed.stderr)
            played = replayed.cmd("replayplay %s" % rec, timeout=30)
            if "replayplay: %d frames" % FRAMES not in played:
                fail("replayplay did not arm\n" + played, replayed.stdout, replayed.stderr)
            code = replayed.continue_to_end()
            if code != 0 or b"replay matched\n" not in replayed.stdout:
                fail("replay exited %s" % code, replayed.stdout, replayed.stderr)
        finally:
            replayed.close()

        bad = home / "bad.txt"
        rewritten = []
        flipped = False
        for line in text.splitlines(keepends=True):
            if not flipped and line.startswith("tos ") and not line.startswith("tos none"):
                rewritten.append(line[:-2] + ("0\n" if line[-2] != "0" else "1\n"))
                flipped = True
            else:
                rewritten.append(line)
        bad.write_text("".join(rewritten))
        rejected = Session(hatari, tos, prg, home, parse)
        try:
            rejected.wait_prompt(120)
            rejected.cmd("reststop %s" % sav, timeout=30)
            answer = rejected.cmd("replayplay %s" % bad, timeout=30)
            if "replay rejected: tos" not in rejected.stdout.decode("latin-1", "replace"):
                fail("a different ROM hash was not refused\n" + answer,
                     rejected.stdout, rejected.stderr)
        finally:
            rejected.close()

        print("pass tos-entry version=%x pc=%08x frames=%d rom=%s"
              % (version, stopped["pc"], FRAMES, tos))
    finally:
        port.close()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    main()

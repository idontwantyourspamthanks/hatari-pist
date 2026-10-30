#!/usr/bin/env python3
"""Prove paltrace records shifter palette writes on Hatari's scanline clock.

One process arms the log, continues once to the spin, and checks the two
move.w events. A second process must agree on every event field. A palette
write while paltrace is off must not appear.
"""

import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import time

ENTRY = 0x1100
COLOR50 = 0x0700
COLOR60 = 0x0070
COLOR_OFF = 0x0007
REG = 0xFF8240
LINE50 = 512
LINE60 = 508

PROMPT = b"> "
PROMPT_QUIET = 0.05

EVENT_RE = re.compile(
    r"event (?P<seq>\d+) pc (?P<pc>[0-9a-f]{8}) reg (?P<reg>[0-9a-f]{6}) "
    r"value (?P<value>[0-9a-f]{4}) vbl (?P<vbl>-?\d+) hbl (?P<hbl>-?\d+) "
    r"pos (?P<pos>-?\d+) frame (?P<frame>-?\d+)\Z"
)
FIELDS = ("seq", "pc", "reg", "value", "vbl", "hbl", "pos", "frame")


class DumpError(Exception):
    pass


def fail(msg, stdout=b"", stderr=b""):
    print("FAIL: " + msg, file=sys.stderr)
    if stdout:
        print("---- stdout ----", file=sys.stderr)
        sys.stderr.write(stdout.decode("latin-1", "replace")[-4000:])
        if not stdout.endswith(b"\n"):
            print(file=sys.stderr)
    if stderr:
        print("---- stderr ----", file=sys.stderr)
        sys.stderr.write(stderr.decode("latin-1", "replace")[-8000:])
        if not stderr.endswith(b"\n"):
            print(file=sys.stderr)
    sys.exit(1)


def hold_hrdb_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 56001))
    except OSError as exc:
        fail("cannot hold 127.0.0.1:56001 (%s); HRDB would own the debugger" % exc)
    sock.listen(1)
    return sock


def load_fixture(path):
    data = open(path, "rb").read()
    if len(data) < 0x1C + 8 or data[0:2] != b"\x60\x1a":
        fail("fixture is not a TOS PRG: %s" % path)
    text = int.from_bytes(data[2:6], "big")
    if text < 8 or 0x1C + text > len(data):
        fail("fixture text size %d does not fit the file" % text)
    body = data[0x1C:0x1C + text]
    if body[:6] != bytes.fromhex("31fc0200820a"):
        fail("entry is not move.w #$0200,$ff820a: %s" % body[:6].hex())

    def find_one(pattern, label):
        found = []
        start = 0
        while True:
            at = body.find(pattern, start)
            if at < 0:
                break
            found.append(at)
            start = at + 1
        if len(found) != 1:
            fail("fixture %s occurs %d times, expected 1" % (label, len(found)))
        return ENTRY + found[0]

    write50 = find_one(bytes.fromhex("31fc07008240"), "move.w #$0700,$ff8240")
    write60 = find_one(bytes.fromhex("31fc00708240"), "move.w #$0070,$ff8240")
    off = find_one(bytes.fromhex("31fc00078240"), "move.w #$0007,$ff8240")
    if not (ENTRY < write50 < write60 < off):
        fail("palette moves are out of order: %x %x %x" % (write50, write60, off))

    def find_bra(after):
        at = body.find(b"\x60\xfe", after - ENTRY)
        if at < 0:
            fail("bra.s spin missing after $%x" % after)
        return ENTRY + at

    spin = find_bra(write60)
    off_done = find_bra(off)
    if not (write60 < spin < off < off_done):
        fail("spin $%x and off_done $%x are not around the writes" % (spin, off_done))
    return {
        "write50": write50,
        "write60": write60,
        "off": off,
        "spin": spin,
        "off_done": off_done,
    }


def show_event(ev):
    if ev is None:
        return "<none>"
    return ("event %d pc %08x reg %06x value %04x vbl %d hbl %d pos %d frame %d"
            % (ev["seq"], ev["pc"], ev["reg"], ev["value"],
               ev["vbl"], ev["hbl"], ev["pos"], ev["frame"]))


def parse_event(line):
    match = EVENT_RE.fullmatch(line)
    if not match:
        raise DumpError("event line does not match the log format: %s" % line)
    ev = {name: int(match.group(name), 16 if name in ("pc", "reg", "value") else 10)
          for name in FIELDS}
    ev["line"] = line
    return ev


def parse_log(path):
    lines = open(path, "r", encoding="ascii").read().splitlines()
    if not lines or lines[0] != "HPISTPAL 1":
        raise DumpError("missing HPISTPAL 1 in %s" % path)
    events = []
    for line in lines[1:]:
        if not line.startswith("event "):
            raise DumpError("log line is not an event: %s" % line)
        events.append(parse_event(line))
    return events


def first_mismatch(got, expect):
    count = min(len(got), len(expect))
    for i in range(count):
        for field in FIELDS:
            if got[i][field] != expect[i][field]:
                return i, field, got[i], expect[i]
    if len(got) != len(expect):
        if len(got) > len(expect):
            return len(expect), "seq", got[len(expect)], None
        return len(got), "seq", None, expect[len(got)]
    return None


def require_events(label, got, expect):
    bad = first_mismatch(got, expect)
    if not bad:
        return
    index, field, g, e = bad
    if g is None:
        fail("%s event %d missing field %s: expected %s" % (label, e["seq"], field, show_event(e)))
    if e is None:
        fail("%s event %d field %s: got %s, expected end of log" % (label, g["seq"], field, show_event(g)))
    fail("%s event %d field %s: got %s expected %s" % (
        label, g["seq"], field, show_event(g), show_event(e)))


def parse_regs(text):
    regs = {}
    for name, value in re.findall(r"\b([DA][0-7]) ([0-9A-F]{8})\b", text):
        regs[name] = int(value, 16)
    sr = re.findall(r"SR=([0-9A-F]{4})", text)
    dis = re.findall(r"^([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{4})\s+(.*)$", text, re.M)
    nxt = re.findall(r"Next PC: ([0-9A-Fa-f]{8})", text)
    if not sr or not dis or not nxt:
        raise DumpError("register dump is incomplete\n%s" % text)
    pc, opcode, insn = dis[-1]
    return {
        "sr": int(sr[-1], 16),
        "pc": int(pc, 16),
        "opcode": int(opcode, 16),
        "insn": insn.strip(),
        "next": int(nxt[-1], 16),
        "regs": regs,
    }


def parse_eval(text):
    if "ERROR" in text:
        raise DumpError("evaluate failed\n%s" % text)
    found = re.findall(r"\$([0-9A-Fa-f]+) \(hex\)", text)
    if not found:
        raise DumpError("evaluate value missing\n%s" % text)
    return int(found[-1], 16)


def parse_word(text, addr):
    found = re.findall(r"^%08X: ([0-9A-Fa-f]{4})\b" % addr, text, re.M)
    if not found:
        raise DumpError("word at $%x missing from memdump\n%s" % (addr, text))
    return int(found[-1], 16)


def stopped_pc(text):
    found = re.findall(r"CPU=\$([0-9a-fA-F]+)", text)
    if not found:
        raise DumpError("continue did not report CPU=\n%s" % text)
    return int(found[-1], 16)


class Session:
    def __init__(self, hatari, prg, home, parse_path):
        self.stdout = b""
        self.stderr = b""
        self.mark = 0
        env = os.environ.copy()
        env["HATARI_TEST"] = "paltrace"
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["HOME"] = home
        args = [
            hatari,
            "--log-level", "error",
            "--sound", "off",
            "--confirm-quit", "off",
            "--machine", "st",
            "--cpulevel", "0",
            "--cpuclock", "8",
            "--compatible", "on",
            "--cpu-exact", "on",
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
            "--tos", "none",
            "--vdi", "off",
            "--joy0", "none",
            "--joy1", "none",
            "--joy2", "none",
            "--joy3", "none",
            "--joy4", "none",
            "--joy5", "none",
            "--parse", parse_path,
            prg,
        ]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=0,
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

    def wait_prompt(self, timeout=30):
        deadline = time.monotonic() + timeout
        quiet_at = None
        while time.monotonic() < deadline:
            if b"Remote Debug Listening" in self.stdout or b"Remote Debug Listening" in self.stderr:
                fail("HRDB is listening; it must not be what stops the CPU",
                     self.stdout, self.stderr)
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
                    return text.decode("latin-1")
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

    def quit(self):
        if self.proc.poll() is None and self.proc.stdin is not None:
            try:
                self.proc.stdin.write(b"q\n")
                self.proc.stdin.flush()
            except BrokenPipeError:
                pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return self.proc.returncode
            self._read(0.1)
        self.close()
        fail("quit did not leave the debugger", self.stdout, self.stderr)


def check_timing(events, where, stop_vbl, stop_frame):
    if len(events) != 2:
        fail("%s log has %d events, expected 2" % (where, len(events)))
    low, high = events
    for ev, length, end in ((low, LINE50, LINE50 - 20), (high, LINE60, LINE60 - 20)):
        if not (0 <= ev["pos"] < length):
            fail("%s event %d field pos: %d is outside 0..%d" % (where, ev["seq"], ev["pos"], length - 1))
        if not (20 <= ev["pos"] <= end):
            fail("%s event %d field pos: %d is within 20 cycles of either end of the %d-cycle line"
                 % (where, ev["seq"], ev["pos"], length))
        if ev["hbl"] < 0 or ev["frame"] < 0 or ev["vbl"] < 0:
            fail("%s event %d field hbl: vbl %d hbl %d pos %d frame %d" % (
                where, ev["seq"], ev["vbl"], ev["hbl"], ev["pos"], ev["frame"]))
        if ev["frame"] != ev["hbl"] * length + ev["pos"]:
            if ev["pos"] != ev["frame"] % length:
                fail("%s event %d field pos: frame %d is not hbl %d * %d + pos %d" % (
                    where, ev["seq"], ev["frame"], ev["hbl"], length, ev["pos"]))
            fail("%s event %d field hbl: frame %d is not hbl %d * %d + pos %d" % (
                where, ev["seq"], ev["frame"], ev["hbl"], length, ev["pos"]))
    if low["hbl"] == high["hbl"]:
        fail("%s event 2 field hbl: both writes are on hbl %d" % (where, low["hbl"]))
    if high["hbl"] == high["frame"] // LINE50 and high["pos"] == high["frame"] % LINE50:
        fail("%s event 2 field hbl: 60 Hz log satisfies frame/512 (hbl %d pos %d frame %d)" % (
            where, high["hbl"], high["pos"], high["frame"]))
    if high["vbl"] <= low["vbl"]:
        fail("%s event 2 field vbl: %d is not a later VBL than %d" % (where, high["vbl"], low["vbl"]))
    for ev in events:
        if ev["vbl"] >= stop_vbl:
            fail("%s event %d field vbl: %d is not earlier than the stop VBL %d" % (
                where, ev["seq"], ev["vbl"], stop_vbl))
        if ev["frame"] == stop_frame:
            fail("%s event %d field frame: %d is the stop FrameCycles, sampled at the prompt" % (
                where, ev["seq"], ev["frame"]))
    if stop_frame < high["frame"] + 2048:
        fail("%s event 2 field frame: stop FrameCycles %d is not at least 2048 greater than %d" % (
            where, stop_frame, high["frame"]))


def run_once(hatari, prg, home, parse_path, log_path, off_path, sites):
    session = Session(hatari, prg, home, parse_path)
    try:
        session.wait_prompt()
        help_text = session.cmd("h paltrace")
        if "Command 'paltrace' not found." in help_text or "paltrace" not in help_text:
            fail("paltrace is not a debugger command\n" + help_text, session.stdout, session.stderr)
        unknown = session.cmd("nosuchcmd")
        if "Command 'nosuchcmd' not found." not in unknown:
            fail("unknown command text changed\n" + unknown, session.stdout, session.stderr)

        entered = parse_regs(session.cmd("r"))
        if entered["pc"] != ENTRY:
            fail("entry PC is $%x, expected $%x" % (entered["pc"], ENTRY), session.stdout, session.stderr)
        if entered["sr"] != 0x0300:
            fail("SR is %04X before paltrace; fake TOS must stay user mode $0300" % entered["sr"],
                 session.stdout, session.stderr)

        session.cmd("r SR=$2300")
        supervised = parse_regs(session.cmd("r"))
        if supervised["sr"] != 0x2300 or supervised["pc"] != ENTRY:
            fail("SR set moved the stop to pc=$%x sr=%04X" % (supervised["pc"], supervised["sr"]),
                 session.stdout, session.stderr)

        session.cmd("paltrace on")
        armed = parse_regs(session.cmd("r"))
        if armed["pc"] != ENTRY or armed["sr"] != 0x2300:
            fail("paltrace on moved the stop to pc=$%x sr=%04X" % (armed["pc"], armed["sr"]),
                 session.stdout, session.stderr)

        session.cmd("b pc=$%x :once :quiet" % sites["spin"])
        landed = session.cmd("c", timeout=60)
        pc = stopped_pc(landed)
        if pc != sites["spin"]:
            fail("continue stopped at $%x, expected the spin breakpoint $%x" % (pc, sites["spin"]),
                 session.stdout, session.stderr)
        state = parse_regs(session.cmd("r"))
        if state["pc"] != sites["spin"] or "bra" not in state["insn"].lower():
            fail("spin stop is pc=$%x insn=%s; paltrace must not be why the CPU stopped"
                 % (state["pc"], state["insn"]), session.stdout, session.stderr)

        stop_vbl = parse_eval(session.cmd("e VBL"))
        stop_frame = parse_eval(session.cmd("e FrameCycles"))
        session.cmd("paltrace save %s" % log_path)
        events = parse_log(log_path)
        expect = [
            {
                "seq": 1, "pc": sites["write50"], "reg": REG, "value": COLOR50,
                "vbl": None, "hbl": None, "pos": None, "frame": None,
            },
            {
                "seq": 2, "pc": sites["write60"], "reg": REG, "value": COLOR60,
                "vbl": None, "hbl": None, "pos": None, "frame": None,
            },
        ]
        if len(events) != 2:
            shown = events[2] if len(events) > 2 else None
            if shown is not None:
                fail("spin event %d field seq: got %s, expected end of log" % (shown["seq"], show_event(shown)))
            fail("spin log has %d events, expected 2" % len(events))
        for i, known in enumerate(("seq", "pc", "reg", "value")):
            for ev, exp in zip(events, expect):
                if ev[known] != exp[known]:
                    fail("spin event %d field %s: got %s expected %s" % (
                        ev["seq"], known, show_event(ev),
                        "pc %08x reg %06x value %04x" % (exp["pc"], exp["reg"], exp["value"])))
        check_timing(events, "spin", stop_vbl, stop_frame)

        color = parse_word(session.cmd("m w $%x 1" % REG), REG) & 0x777
        if color != COLOR60:
            fail("color 0 at the spin is $%04x, expected $%04x" % (color, COLOR60),
                 session.stdout, session.stderr)

        session.cmd("paltrace off")
        session.cmd("b pc=$%x :once :quiet" % sites["off_done"])
        session.cmd("r PC=$%x" % sites["off"])
        off_landed = session.cmd("c", timeout=30)
        off_pc = stopped_pc(off_landed)
        if off_pc != sites["off_done"]:
            fail("disarmed continue stopped at $%x, expected $%x" % (off_pc, sites["off_done"]),
                 session.stdout, session.stderr)
        session.cmd("paltrace save %s" % off_path)
        off_events = parse_log(off_path)
        require_events("disarmed", off_events, events)
        off_color = parse_word(session.cmd("m w $%x 1" % REG), REG) & 0x777
        if off_color != COLOR_OFF:
            fail("palette write while paltrace is off did not reach $ff8240 ($%04x)" % off_color,
                 session.stdout, session.stderr)

        code = session.quit()
        if code != 0:
            fail("hatari quit with status %s" % code, session.stdout, session.stderr)
        return {
            "events": events,
            "stop_vbl": stop_vbl,
            "stop_frame": stop_frame,
        }
    except DumpError as exc:
        fail(str(exc), session.stdout, session.stderr)
    finally:
        session.close()


def agree(first, second):
    bad = first_mismatch(first, second)
    if not bad:
        return
    index, field, g, e = bad
    if g is None:
        fail("second process event %d missing field %s: expected %s" % (e["seq"], field, show_event(e)))
    if e is None:
        fail("second process event %d field %s: got %s, expected end of log" % (g["seq"], field, show_event(g)))
    fail("second process event %d field %s: got %s expected %s" % (
        g["seq"], field, show_event(g), show_event(e)))


def main():
    if len(sys.argv) != 3:
        fail("usage: run_test.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        fail("not executable: %s" % hatari)
    sites = load_fixture(prg)

    port = hold_hrdb_port()
    try:
        with tempfile.TemporaryDirectory(prefix="hatari-paltrace-") as home:
            parse_path = os.path.join(home, "break.txt")
            with open(parse_path, "w", encoding="ascii") as fh:
                fh.write("b pc=$%x :once :quiet\n" % ENTRY)
            first = run_once(hatari, prg, home, parse_path,
                             os.path.join(home, "pal1.log"),
                             os.path.join(home, "off1.log"), sites)
            second = run_once(hatari, prg, home, parse_path,
                              os.path.join(home, "pal2.log"),
                              os.path.join(home, "off2.log"), sites)
    finally:
        port.close()

    agree(first["events"], second["events"])
    low, high = first["events"]
    print("pass")
    print("event 1 pc %08x value %04x vbl %d hbl %d pos %d frame %d" % (
        low["pc"], low["value"], low["vbl"], low["hbl"], low["pos"], low["frame"]))
    print("event 2 pc %08x value %04x vbl %d hbl %d pos %d frame %d" % (
        high["pc"], high["value"], high["vbl"], high["hbl"], high["pos"], high["frame"]))
    print("FrameCycles %d" % first["stop_frame"])
    print("second process agrees on every event field")


if __name__ == "__main__":
    main()

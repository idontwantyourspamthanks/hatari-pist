#!/usr/bin/env python3
"""Prove writetrace records CPU, blitter, and unknown stores as they happen.

One process arms a range, pokes a byte, continues to a spin breakpoint, fills
a 4-deep ring, then times the same guest loop with tracing off and on. A
second process must agree on every event line and the drop count. The two
host times are printed and not compared.
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
SPIN = 0x116E
SIX = 0x1170
SIX_DONE = 0x11A0
COST = 0x11A2
COST_LOOP = 0x11A4
COST_DONE = 0x11AE
RANGE_LO = 0x30000
RANGE_HI = 0x3000F
OUTSIDE = 0x30010
SOURCE = 0xBEEF
POKE = 0x5A
CPU_NEW = 0xA5
DEFAULT_CAPACITY = 256
RING_CAPACITY = 4
COST_WRITES = 64

PROMPT = b"> "
PROMPT_QUIET = 0.05

EVENT_RE = re.compile(
    r"event (?P<seq>\d+) (?P<origin>cpu|blitter|unknown) (?P<width>[124]) "
    r"(?P<addr>[0-9a-f]{8}) (?P<old>[0-9a-f]+) (?P<new>[0-9a-f]+)"
    r"(?: pc (?P<pc>[0-9a-f]{8}))?\Z"
)
FIELDS = ("seq", "origin", "width", "addr", "old", "new", "pc")


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


def prg_ok(path):
    data = open(path, "rb").read()
    if len(data) < 0x1C + 0xB0 or data[0:2] != b"\x60\x1a":
        fail("fixture is not a TOS PRG: %s" % path)
    text = int.from_bytes(data[2:6], "big")
    if text != 0xB0:
        fail("fixture text size is %d, expected 176" % text)
    body = data[0x1C:0x1C + text]
    checks = {
        0x00: bytes.fromhex("13fc00a500030000"),
        0x6E: bytes.fromhex("60fe"),
        0x70: bytes.fromhex("13fc001100030000"),
        0xA0: bytes.fromhex("60fe"),
        0xA2: bytes.fromhex("703f"),
        0xA4: bytes.fromhex("13c000030000"),
        0xAA: bytes.fromhex("51c8fff8"),
        0xAE: bytes.fromhex("60fe"),
    }
    for off, expect in checks.items():
        got = body[off:off + len(expect)]
        if got != expect:
            fail("fixture at $%x is %s, expected %s" % (ENTRY + off, got.hex(), expect.hex()))


def show_event(ev):
    if ev is None:
        return "<none>"
    pc = " pc %08x" % ev["pc"] if ev["pc"] is not None else ""
    width = ev["width"]
    return "event %d %s %d %08x %0*x %0*x%s" % (
        ev["seq"], ev["origin"], width, ev["addr"],
        width * 2, ev["old"], width * 2, ev["new"], pc)


def parse_event(line):
    match = EVENT_RE.fullmatch(line)
    if not match:
        raise DumpError("event line does not match the log format: %s" % line)
    width = int(match.group("width"))
    old = match.group("old")
    new = match.group("new")
    if len(old) != width * 2 or len(new) != width * 2:
        raise DumpError("event %s field width: value digits do not match width %d" % (match.group("seq"), width))
    origin = match.group("origin")
    pc = match.group("pc")
    if origin == "cpu" and pc is None:
        raise DumpError("event %s field pc: cpu event has no pc" % match.group("seq"))
    if origin != "cpu" and pc is not None:
        raise DumpError("event %s field pc: %s event has pc %s" % (match.group("seq"), origin, pc))
    return {
        "seq": int(match.group("seq")),
        "origin": origin,
        "width": width,
        "addr": int(match.group("addr"), 16),
        "old": int(old, 16),
        "new": int(new, 16),
        "pc": int(pc, 16) if pc is not None else None,
        "line": line,
    }


def parse_log(path):
    lines = open(path, "r", encoding="ascii").read().splitlines()
    if not lines or lines[0] != "HPISTWRITE 1":
        raise DumpError("missing HPISTWRITE 1 in %s" % path)
    range_m = re.fullmatch(r"range ([0-9a-f]{8}) ([0-9a-f]{8})", lines[1] if len(lines) > 1 else "")
    cap_m = re.fullmatch(r"capacity (\d+)", lines[2] if len(lines) > 2 else "")
    drop_m = re.fullmatch(r"dropped (\d+)", lines[3] if len(lines) > 3 else "")
    if not range_m or not cap_m or not drop_m:
        raise DumpError("log header is not range/capacity/dropped:\n%s" % "\n".join(lines[:4]))
    events = []
    idx = 4
    while idx < len(lines) and lines[idx].startswith("event "):
        events.append(parse_event(lines[idx]))
        idx += 1
    if idx >= len(lines):
        raise DumpError("cost line missing from %s" % path)
    cost_m = re.fullmatch(r"cost off (\d+) on (\d+) writes (\d+)", lines[idx])
    if not cost_m or idx != len(lines) - 1:
        raise DumpError("cost line is not last, or has extra text, in %s: %s" % (path, lines[idx:]))
    return {
        "range": (int(range_m.group(1), 16), int(range_m.group(2), 16)),
        "capacity": int(cap_m.group(1)),
        "dropped": int(drop_m.group(1)),
        "events": events,
        "cost_off": int(cost_m.group(1)),
        "cost_on": int(cost_m.group(2)),
        "cost_writes": int(cost_m.group(3)),
    }


def first_mismatch(got, expect):
    """Return (index, field, got_event, expect_event) for the first difference."""
    count = min(len(got), len(expect))
    for i in range(count):
        for field in FIELDS:
            if got[i][field] != expect[i][field]:
                return i, field, got[i], expect[i]
    if len(got) != len(expect):
        if len(got) > len(expect):
            return len(expect), "origin", got[len(expect)], None
        return len(got), "origin", None, expect[len(got)]
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


def expect_spin():
    return [
        event(1, "unknown", 1, 0x30004, 0x00, POKE, None),
        event(2, "cpu", 1, RANGE_LO, 0x00, CPU_NEW, ENTRY),
        event(3, "blitter", 2, 0x30008, 0x0000, SOURCE, None),
    ]


def expect_drop():
    olds = (CPU_NEW, 0x00, 0xBE, 0xEF)
    news = (0x11, 0x22, 0x33, 0x44)
    out = []
    for i in range(4):
        out.append(event(i + 1, "cpu", 1, RANGE_LO + i, olds[i], news[i], SIX + i * 8))
    return out


def expect_cost():
    out = []
    old = 0
    seq = 1
    for new in range(COST_WRITES - 1, -1, -1):
        out.append(event(seq, "cpu", 1, RANGE_LO, old, new, COST_LOOP))
        old = new
        seq += 1
    return out


def event(seq, origin, width, addr, old, new, pc):
    return {
        "seq": seq,
        "origin": origin,
        "width": width,
        "addr": addr,
        "old": old,
        "new": new,
        "pc": pc,
    }


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


def parse_word(text, addr):
    found = re.findall(r"^%08X: ([0-9A-Fa-f]{4})\b" % addr, text, re.M)
    if not found:
        raise DumpError("RAM word at $%x missing from memdump\n%s" % (addr, text))
    return int(found[0], 16)


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
        env["HATARI_TEST"] = "writetrace"
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["HOME"] = home
        args = [
            hatari,
            "--log-level", "error",
            "--sound", "off",
            "--confirm-quit", "off",
            "--machine", "ste",
            "--cpulevel", "0",
            "--cpuclock", "8",
            "--compatible", "on",
            "--cpu-exact", "on",
            "--data-cache", "off",
            "--addr24", "on",
            "--fpu", "none",
            "--mmu", "off",
            "--blitter", "on",
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


def require_stop(text, addr, kind):
    pc = stopped_pc(text)
    if pc != addr:
        fail("continue stopped at $%x, expected the %s breakpoint $%x" % (pc, kind, addr))
    state = parse_regs(text) if "SR=" in text else None
    return pc, state


def run_once(hatari, prg, home, parse_path, logs):
    session = Session(hatari, prg, home, parse_path)
    try:
        session.wait_prompt()
        help_text = session.cmd("h writetrace")
        if "Command 'writetrace' not found." in help_text or "writetrace" not in help_text:
            fail("writetrace is not a debugger command\n" + help_text, session.stdout, session.stderr)
        unknown = session.cmd("nosuchcmd")
        if "Command 'nosuchcmd' not found." not in unknown:
            fail("unknown command text changed\n" + unknown, session.stdout, session.stderr)

        entered = parse_regs(session.cmd("r"))
        if entered["pc"] != ENTRY:
            fail("entry PC is $%x, expected $%x" % (entered["pc"], ENTRY), session.stdout, session.stderr)
        if entered["sr"] != 0x0300:
            fail("SR is %04X before writetrace; fake TOS must stay user mode $0300" % entered["sr"],
                 session.stdout, session.stderr)
        dis = session.cmd("d $%x" % ENTRY)
        if "move" not in dis.lower() or "a5" not in dis.lower():
            fail("instruction at $1100 is not the move.b of $A5\n" + dis, session.stdout, session.stderr)

        session.cmd("r SR=$2300")
        supervised = parse_regs(session.cmd("r"))
        if supervised["sr"] != 0x2300 or supervised["pc"] != ENTRY:
            fail("SR set moved the stop to pc=$%x sr=%04X" % (supervised["pc"], supervised["sr"]),
                 session.stdout, session.stderr)

        # Plant the source word before arming so that store is not an event.
        session.cmd("w w $%x $%x" % (0x30002, SOURCE))
        session.cmd("b pc=$%x :once :quiet" % SPIN)
        session.cmd("writetrace $%x $%x" % (RANGE_LO, RANGE_HI))
        session.cmd("w b $%x $%x" % (0x30004, POKE))

        landed = session.cmd("c")
        pc, _ = require_stop(landed, SPIN, "spin")
        state = parse_regs(session.cmd("r"))
        if state["pc"] != SPIN or "bra" not in state["insn"].lower():
            fail("spin stop is pc=$%x insn=%s; writetrace must not be why the CPU stopped"
                 % (state["pc"], state["insn"]), session.stdout, session.stderr)
        if state["pc"] != pc:
            fail("register PC $%x disagrees with the stop at $%x" % (state["pc"], pc))
        dest = parse_word(session.cmd("m w $%x" % 0x30008), 0x30008)
        if dest != SOURCE:
            fail("blitter destination word is %04X, expected %04X" % (dest, SOURCE),
                 session.stdout, session.stderr)

        session.cmd("writetrace save %s" % logs["spin"])
        spin = parse_log(logs["spin"])
        if spin["range"] != (RANGE_LO, RANGE_HI):
            fail("spin log range is %08x %08x" % spin["range"])
        if spin["capacity"] != DEFAULT_CAPACITY:
            fail("spin log capacity is %d, expected the default %d" % (spin["capacity"], DEFAULT_CAPACITY))
        if spin["dropped"] != 0:
            fail("spin log dropped %d, expected 0" % spin["dropped"])
        require_events("spin", spin["events"], expect_spin())
        for ev in spin["events"]:
            if ev["addr"] == OUTSIDE:
                fail("spin event %d field addr: got %08x, a store outside the range" % (ev["seq"], ev["addr"]))
            if ev["pc"] == SPIN:
                fail("spin event %d field pc: the spin instruction is recorded as a writer" % ev["seq"])

        session.cmd("writetrace capacity %d" % RING_CAPACITY)
        session.cmd("writetrace $%x $%x" % (RANGE_LO, RANGE_HI))
        session.cmd("b pc=$%x :once :quiet" % SIX_DONE)
        session.cmd("r PC=$%x" % SIX)
        six_landed = session.cmd("c")
        require_stop(six_landed, SIX_DONE, "six")
        six_state = parse_regs(session.cmd("r"))
        if six_state["pc"] != SIX_DONE or "bra" not in six_state["insn"].lower():
            fail("six stop is pc=$%x insn=%s" % (six_state["pc"], six_state["insn"]),
                 session.stdout, session.stderr)
        session.cmd("writetrace save %s" % logs["drop"])
        drop = parse_log(logs["drop"])
        if drop["capacity"] != RING_CAPACITY:
            fail("drop log capacity is %d, expected %d" % (drop["capacity"], RING_CAPACITY))
        if drop["dropped"] != 2:
            fail("drop log dropped %d, expected 2" % drop["dropped"])
        require_events("drop", drop["events"], expect_drop())

        # Disarmed run must not add events. The same loop is then run armed.
        session.cmd("writetrace off")
        session.cmd("b pc=$%x :once :quiet" % COST_DONE)
        session.cmd("r PC=$%x" % COST)
        off_landed = session.cmd("c")
        require_stop(off_landed, COST_DONE, "cost off")
        session.cmd("writetrace save %s" % logs["off"])
        off = parse_log(logs["off"])
        if off["dropped"] != 2:
            fail("disarmed run changed dropped to %d" % off["dropped"])
        require_events("disarmed", off["events"], expect_drop())

        session.cmd("writetrace capacity %d" % DEFAULT_CAPACITY)
        session.cmd("writetrace $%x $%x" % (RANGE_LO, RANGE_HI))
        session.cmd("b pc=$%x :once :quiet" % COST_DONE)
        session.cmd("r PC=$%x" % COST)
        on_landed = session.cmd("c")
        require_stop(on_landed, COST_DONE, "cost on")
        session.cmd("writetrace save %s" % logs["cost"])
        cost = parse_log(logs["cost"])
        if cost["dropped"] != 0:
            fail("cost log dropped %d, expected 0" % cost["dropped"])
        if cost["cost_writes"] != COST_WRITES:
            fail("cost line writes %d, expected %d" % (cost["cost_writes"], COST_WRITES))
        require_events("cost", cost["events"], expect_cost())

        code = session.quit()
        if code != 0:
            fail("hatari quit with status %s" % code, session.stdout, session.stderr)
        return {
            "spin_events": [ev["line"] for ev in spin["events"]],
            "spin_dropped": spin["dropped"],
            "drop_events": [ev["line"] for ev in drop["events"]],
            "drop_dropped": drop["dropped"],
            "cost_events": [ev["line"] for ev in cost["events"]],
            "cost_dropped": cost["dropped"],
            "cost_off": cost["cost_off"],
            "cost_on": cost["cost_on"],
            "cost_writes": cost["cost_writes"],
            "unknown": spin["events"][0]["line"],
            "cpu": spin["events"][1]["line"],
            "blitter": spin["events"][2]["line"],
        }
    except DumpError as exc:
        fail(str(exc), session.stdout, session.stderr)
    finally:
        session.close()


def agree(label, first, second):
    if first[label + "_dropped"] != second[label + "_dropped"]:
        fail("%s dropped disagrees: first %d second %d" % (
            label, first[label + "_dropped"], second[label + "_dropped"]))
    a = first[label + "_events"]
    b = second[label + "_events"]
    if a != b:
        count = min(len(a), len(b))
        for i in range(count):
            if a[i] != b[i]:
                ga = parse_event(a[i])
                gb = parse_event(b[i])
                for field in FIELDS:
                    if ga[field] != gb[field]:
                        fail("second process %s event %d field %s: got %s expected %s" % (
                            label, ga["seq"], field, show_event(ga), show_event(gb)))
                fail("second process %s event line disagrees:\n%s\n%s" % (label, a[i], b[i]))
        fail("second process %s event count %d disagrees with %d" % (label, len(b), len(a)))


def main():
    if len(sys.argv) != 3:
        fail("usage: run_test.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        fail("not executable: %s" % hatari)
    prg_ok(prg)

    port = hold_hrdb_port()
    try:
        with tempfile.TemporaryDirectory(prefix="hatari-writetrace-") as home:
            parse_path = os.path.join(home, "break.txt")
            with open(parse_path, "w", encoding="ascii") as fh:
                fh.write("b pc=$%x :once :quiet\n" % ENTRY)
            first = run_once(hatari, prg, home, parse_path, {
                "spin": os.path.join(home, "spin1.log"),
                "drop": os.path.join(home, "drop1.log"),
                "off": os.path.join(home, "off1.log"),
                "cost": os.path.join(home, "cost1.log"),
            })
            second = run_once(hatari, prg, home, parse_path, {
                "spin": os.path.join(home, "spin2.log"),
                "drop": os.path.join(home, "drop2.log"),
                "off": os.path.join(home, "off2.log"),
                "cost": os.path.join(home, "cost2.log"),
            })
    finally:
        port.close()

    agree("spin", first, second)
    agree("drop", first, second)
    agree("cost", first, second)
    if first["cost_writes"] != second["cost_writes"]:
        fail("cost writes disagree: %d vs %d" % (first["cost_writes"], second["cost_writes"]))

    print("pass")
    print("unknown: %s" % first["unknown"])
    print("cpu: %s" % first["cpu"])
    print("blitter: %s" % first["blitter"])
    print("dropped: %d" % first["drop_dropped"])
    print("cost off %d on %d writes %d" % (first["cost_off"], first["cost_on"], first["cost_writes"]))
    print("second process agrees on every event line and the drop count")


if __name__ == "__main__":
    main()

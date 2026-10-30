#!/usr/bin/env python3
"""Restore one statesave snapshot and stop before its instruction.

Two full runs must agree. The snapshot is a statesave file. reststop is a
debugger command, probed by name. HRDB must not be the stop: this process
holds 127.0.0.1:56001 so the listener cannot replace the stdin debugger.
"""

import os
import re
import select
import socket
import subprocess
import sys
import tempfile
import time

PC_ADDR = 0x1100
RAM_ADDR = 0x1104
OPCODE = 0x5280
POKED = 0xA5A5
# A prompt is a trailing "> ". A "> <command>" echo ends with the command.
PROMPT = b"> "
PROMPT_QUIET = 0.05


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
    """Bind the HRDB port so a successful listen cannot replace stdin."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 56001))
    except OSError as exc:
        fail("cannot hold 127.0.0.1:56001 (%s); HRDB would own the debugger" % exc)
    sock.listen(1)
    return sock


def prg_ok(path):
    data = open(path, "rb").read()
    if len(data) < 0x1C + 6 or data[0:2] != b"\x60\x1a":
        fail("fixture is not a TOS PRG: %s" % path)
    text = int.from_bytes(data[2:6], "big")
    if text < 6:
        fail("fixture text size is %d, expected at least 6" % text)
    body = data[0x1C:0x1C + 6]
    if body != bytes([0x52, 0x80, 0x60, 0xFC, 0xBE, 0xEF]):
        fail("fixture text is %s, expected 5280 60fc beef" % body.hex())


class Session:
    def __init__(self, hatari, prg, home, parse_path):
        self.stdout = b""
        self.stderr = b""
        self.mark = 0
        env = os.environ.copy()
        env["HATARI_TEST"] = "restore"
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
            "--tos", "none",
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

    def wait_prompt(self, timeout=20):
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

    def cmd(self, line):
        if self.proc.poll() is not None:
            fail("hatari exited before command %r" % line, self.stdout, self.stderr)
        self.proc.stdin.write((line + "\n").encode("ascii"))
        self.proc.stdin.flush()
        return self.wait_prompt()

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


def parse_regs(text):
    regs = {}
    for name, value in re.findall(r"\b([DA][0-7]) ([0-9A-F]{8})\b", text):
        regs[name] = int(value, 16)
    sr = re.findall(r"SR=([0-9A-F]{4})", text)
    dis = re.findall(r"^([0-9A-Fa-f]{8}) ([0-9A-Fa-f]{4})\s+(.*)$", text, re.M)
    nxt = re.findall(r"Next PC: ([0-9A-Fa-f]{8})", text)
    missing = [n for n in ["D%d" % i for i in range(8)] + ["A%d" % i for i in range(8)] if n not in regs]
    if missing or not sr or not dis or not nxt:
        raise DumpError("register dump is incomplete (%s)\n%s" % (missing, text))
    pc, opcode, insn = dis[-1]
    return {
        "regs": regs,
        "sr": int(sr[-1], 16),
        "pc": int(pc, 16),
        "opcode": int(opcode, 16),
        "insn": insn.strip(),
        "next": int(nxt[-1], 16),
    }


def parse_word(text, addr):
    found = re.findall(r"^%08X: ([0-9A-Fa-f]{4})\b" % addr, text, re.M)
    if not found:
        raise DumpError("RAM word at $%x missing from memdump\n%s" % (addr, text))
    return int(found[0], 16)


def snapshot(state, ram):
    return {
        "pc": state["pc"],
        "sr": state["sr"],
        "regs": dict(state["regs"]),
        "ram": ram,
        "insn": state["insn"],
        "opcode": state["opcode"],
        "next": state["next"],
    }


def same_stop(a, b):
    return a["pc"] == b["pc"] and a["sr"] == b["sr"] and a["regs"] == b["regs"] and a["ram"] == b["ram"]


def show(stop):
    return "pc=%08X sr=%04X d0=%08X ram=%04X insn=%s" % (
        stop["pc"], stop["sr"], stop["regs"]["D0"], stop["ram"], stop["insn"])


def run_once(hatari, prg, home, parse_path, snap_path):
    session = Session(hatari, prg, home, parse_path)
    try:
        session.wait_prompt()
        help_text = session.cmd("h reststop")
        if "Unknown command 'reststop'" in help_text or "'reststop'" not in help_text:
            fail("reststop is not a debugger command\n" + help_text, session.stdout, session.stderr)
        state_help = session.cmd("h stateload")
        if "restore emulation state" not in state_help:
            fail("stateload help changed\n" + state_help, session.stdout, session.stderr)
        unknown = session.cmd("nosuchcmd")
        if "Command 'nosuchcmd' not found." not in unknown:
            fail("unknown command text changed\n" + unknown, session.stdout, session.stderr)

        session.cmd("w w $%x $%x" % (RAM_ADDR, POKED))
        stopped = parse_regs(session.cmd("r"))
        ram = parse_word(session.cmd("m w $%x" % RAM_ADDR), RAM_ADDR)
        if stopped["pc"] != PC_ADDR or stopped["opcode"] != OPCODE:
            fail("did not stop on the increment: %s" % stopped["insn"],
                 session.stdout, session.stderr)
        if stopped["sr"] & 0x2000:
            fail("stop is supervisor SR=%04X; fake TOS SR was rewritten" % stopped["sr"],
                 session.stdout, session.stderr)
        if stopped["sr"] != 0x0300:
            fail("SR is %04X, expected 0300 from fake TOS" % stopped["sr"],
                 session.stdout, session.stderr)
        if ram != POKED:
            fail("RAM word is %04X before save, expected %04X" % (ram, POKED),
                 session.stdout, session.stderr)
        saved = snapshot(stopped, ram)

        session.cmd("statesave %s" % snap_path)
        if not os.path.isfile(snap_path):
            fail("statesave did not write %s" % snap_path, session.stdout, session.stderr)
        size = os.path.getsize(snap_path)
        after_save = parse_regs(session.cmd("r"))
        if after_save["pc"] != saved["pc"]:
            fail("statesave moved PC to %08X" % after_save["pc"], session.stdout, session.stderr)

        session.cmd("s")
        stepped = parse_regs(session.cmd("r"))
        if stepped["pc"] == saved["pc"] or stepped["regs"]["D0"] == saved["regs"]["D0"]:
            fail("step did not change D0 and PC; the rest would be vacuous\n%s" % show(snapshot(stepped, ram)),
                 session.stdout, session.stderr)

        session.cmd("reststop %s" % snap_path)
        restored_regs = parse_regs(session.cmd("r"))
        restored_ram = parse_word(session.cmd("m w $%x" % RAM_ADDR), RAM_ADDR)
        restored = snapshot(restored_regs, restored_ram)
        if not same_stop(saved, restored):
            fail("restore disagrees with the saved stop\nsaved    %s\nrestored %s\n"
                 "first guest instruction after restore: %s"
                 % (show(saved), show(restored), saved["insn"]),
                 session.stdout, session.stderr)

        session.cmd("s")
        one = parse_regs(session.cmd("r"))
        expect_d0 = (saved["regs"]["D0"] + 1) & 0xFFFFFFFF
        if one["regs"]["D0"] != expect_d0 or one["pc"] != saved["next"]:
            fail("step was not one increment\nexpected d0=%08X pc=%08X\ngot      d0=%08X pc=%08X insn=%s\n"
                 "instruction at the restored PC was: %s"
                 % (expect_d0, saved["next"], one["regs"]["D0"], one["pc"], one["insn"], saved["insn"]),
                 session.stdout, session.stderr)

        session.cmd("reststop %s" % snap_path)
        again_regs = parse_regs(session.cmd("r"))
        again_ram = parse_word(session.cmd("m w $%x" % RAM_ADDR), RAM_ADDR)
        again = snapshot(again_regs, again_ram)
        if not same_stop(saved, again):
            fail("second restore disagrees\nsaved  %s\nsecond %s" % (show(saved), show(again)),
                 session.stdout, session.stderr)
        if not os.path.isfile(snap_path) or os.path.getsize(snap_path) != size:
            fail("snapshot was consumed", session.stdout, session.stderr)

        code = session.quit()
        if code != 0:
            fail("hatari quit with status %s" % code, session.stdout, session.stderr)
        return {
            "saved": saved,
            "step_d0": one["regs"]["D0"],
            "step_pc": one["pc"],
            "step_insn": one["insn"],
        }
    except DumpError as exc:
        fail(str(exc), session.stdout, session.stderr)
    finally:
        session.close()


def main():
    if len(sys.argv) != 3:
        fail("usage: run_test.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        fail("not executable: %s" % hatari)
    prg_ok(prg)

    port = hold_hrdb_port()
    try:
        with tempfile.TemporaryDirectory(prefix="hatari-restore-") as home:
            parse_path = os.path.join(home, "break.txt")
            with open(parse_path, "w", encoding="ascii") as fh:
                fh.write("b pc=$%x :quiet\n" % PC_ADDR)
            first = run_once(hatari, prg, home, parse_path, os.path.join(home, "one.sav"))
            second = run_once(hatari, prg, home, parse_path, os.path.join(home, "two.sav"))
    finally:
        port.close()

    if not same_stop(first["saved"], second["saved"]):
        fail("second run saved stop disagrees\nfirst  %s\nsecond %s"
             % (show(first["saved"]), show(second["saved"])))
    if first["step_d0"] != second["step_d0"] or first["step_pc"] != second["step_pc"]:
        fail("second run step disagrees")

    saved = first["saved"]
    print("pass reststop saved_pc=%08X saved_d0=%08X restored_pc=%08X restored_d0=%08X "
          "step_d0=%08X step_pc=%08X step_insn=%s saved_insn=%s"
          % (saved["pc"], saved["regs"]["D0"], saved["pc"], saved["regs"]["D0"],
             first["step_d0"], first["step_pc"], first["step_insn"], saved["insn"]))
    print("stateload unchanged")


if __name__ == "__main__":
    main()

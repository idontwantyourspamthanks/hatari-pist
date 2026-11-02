#!/usr/bin/env python3
"""Record one key injected the way PiST's media channel injects it.

The fork connects to a listener, authenticates, and is stopped when the
vertical-blank counter reaches 5. The key is sent while that prompt is up,
so the next screen poll is the first chance to see it. That poll is before
the counter increments, and the recording must stamp the key for the blank
that follows (frame 6). Replay of the file matches, including a run that
was given the same key with --replay-key. A different scancode diverges
after that frame.
"""

import os
import re
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

import run_stop_recording as recording

KEY_FRAME = 6
SCAN = 0x39
PROMPT = b"> "
PROMPT_QUIET = 0.05
TOKEN = bytes(range(16))


def machine(hatari, prg, extra):
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
        "--tos", "none",
        "--joy0", "none",
        "--joy1", "none",
        "--joy2", "none",
        "--joy3", "none",
        "--joy4", "none",
        "--joy5", "none",
        "--run-vbls", str(recording.RUN_VBLS),
    ]
    args.extend(extra)
    args.append(prg)
    return args


class Media:
    def __init__(self):
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.conn = None
        self.error = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._handshake, daemon=True)
        self.thread.start()

    def close(self):
        for sock in (self.conn, self.listener):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _handshake(self):
        try:
            self.listener.settimeout(30)
            conn, _ = self.listener.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            auth = b""
            conn.settimeout(10)
            while len(auth) < 20:
                chunk = conn.recv(20 - len(auth))
                if not chunk:
                    raise RuntimeError("media peer closed before AUTH")
                auth += chunk
            if auth != b"PSA1" + TOKEN:
                raise RuntimeError("AUTH was %r" % auth)
            conn.sendall(struct.pack("<4sII", b"PSH1", 2, 3))
            self.conn = conn
            self.ready.set()
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def send_key(self, code, down):
        if not self.ready.wait(30):
            recording.fail("media channel did not authenticate")
        if self.error is not None:
            recording.fail("media handshake failed: %s" % self.error)
        self.conn.sendall(bytes((ord("K"), code, 1 if down else 0)))

    def send_mouse(self, dx, dy, buttons):
        if not self.ready.wait(30):
            recording.fail("media channel did not authenticate")
        if self.error is not None:
            recording.fail("media handshake failed: %s" % self.error)
        self.conn.sendall(struct.pack("<BhhB", ord("M"), dx, dy, buttons))


class Session:
    def __init__(self, hatari, prg, home, parse_path, rec, port):
        self.stdout = b""
        self.stderr = b""
        self.mark = 0
        env = os.environ.copy()
        env["HATARI_TEST"] = "replay-media-key"
        env["SDL_VIDEODRIVER"] = "dummy"
        env["SDL_AUDIODRIVER"] = "dummy"
        env["HOME"] = home
        env["PIST_MEDIA_TOKEN"] = TOKEN.hex()
        args = machine(hatari, prg, [
            "--replay-record", rec,
            "--pist-media", str(port),
            "--parse", parse_path,
        ])
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, bufsize=0,
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
                recording.fail("HRDB is listening; it must not be what stops the CPU",
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
        recording.fail("debugger prompt did not return", self.stdout, self.stderr)

    def cmd(self, line, timeout=30):
        if self.proc.poll() is not None:
            recording.fail("hatari exited before command %r" % line, self.stdout, self.stderr)
        self.proc.stdin.write((line + "\n").encode("ascii"))
        self.proc.stdin.flush()
        return self.wait_prompt(timeout)

    def continue_to_end(self, timeout=60):
        self.proc.stdin.write(b"c\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        quiet_at = None
        while time.monotonic() < deadline:
            got = self._read(0.05)
            if self.proc.poll() is not None:
                while self._read(0.05):
                    pass
                return self.proc.returncode
            if self.stderr.endswith(PROMPT):
                if got or quiet_at is None:
                    quiet_at = time.monotonic()
                elif time.monotonic() - quiet_at >= PROMPT_QUIET:
                    recording.fail("breakpoint fired again after continue", self.stdout, self.stderr)
            else:
                quiet_at = None
        recording.fail("recording did not finish after continue", self.stdout, self.stderr)


def vbl_of(text):
    found = re.findall(r"#(\d+) \(dec\)", text)
    if not found:
        raise RuntimeError("VBL value missing\n" + text)
    return int(found[-1])


def input_lines(path):
    lines = []
    with open(path, "r", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("input "):
                lines.append(line.rstrip("\n"))
    return lines


def main():
    if len(sys.argv) != 3:
        recording.fail("usage: run_media_key.py <hatari> <fixture.prg>")
    hatari, prg = sys.argv[1], sys.argv[2]
    if not os.access(hatari, os.X_OK):
        recording.fail("not executable: %s" % hatari)

    with tempfile.TemporaryDirectory(prefix="hatari-replay-media-") as home:
        rec = os.path.join(home, "rec.txt")
        scripted = os.path.join(home, "scripted.txt")
        other = os.path.join(home, "other.txt")
        stop_parse = os.path.join(home, "stop.txt")
        keep_parse = os.path.join(home, "keep.txt")
        with open(stop_parse, "w", encoding="ascii") as fh:
            fh.write("b pc = $1\n")
            fh.write("b VBL = %d :once :quiet\n" % (KEY_FRAME - 1))
        with open(keep_parse, "w", encoding="ascii") as fh:
            fh.write("b pc = $1\n")

        media = Media()
        port = recording.hold_hrdb_port()
        try:
            session = Session(hatari, prg, home, stop_parse, rec, media.port)
            try:
                session.wait_prompt()
                vbl = vbl_of(session.cmd("e VBL"))
                if vbl != KEY_FRAME - 1:
                    recording.fail("stopped at VBL %d, expected %d" % (vbl, KEY_FRAME - 1),
                                   session.stdout, session.stderr)
                media.send_key(SCAN, True)
                code = session.continue_to_end()
                if code != 0 or b"replay recorded\n" not in session.stdout:
                    recording.fail("recording did not complete (exit %s)" % code,
                                   session.stdout, session.stderr)
            finally:
                session.close()
        finally:
            port.close()
            media.close()

        got = input_lines(rec)
        expect = "input %d %02x down" % (KEY_FRAME, SCAN)
        if got != [expect]:
            recording.fail("media key was logged as %s, expected %s" % (got, expect))

        script = recording.run_batch(
            hatari, prg, home,
            ["--replay-record", scripted,
             "--replay-key", "%d:%02x:down" % (KEY_FRAME, SCAN),
             "--parse", keep_parse],
            False)
        if script.returncode != 0 or b"replay recorded\n" not in script.stdout:
            recording.fail("scripted record did not complete", script.stdout, script.stderr)
        if recording.trace_lines(rec) != recording.trace_lines(scripted):
            recording.fail("media-key trace does not match --replay-key at the same frame")

        matched = recording.run_batch(
            hatari, prg, home,
            ["--replay", rec, "--parse", keep_parse],
            False)
        if matched.returncode != 0 or b"replay matched\n" not in matched.stdout:
            recording.fail("replay of the media key did not match", matched.stdout, matched.stderr)

        lines = open(rec, "r", encoding="ascii").read().splitlines()
        replaced = [("input %d 1c down" % KEY_FRAME) if line == expect else line for line in lines]
        if replaced == lines:
            recording.fail("negative recording did not change the key")
        with open(other, "w", encoding="ascii") as fh:
            fh.write("\n".join(replaced) + "\n")
        changed = recording.run_batch(
            hatari, prg, home,
            ["--replay", other, "--parse", keep_parse],
            False)
        text = changed.stdout.decode("latin-1", errors="replace")
        found = re.findall(r"^replay diverged at frame (\d+)$", text, re.M)
        if changed.returncode == 0 or len(found) != 1:
            recording.fail("changed media key did not diverge", changed.stdout, changed.stderr)
        frame = int(found[0])
        if frame <= KEY_FRAME or frame > recording.RUN_VBLS:
            recording.fail("changed media key diverged at frame %d" % frame,
                           changed.stdout, changed.stderr)

    print("pass media-key frame=%d scancode=%02x diverged=%d" % (KEY_FRAME, SCAN, frame))


if __name__ == "__main__":
    main()

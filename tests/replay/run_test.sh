#!/bin/sh
# Record one --tos none program, replay it, then replay with one key changed.
# Exit 0 only when the matching replay agrees and the changed key diverges.

if [ $# -lt 2 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
	echo "Usage: $0 <hatari> <fixture.prg>"
	exit 1
fi

hatari=$1
fixture=$2

if [ ! -x "$hatari" ]; then
	echo "First parameter must point to a hatari executable."
	exit 1
fi
if [ ! -f "$fixture" ]; then
	echo "Second parameter must point to the fixture PRG."
	exit 1
fi

# 28-byte GEMDOS header + 38 bytes of text. See fixture.s.
size=$(wc -c < "$fixture")
size=$(echo "$size" | tr -d ' ')
if [ "$size" != 66 ]; then
	echo "Fixture size is $size, expected 66."
	exit 1
fi

KEY_FRAME=15
RUN_VBLS=40

testdir=$(mktemp -d)
out="$testdir/out.txt"
err="$testdir/err.txt"
rec="$testdir/rec.txt"
neg="$testdir/neg.txt"
bad="$testdir/bad.txt"

remove_temp() {
	rm -rf "$testdir"
}
trap remove_temp EXIT

fail() {
	echo "FAILED: $*"
	echo "---- stdout ----"
	if [ -f "$out" ]; then
		cat "$out"
	fi
	echo "---- stderr ----"
	if [ -f "$err" ]; then
		cat "$err"
	fi
	if [ -f "$rec" ]; then
		echo "---- recording (header) ----"
		awk 'BEGIN { n = 0 } /^trace / { n++; if (n > 1) next } { print }' "$rec"
	fi
	exit 1
}

export HATARI_TEST=replay
export SDL_VIDEODRIVER=dummy
export SDL_AUDIODRIVER=dummy

# Pinned guest. HOME is empty so a user hatari.cfg cannot change it.
# No floppy image is passed. --tos none is the only ROM.
common() {
	# shellcheck disable=SC2068
	HOME="$testdir" timeout 90 "$hatari" \
		--log-level error \
		--sound off \
		--fast-forward on \
		--confirm-quit off \
		--machine st \
		--cpulevel 0 \
		--cpuclock 8 \
		--compatible on \
		--cpu-exact on \
		--data-cache off \
		--addr24 on \
		--fpu none \
		--mmu off \
		--blitter off \
		--memsize 1024 \
		--video-timing ws3 \
		--monitor rgb \
		--borders off \
		--frameskips 0 \
		--gemdos-time atari \
		--tos none \
		--run-vbls "$RUN_VBLS" \
		"$@"
}

run() {
	common "$@" >"$out" 2>"$err"
}

# Capability probe is the option text, not a version number.
if ! "$hatari" --help >"$testdir/help.txt" 2>&1; then
	:
fi
if ! grep -q -- '--replay-record <file>' "$testdir/help.txt"; then
	fail "--help does not list --replay-record"
fi
if ! grep -q -- '--replay <file>' "$testdir/help.txt"; then
	fail "--help does not list --replay"
fi

run --replay-record "$rec" \
	--replay-key "${KEY_FRAME}:39:down" \
	--replay-key 25:39:up \
	"$fixture"
status=$?
if [ "$status" -ne 0 ]; then
	fail "record exited $status"
fi
if ! grep -x 'replay recorded' "$out" >/dev/null; then
	fail "record did not report replay recorded"
fi
if ! grep -x 'HPISTREPLAY 1' "$rec" >/dev/null; then
	fail "recording has no format version"
fi
if ! grep -x 'debugger no' "$rec" >/dev/null; then
	fail "trace was taken with the per-instruction debugger attached"
fi
if ! grep -x 'tos none' "$rec" >/dev/null; then
	fail "recording is not --tos none"
fi
if ! grep -x 'floppy no' "$rec" >/dev/null; then
	fail "recording is not floppy-free"
fi
if ! grep -x 'machine st' "$rec" >/dev/null; then
	fail "recording is not the ST"
fi
if ! grep -E '^build [0-9a-f]{64}$' "$rec" >/dev/null; then
	fail "recording has no executable sha256"
fi
if ! grep -E '^program [0-9a-f]{64}$' "$rec" >/dev/null; then
	fail "recording has no program sha256"
fi
if ! grep -x "frames $RUN_VBLS" "$rec" >/dev/null; then
	fail "recording frame count is not $RUN_VBLS"
fi
if ! grep -x "input $KEY_FRAME 39 down" "$rec" >/dev/null; then
	fail "recording is missing the space key"
fi
traces=$(grep -c '^trace ' "$rec")
if [ "$traces" != "$RUN_VBLS" ]; then
	fail "trace count is $traces, expected $RUN_VBLS"
fi
if ! grep -q '^trace 1 pc ' "$rec"; then
	fail "trace does not start at frame 1"
fi
if ! grep -q 'pc 000011' "$rec"; then
	fail "program never reached its load address"
fi

run --replay "$rec" "$fixture"
status=$?
if [ "$status" -ne 0 ]; then
	fail "matching replay exited $status"
fi
if ! grep -x 'replay matched' "$out" >/dev/null; then
	fail "matching replay did not match"
fi
if grep -q '^replay diverged' "$out"; then
	fail "matching replay also diverged"
fi

sed "s/^input ${KEY_FRAME} 39 down\$/input ${KEY_FRAME} 1c down/" "$rec" >"$neg"
if ! grep -x "input $KEY_FRAME 1c down" "$neg" >/dev/null; then
	fail "negative control did not change the key"
fi
if grep -x "input $KEY_FRAME 39 down" "$neg" >/dev/null; then
	fail "negative control still has the original key"
fi

run_negative() {
	if run --replay "$neg" "$fixture"; then
		fail "changed key replay exited 0"
	fi
	if ! grep -x 'replay diverged at frame [0-9]*' "$out" >/dev/null; then
		# grep -x basic regex: * applies to the previous atom only if escaped
		# in some greps. Use a line read instead.
		:
	fi
	frame=$(sed -n 's/^replay diverged at frame \([0-9][0-9]*\)$/\1/p' "$out")
	if [ -z "$frame" ]; then
		fail "changed key did not diverge"
	fi
	if [ "$frame" -le "$KEY_FRAME" ]; then
		fail "diverged at frame $frame, which is not after the changed key at $KEY_FRAME"
	fi
	if [ "$frame" -gt "$RUN_VBLS" ]; then
		fail "diverged at frame $frame, past the run"
	fi
}

run_negative
frame_a=$frame
run_negative
frame_b=$frame
if [ "$frame_a" != "$frame_b" ]; then
	fail "negative control frame changed ($frame_a then $frame_b)"
fi

# A different executable identity must be refused, not replayed.
awk '
/^build / {
	n = length($2)
	c = substr($2, n, 1)
	if (c == "0")
		c = "1"
	else
		c = "0"
	$2 = substr($2, 1, n - 1) c
}
{ print }
' "$rec" >"$bad"
if cmp -s "$rec" "$bad"; then
	fail "build line was not tampered"
fi
if run --replay "$bad" "$fixture"; then
	fail "tampered build was accepted"
fi
if ! grep -x 'replay rejected: build' "$out" >/dev/null; then
	fail "tampered build was not rejected as a different build"
fi
if grep -q '^replay diverged' "$out"; then
	fail "tampered build diverged instead of being rejected"
fi

echo "PASS"
echo "format: HPISTREPLAY 1"
echo "fixture: $fixture"
echo "debugger attached: no"
echo "negative control first frame: $frame_a"
echo "---- recording header ----"
awk 'BEGIN { n = 0 } /^trace / { n++; if (n > 2) next } { print }' "$rec"
exit 0

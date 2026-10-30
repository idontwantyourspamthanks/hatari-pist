; paltrace gate. --tos none loads the text at $1100.
; $FF8240 and $FF820A are supervisor registers. The test sets SR
; to $2300 before continuing. Do not rewrite fake TOS "move #$0300,sr".
;
; Stores are move.w. A byte write to a color register is not the proof.
; Bit 1 of $FF820A set is 50 Hz; clear is 60 Hz.
;
; 1. Set 50 Hz. Wait until a VBL has started with that clock in force.
; 2. From that VBL, the same delay, then move.w a known color to $FF8240.
; 3. Clear bit 1 (60 Hz). Wait until a later VBL has started on 508-cycle lines.
; 4. The same delay, then move.w a different color to $FF8240.
; Then wait for another VBL and burn more than 2048 cycles before the spin,
; so the stop is a later VBL and its FrameCycles is past the second write.
;
; The delay is one dbra of 6526, about 65000 CPU cycles. Four cycles per
; line separate 512 from 508, so the same delay from each VBL lands on
; different HBLs, both away from either end of the line.

	org	$1100

begin:
	move.w	#$0200,$ffff820a.w	; 50 Hz
	bsr.w	wait_vbl
	bsr.w	delay
write50:
	move.w	#$0700,$ffff8240.w

	move.w	#$0000,$ffff820a.w	; 60 Hz
	bsr.w	wait_vbl
	bsr.w	delay
write60:
	move.w	#$0070,$ffff8240.w

	bsr.w	wait_vbl
	bsr.w	delay
	bsr.w	extra
spin:
	bra.s	spin

off_write:
	move.w	#$0007,$ffff8240.w
off_done:
	bra.s	off_done

wait_vbl:
	move.l	$462.w,d1
.wait:
	cmp.l	$462.w,d1
	beq.s	.wait
	rts

delay:
	move.w	#6526,d0
.loop:
	dbra	d0,.loop
	rts

extra:
	move.w	#450,d0
.loop:
	dbra	d0,.loop
	rts

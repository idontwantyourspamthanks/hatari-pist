; Reads one IKBD relative-mouse packet and remembers it.
;
; Loaded by --tos none at $1100. The recorder puts the program in
; supervisor so the ACIA read does not bus-error. A relative packet is
; a header $F8-$FB (bit 1 is the left button, bit 0 the right), then dx,
; then dy. Other bytes are ignored. Afterwards D1 is dx, D2 is dy, D3 is
; the header's low two bits, and D4 counts the spin.

	org	$1100

wait_hdr:
	btst.b	#0,$fffffc00.w
	beq.s	wait_hdr
	move.b	$fffffc02.w,d0
	cmp.b	#$f8,d0
	bcs.s	wait_hdr
	cmp.b	#$fc,d0
	bcc.s	wait_hdr
	move.b	d0,d3
	and.b	#3,d3
wait_dx:
	btst.b	#0,$fffffc00.w
	beq.s	wait_dx
	move.b	$fffffc02.w,d1
wait_dy:
	btst.b	#0,$fffffc00.w
	beq.s	wait_dy
	move.b	$fffffc02.w,d2
spin:
	addq.l	#1,d4
	bra.s	spin

; writetrace gate. --tos none loads the text at $1100.
; User mode cannot program $FF8A00. The test sets SR to $2300
; before continuing. Do not rewrite fake TOS "move #$0300,sr".
;
; The watched range is $30000-$3000F. The source word at $30002 is
; planted by the debugger before the trace is armed, so that store
; is not an event. Once in supervisor this code:
;   1. move.b #$A5 to $30000
;   2. one blitter copy of the word at $30002 to $30008
;      (end masks all ones, HOP source, LOP source, hog)
;   3. move.b #$77 to $30010, outside the range
; then bra.s to itself.
;
; 'six' is six distinct byte stores. 'cost' is moveq #63 + dbra,
; 64 byte stores to $30000, then bra.s to itself.
;
; No relocation. Addresses below are offsets from the text start.

	org	$1100

begin:
	move.b	#$a5,$30000.l

	move.w	#2,$ffff8a20.w		; source x increment
	move.w	#2,$ffff8a22.w		; source y increment
	move.l	#$30002,$ffff8a24.w	; source address
	move.w	#$ffff,$ffff8a28.w	; endmask 1
	move.w	#$ffff,$ffff8a2a.w	; endmask 2
	move.w	#$ffff,$ffff8a2c.w	; endmask 3
	move.w	#2,$ffff8a2e.w		; dest x increment
	move.w	#2,$ffff8a30.w		; dest y increment
	move.l	#$30008,$ffff8a32.w	; dest address
	move.w	#1,$ffff8a36.w		; words per line
	move.w	#1,$ffff8a38.w		; lines
	move.b	#2,$ffff8a3a.w		; HOP = source
	move.b	#3,$ffff8a3b.w		; LOP = source
	move.b	#0,$ffff8a3d.w		; skew, no FXSR/NFSR
	move.b	#$c0,$ffff8a3c.w	; hog + busy

	move.b	#$77,$30010.l
spin:
	bra.s	spin

six:
	move.b	#$11,$30000.l
	move.b	#$22,$30001.l
	move.b	#$33,$30002.l
	move.b	#$44,$30003.l
	move.b	#$55,$30004.l
	move.b	#$66,$30005.l
six_done:
	bra.s	six_done

cost:
	moveq	#63,d0
cost_loop:
	move.b	d0,$30000.l
	dbra	d0,cost_loop
cost_done:
	bra.s	cost_done

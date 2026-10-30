; Fixture for the replay gate. One ST program, no floppy, no TOS calls.
;
; Loaded by --tos none at $1100 (basepage $1000). The recorder rewrites
; fake TOS's "move #$0300,sr" to "move #$2300,sr" so this code is
; supervisor: Hatari bus-errors a user-mode ACIA read. It polls the ACIA
; until a make code arrives, then spins in one of two loops:
;   scancode $39 -> D1 = 2, increment D2
;   anything else -> D1 = 1, increment D3
; Break codes (bit 7 set) are ignored. The two loops are different PCs, so a
; trace of frame, PC, and registers diverges after the key and stays apart.
;
; No relocation. Addresses below are offsets from the text start ($1100).
;
; 0000  0839 0000 00fffc00   btst.b #0,$fffffc00.w
; 0008  67f6                 beq.s  0000
; 000a  1039 00fffc02       move.b $fffffc02.w,d0
; 0010  6bee                 bmi.s  0000
; 0012  0c00 0039           cmp.b  #$39,d0
; 0016  6704                 beq.s  001c
; 0018  7201                 moveq  #1,d1
; 001a  6006                 bra.s  0022
; 001c  7202                 moveq  #2,d1
; 001e  5282                 addq.l #1,d2
; 0020  60fc                 bra.s  001e
; 0022  5283                 addq.l #1,d3
; 0024  60fc                 bra.s  0022

	org	$1100

wait_key:
	btst.b	#0,$fffffc00.w
	beq.s	wait_key
	move.b	$fffffc02.w,d0
	bmi.s	wait_key
	cmp.b	#$39,d0
	beq.s	is_space
	moveq	#1,d1
	bra.s	spin_other
is_space:
	moveq	#2,d1
spin_space:
	addq.l	#1,d2
	bra.s	spin_space
spin_other:
	addq.l	#1,d3
	bra.s	spin_other

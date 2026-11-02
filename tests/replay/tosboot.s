; Relocatable. No IO, so the user mode TOS gives a program is enough.
; The first instruction is moveq #1,d1. Once it has run, D1 stays 1,
; D2 stays 2, and D3 only increases.

	org	$0

	moveq	#1,d1
	moveq	#2,d2
spin:
	addq.l	#1,d3
	bra.s	spin

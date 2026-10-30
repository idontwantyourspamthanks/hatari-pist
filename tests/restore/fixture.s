; User-mode increment loop. --tos none loads the text at $1100.
; No I/O. Do not rewrite fake TOS "move #$0300,sr".
; No relocation. Addresses below are offsets from the text start.
;
; 0000  5280      addq.l #1,d0
; 0002  60fc      bra.s  0000
; 0004  beef      the word this program owns, at $1104

loop:
	addq.l	#1,d0
	bra.s	loop
	dc.w	$beef

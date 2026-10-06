/*
  hatari-pist - media channel
  Copyright (C) 2026 PiST contributors

  This program is free software; you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation; either version 2 of the License, or
  (at your option) any later version.

  This program is distributed in the hope that it will be useful,
  but WITHOUT ANY WARRANTY; without even the implied warranty of
  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
  GNU General Public License for more details.
*/

/*
  The PiST media channel (docs/PLAN.md §12 in the PiST repo). With
  --pist-media <port>, Hatari runs windowless and connects to the IDE's TCP
  listener on 127.0.0.1. Nothing is pushed until the IDE's HELLO arrives —
  a socket that never sees one (including a loopback self-connect, where the
  client's ephemeral port collided with the server port) is never streamed to.

  Protocol v2 (little-endian):
    Hatari->IDE AUTH:  'P','S','A','1', token[16] — sent immediately after
                       connect, proving this is the process PiST spawned
                       (the token travels via PIST_MEDIA_TOKEN in the env).
                       The IDE closes anything whose first message is not a
                       matching AUTH and says nothing until one arrives.
    IDE->Hatari HELLO: 'P','S','H','1', u32 version (=2), u32 caps
                       (bit0: video, bit1: input). Sent only after a valid
                       AUTH. Nothing is pushed until it arrives.
    IDE->Hatari KEY:   'K', u8 ST scancode, u8 down(1)/up(0).
    IDE->Hatari MOUSE: 'M', s16 dx (LE), s16 dy (LE), u8 buttons
                       (bit0: left down, bit1: right down — absolute state,
                       not events). Deltas feed the IKBD's relative-mouse
                       accumulator; button state mirrors the SDL path.
    Hatari->IDE FRAME: u32 magic 0x46524D31, u32 w, u32 h, u32 pitch, u32 seq,
                       u32 bpp, u32 rmask, u32 gmask, u32 bmask,
                       then pitch*h bytes (h rows of the ST screen area only;
                       pitch may exceed w*4). The magic is compared as a u32
                       value, so its bytes on the wire are 31 4D 52 46.

  Sends never block emulation: a frame that cannot go out whole is dropped.
  A partial frame is never abandoned mid-stream — its remainder is buffered
  and flushed before the next frame; a new frame arriving while one is still
  pending is dropped (never two queued, or latency would grow without bound).
*/
#include "main.h"
#include "pistmedia.h"
#include "configuration.h"
#include "ikbd.h"
#include "audio.h"
#include "video.h"
#include "log.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* The channel is plain TCP, so it wants no Unix-domain sockets — but Windows
 * has no POSIX socket headers at all and gets WinSock instead. Same shape as
 * debug/remotedebug.c: HAVE_WINSOCK_SOCKETS is what CMake's
 * check_include_files(winsock.h) puts in config.h (reached through main.h), and
 * src/CMakeLists.txt links ws2_32 on WIN32. WinSock has neither MSG_DONTWAIT
 * nor fcntl, so the socket goes non-blocking through ioctlsocket — which is
 * what the flag was asking for — and its errors come from WSAGetLastError, not
 * errno. A SOCKET is unsigned, hence the sentinel and MEDIA_CONNECTED() in
 * place of a `fd >= 0` test. */
#if HAVE_WINSOCK_SOCKETS
#include <winsock.h>
typedef SOCKET    media_socket_t;
typedef int       media_socklen_t;
typedef SSIZE_T   media_ssize_t;
#define MEDIA_INVALID_SOCKET  INVALID_SOCKET
#define MEDIA_CONNECTED()     (media_fd != MEDIA_INVALID_SOCKET)
#define MEDIA_RW_FLAGS        0
#define MEDIA_CLOSE           closesocket
#else
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <fcntl.h>
typedef int       media_socket_t;
typedef socklen_t media_socklen_t;
typedef ssize_t   media_ssize_t;
#define MEDIA_INVALID_SOCKET  (-1)
#define MEDIA_CONNECTED()     (media_fd >= 0)
#define MEDIA_RW_FLAGS        MSG_DONTWAIT
#define MEDIA_CLOSE           close
#endif

#define FRAME_MAGIC    0x46524D31u
#define HELLO_VERSION  1u
#define HELLO_BYTES    12
#define FRAME_MAX      (8 * 1024 * 1024 + 36) /* generous: any ST/TT/Falcon mode */

static media_socket_t media_fd = MEDIA_INVALID_SOCKET;
static int      media_port;
static uint32_t media_seq;
static bool     hello_seen;
static int      last_w, last_h;
static uint64_t next_connect_ms;
static uint8_t *pending;      /* buffered partial frame */
static size_t   pending_cap;  /* its allocation size */
static size_t   pending_len;  /* total bytes of the buffered frame */
static size_t   pending_off;  /* bytes of it already written */


/* inbound message accumulator (HELLO is 12 bytes, KEY 3, magic check 4) */
static uint8_t  inbuf[64];
static int      inlen;

static uint64_t stat_frames_sent,     /* fully handed to the kernel */
                stat_frames_queued,   /* partially sent, remainder buffered */
                stat_frames_dropped, stat_connect_failures,
                stat_selfconnects, stat_blocked_ms;

static uint64_t MonoMs(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void LogStats(void)
{
	Log_Printf(LOG_INFO, "pist-media: sent=%llu queued=%llu dropped=%llu "
	           "connect failures=%llu self-connects=%llu blocked=%llums\n",
	           (unsigned long long)stat_frames_sent,
	           (unsigned long long)stat_frames_queued,
	           (unsigned long long)stat_frames_dropped,
	           (unsigned long long)stat_connect_failures,
	           (unsigned long long)stat_selfconnects,
	           (unsigned long long)stat_blocked_ms);
}

#if HAVE_WINSOCK_SOCKETS
/* WinSock needs one startup per process. remotedebug.c does its own and the
 * calls are refcounted, so the media channel never depends on the HRDB
 * listener having come up first. There is no matching WSACleanup: the socket
 * lives until the process does. */
static bool StartWinsock(void)
{
	static bool started;
	WSADATA data;

	if (started)
		return true;
	if (WSAStartup(MAKEWORD(2, 2), &data) != 0)
	{
		Log_Printf(LOG_WARN, "pist-media: WSAStartup failed (WSA error %d)\n",
		           WSAGetLastError());
		return false;
	}
	started = true;
	return true;
}
#endif

/* WinSock has no fcntl; FIONBIO is the same request. The whole channel depends
 * on the switch taking effect: this path runs from the VBL, so a recv() that
 * blocks on an empty socket would freeze the emulation loop — and with it the
 * HRDB listener — while the IDE still believes the session is running. A
 * failed switch therefore drops the channel instead of wedging the machine. */
static bool SetNonBlocking(media_socket_t fd)
{
#if HAVE_WINSOCK_SOCKETS
	u_long mode = 1;
	if (ioctlsocket(fd, FIONBIO, &mode) != 0)
	{
		Log_Printf(LOG_WARN, "pist-media: ioctlsocket(FIONBIO) failed (WSA error %d); "
		           "dropping the channel rather than blocking the loop\n",
		           WSAGetLastError());
		return false;
	}
	return true;
#else
	if (fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK) == -1)
	{
		Log_Printf(LOG_WARN, "pist-media: O_NONBLOCK failed (%s); dropping the channel "
		           "rather than blocking the loop\n", strerror(errno));
		return false;
	}
	return true;
#endif
}

/* The retry log names why the connect failed; WinSock does not set errno, so
 * the two paths describe it differently. */
static void LogConnectFailure(void)
{
#if HAVE_WINSOCK_SOCKETS
	Log_Printf(LOG_INFO, "pist-media: connect to 127.0.0.1:%d failed (WSA error %d), "
	           "retry in 1s\n", media_port, WSAGetLastError());
#else
	Log_Printf(LOG_INFO, "pist-media: connect to 127.0.0.1:%d failed (%s), "
	           "retry in 1s\n", media_port, strerror(errno));
#endif
}

/* Windows has no setenv, and _putenv_s always overwrites, so the getenv test
 * there is what stands in for setenv's overwrite=0. */
static void DefaultDriver(const char *name, const char *value)
{
	if (getenv(name))
		return;
#ifdef WIN32
	_putenv_s(name, value);
#else
	setenv(name, value, 0);
#endif
}

const char *PistMedia_SetPort(const char *arg)
{
	long port;
	char *end = NULL;

	if (!arg || !*arg)
		return "missing port";
	port = strtol(arg, &end, 10);
	if (!end || *end != '\0' || port < 1 || port > 65535)
		return "invalid port";
	media_port = (int)port;

	/* media mode is windowless: default both drivers to dummy (an explicit
	 * user setting still wins) */
	DefaultDriver("SDL_VIDEODRIVER", "dummy");
	DefaultDriver("SDL_AUDIODRIVER", "dummy");
	return NULL;
}

bool PistMedia_Enabled(void)
{
	return media_port > 0;
}

static void Disconnect(void)
{
	if (MEDIA_CONNECTED())
		MEDIA_CLOSE(media_fd);
	media_fd = MEDIA_INVALID_SOCKET;
	hello_seen = false;
	inlen = 0;
	pending_len = pending_off = 0;
}


/* protocol v2: the fork proves it is the process PiST spawned by sending
 * 'PSA1' + the 16-byte token PiST passed in the environment. 20 bytes. */
static void SendAuth(void)
{
	uint8_t msg[20] = {'P', 'S', 'A', '1'};
	const char *hex = getenv("PIST_MEDIA_TOKEN");

	if (!hex || strlen(hex) != 32)
	{
		Log_Printf(LOG_WARN, "pist-media: no PIST_MEDIA_TOKEN in the "
		           "environment; the IDE will not answer\n");
		return;
	}
	for (int i = 0; i < 16; i++)
	{
		unsigned int byte;
		if (sscanf(hex + 2 * i, "%2x", &byte) != 1)
		{
			Log_Printf(LOG_WARN, "pist-media: malformed PIST_MEDIA_TOKEN\n");
			return;
		}
		msg[4 + i] = (uint8_t)byte;
	}
	if (send(media_fd, (const char *)msg, (int)sizeof(msg), MEDIA_RW_FLAGS)
	    != (media_ssize_t)sizeof(msg))
		Log_Printf(LOG_WARN, "pist-media: could not send AUTH\n");
}

static void Connect(void)
{
	struct sockaddr_in addr, local;
	media_socklen_t loclen = sizeof(local);
	int sndbuf = 4 * 1024 * 1024;
	int one = 1;

	if (MEDIA_CONNECTED() || MonoMs() < next_connect_ms)
		return;

#if HAVE_WINSOCK_SOCKETS
	if (!StartWinsock())
		return;
#endif

	media_fd = socket(AF_INET, SOCK_STREAM, 0);
	if (media_fd == MEDIA_INVALID_SOCKET)
		return;

	addr.sin_family = AF_INET;
	addr.sin_port = htons(media_port);
	addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
	if (connect(media_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
	{
		MEDIA_CLOSE(media_fd);
		media_fd = MEDIA_INVALID_SOCKET;
		stat_connect_failures++;
		next_connect_ms = MonoMs() + 1000; /* 1 Hz backoff */
		LogConnectFailure();
		return;
	}

	/* self-connect guard: an ephemeral local port equal to the server port
	 * means the socket loops back to itself (docs/PLAN.md §12.7) */
	if (getsockname(media_fd, (struct sockaddr *)&local, &loclen) == 0
	    && ntohs(local.sin_port) == media_port)
	{
		Log_Printf(LOG_WARN, "pist-media: self-connect (local port == %d), dropping\n",
		           media_port);
		MEDIA_CLOSE(media_fd);
		media_fd = MEDIA_INVALID_SOCKET;
		stat_selfconnects++;
		next_connect_ms = MonoMs() + 1000;
		return;
	}

	/* (const char *) is WinSock's optval type; POSIX takes const void *. */
	setsockopt(media_fd, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof(one));
	setsockopt(media_fd, SOL_SOCKET, SO_SNDBUF, (const char *)&sndbuf, sizeof(sndbuf));
	if (!SetNonBlocking(media_fd))
	{
		MEDIA_CLOSE(media_fd);
		media_fd = MEDIA_INVALID_SOCKET;
		stat_connect_failures++;
		next_connect_ms = MonoMs() + 1000;
		return;
	}

	/* frameskipping starves the panel under fast-forward: cap it off */
	ConfigureParams.Screen.nFrameSkips = 0;

	if (!pending)
	{
		pending = malloc(FRAME_MAX);
		pending_cap = pending ? FRAME_MAX : 0;
	}

	Log_Printf(LOG_INFO, "pist-media: connected to 127.0.0.1:%d\n", media_port);

	/* authenticate to the IDE before anything else is said. The token
	 * arrives from PiST via the environment (PIST_MEDIA_TOKEN, 32 hex
	 * chars); without it the IDE never sends HELLO and the channel stays
	 * silent — an impostor listener gets nothing. */
	SendAuth();
}

/* non-blocking write; returns bytes consumed (0 on EAGAIN), -1 on error */
static media_ssize_t WriteSome(const uint8_t *buf, size_t len)
{
	media_ssize_t n = send(media_fd, (const char *)buf, (int)len, MEDIA_RW_FLAGS);
	if (n < 0)
	{
#if HAVE_WINSOCK_SOCKETS
		const int err = WSAGetLastError();
		if (err == WSAEWOULDBLOCK || err == WSAEINTR)
			return 0;
#else
		if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
			return 0;
#endif
		return -1;
	}
	return n;
}

/* flush the buffered frame remainder; false if the connection died */
static bool FlushPending(void)
{
	while (pending_off < pending_len)
	{
		media_ssize_t n = WriteSome(pending + pending_off, pending_len - pending_off);
		if (n < 0)
		{
			Disconnect();
			return false;
		}
		if (n == 0)
			return true; /* still pending, but connection alive */
		pending_off += n;
	}
	pending_len = pending_off = 0;
	return true;
}

void PistMedia_PushAudio(const int16_t (*ring)[2], int writePos, int ringPow2, bool indexReset)
{
	static int prev = -1;
	int len, start_idx, first, idx, part;
	size_t off = 0;
	uint64_t start;

	if (media_port <= 0 || !ring)
		return;
	if (!MEDIA_CONNECTED() || !hello_seen || indexReset)
	{
		/* a buffer-index reset (pause, stop, fast-forward) makes any span
		 * stale: start measuring from the next chunk */
		prev = writePos;
		return;
	}
	if (prev < 0)
	{
		prev = writePos;
		return;
	}

	len = writePos - prev;
	if (len < 0)
		len += ringPow2;
	start_idx = prev;
	prev = writePos;
	if (len <= 0)
		return;

	/* a partially-sent frame must finish before anything else is said, or
	 * the audio bytes would land mid-frame and desync the stream; audio is
	 * the more droppable of the two */
	if (pending_len > 0 && !FlushPending())
		return;
	if (pending_len > 0)
		return;

	/* Stage the whole message, then write it from the pending slot: one code
	 * path for whole and partial sends, so a short write can never split a
	 * message and desync the receiver (the failure PushFrame's stash exists
	 * for). 9 + len*4 bytes, far below FRAME_MAX. */
	if ((size_t)(9 + (size_t)len * 4) > pending_cap)
		return; /* cannot stage: drop the chunk, keep the channel */
	start = MonoMs();
	pending[off++] = 'A';
	pending[off++] = (uint8_t)nAudioFrequency;
	pending[off++] = (uint8_t)(nAudioFrequency >> 8);
	pending[off++] = (uint8_t)(nAudioFrequency >> 16);
	pending[off++] = (uint8_t)(nAudioFrequency >> 24);
	pending[off++] = (uint8_t)len;
	pending[off++] = (uint8_t)(len >> 8);
	pending[off++] = (uint8_t)(len >> 16);
	pending[off++] = (uint8_t)(len >> 24);

	/* the ring may wrap: the chunk is at most two spans */
	first = len;
	if (start_idx + first > ringPow2)
		first = ringPow2 - start_idx;
	idx = start_idx;
	for (part = 0; part < 2; part++)
	{
		const int count = (part == 0) ? first : (len - first);
		if (count > 0)
		{
			memcpy(pending + off, ring[idx], (size_t)count * 4);
			off += (size_t)count * 4;
		}
		idx = 0;
	}
	pending_len = off;
	pending_off = 0;
	FlushPending();
	stat_blocked_ms += MonoMs() - start;
}

void PistMedia_PushFrame(SDL_Surface *surface, int w, int h, bool changed)
{
	uint32_t header[9];
	size_t payload, total, done;
	uint64_t start;
	media_ssize_t n1, n2;

	if (media_port <= 0)
		return;
	if (!MEDIA_CONNECTED())
		Connect();
	if (!MEDIA_CONNECTED())
		return;
	if (!hello_seen)
		return; /* nothing streams before the IDE's HELLO */

	/* always drain first: a quiet screen must not starve a queued frame */
	if (!FlushPending())
		return;
	if (pending_len > 0)
	{
		/* previous frame still draining: drop this one, never queue two */
		stat_frames_dropped++;
		return;
	}
	if (!surface || w <= 0 || h <= 0)
		return;
	if (!changed && w == last_w && h == last_h)
		return;
	if (surface->format->BytesPerPixel != 4)
	{
		stat_frames_dropped++;
		return;
	}

	header[0] = FRAME_MAGIC;
	header[1] = w;
	header[2] = h;
	header[3] = surface->pitch;
	header[4] = media_seq++;
	header[5] = surface->format->BitsPerPixel;
	header[6] = surface->format->Rmask;
	header[7] = surface->format->Gmask;
	header[8] = surface->format->Bmask;
	payload = (size_t)surface->pitch * h;
	total = sizeof(header) + payload;

	/* The payload is attempted only once the header is fully out — payload
	 * bytes landing in a partially-sent header's place would desync the
	 * receiver. */
	start = MonoMs();
	n1 = WriteSome((const uint8_t *)header, sizeof(header));
	if (n1 < 0)
	{
		Disconnect();
		return;
	}
	done = (size_t)n1;
	if (done == sizeof(header))
	{
		n2 = WriteSome(surface->pixels, payload);
		if (n2 < 0)
		{
			Disconnect();
			return;
		}
		done += (size_t)n2;
	}

	if (done < total)
	{
		/* partial frame: buffer the remainder so the stream stays framed */
		if (total > pending_cap)
		{
			uint8_t *bigger = malloc(total);
			if (!bigger)
			{
				Disconnect(); /* cannot keep framing: safest reset */
				return;
			}
			free(pending);
			pending = bigger;
			pending_cap = total;
		}
		if (done < sizeof(header))
		{
			memcpy(pending, (uint8_t *)header + done, sizeof(header) - done);
			memcpy(pending + sizeof(header) - done, surface->pixels, payload);
		}
		else
		{
			memcpy(pending, (uint8_t *)surface->pixels + (done - sizeof(header)),
			       total - done);
		}
		pending_len = total;
		pending_off = done;
		stat_frames_queued++;
	}
	else
	{
		stat_frames_sent++;
	}

	stat_blocked_ms += MonoMs() - start;
	last_w = w;
	last_h = h;
	if ((stat_frames_sent + stat_frames_queued) % 500 == 0)
		LogStats();
}

static void OnMessage(void)
{
	if (inbuf[0] == 'K')
	{
		IKBD_PressSTKey(inbuf[1], inbuf[2] != 0);
	}
	else if (inbuf[0] == 'M')
	{
		/* relative deltas into the IKBD accumulator (the path
		 * Main_HandleMouseMotion feeds after its display scaling; PiST
		 * scales its side, so none happens here), buttons as state.
		 * The SDL path's early-boot guard applies too: motion in the
		 * first VBLs confuses TOS (main.c's bIgnoreNextMouseMotion
		 * rationale). */
		if (nVBLs >= 10)
		{
			int16_t dx = (int16_t)(inbuf[1] | (inbuf[2] << 8));
			int16_t dy = (int16_t)(inbuf[3] | (inbuf[4] << 8));
			KeyboardProcessor.Mouse.dx += dx;
			KeyboardProcessor.Mouse.dy += dy;
		}
		if (inbuf[5] & 1)
			Keyboard.bLButtonDown |= BUTTON_MOUSE;
		else
			Keyboard.bLButtonDown &= ~BUTTON_MOUSE;
		if (inbuf[5] & 2)
			Keyboard.bRButtonDown |= BUTTON_MOUSE;
		else
			Keyboard.bRButtonDown &= ~BUTTON_MOUSE;
	}
	else if (inbuf[0] == 'P' && memcmp(inbuf, "PSH1", 4) == 0)
	{
		uint32_t version, caps;
		memcpy(&version, inbuf + 4, 4);
		memcpy(&caps, inbuf + 8, 4);
		hello_seen = true;
		Log_Printf(LOG_INFO, "pist-media: HELLO v%u caps=0x%x\n", version, caps);
	}
}

void PistMedia_PollInput(void)
{
	uint8_t buf[256];

	if (!MEDIA_CONNECTED())
		return;

	for (;;)
	{
		media_ssize_t n = recv(media_fd, (char *)buf, (int)sizeof(buf), MEDIA_RW_FLAGS);
		if (n <= 0)
		{
			if (n == 0)
				Disconnect(); /* peer closed */
			break;
		}
		for (media_ssize_t i = 0; i < n; i++)
		{
			int need;

			inbuf[inlen++] = buf[i];
			/* self-connect echoes our own frame magic back at us */
			if (inlen == 4 && memcmp(inbuf, "FRM1", 4) == 0)
			{
				Log_Printf(LOG_WARN, "pist-media: own frame magic inbound, "
				           "self-connect; dropping\n");
				stat_selfconnects++;
				Disconnect();
				return;
			}
			need = (inbuf[0] == 'P') ? HELLO_BYTES
			     : (inbuf[0] == 'K') ? 3
			     : (inbuf[0] == 'M') ? 6 : 0;
			if (need == 0)
			{
				Log_Printf(LOG_WARN, "pist-media: bad message 0x%02x, dropping\n",
				           inbuf[0]);
				Disconnect();
				return;
			}
			if (inlen == need)
			{
				OnMessage();
				inlen = 0;
			}
		}
	}
}

void PistMedia_Quit(void)
{
	if (media_port > 0)
		LogStats();
	Disconnect();
	free(pending);
	pending = NULL;
}

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

  Protocol v1 (little-endian):
    IDE->Hatari HELLO: 'P','S','H','1', u32 version (=1), u32 caps (bit0: video).
    IDE->Hatari KEY:   'K', u8 ST scancode, u8 down(1)/up(0).
    Hatari->IDE FRAME: u32 magic 0x46524D31, u32 w, u32 h, u32 pitch, u32 seq,
                       u32 bpp, u32 rmask, u32 gmask, u32 bmask,
                       then pitch*h bytes (h rows of the ST screen area only;
                       pitch may exceed w*4).

  Sends never block emulation: a frame that cannot go out whole is dropped.
  A partial frame is never abandoned mid-stream — its remainder is buffered
  and flushed before the next frame; a new frame arriving while one is still
  pending is dropped (never two queued, or latency would grow without bound).
*/
#include "main.h"
#include "pistmedia.h"
#include "configuration.h"
#include "ikbd.h"
#include "log.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <fcntl.h>

#define FRAME_MAGIC    0x46524D31u
#define HELLO_VERSION  1u
#define HELLO_BYTES    12
#define FRAME_MAX      (8 * 1024 * 1024 + 36) /* generous: any ST/TT/Falcon mode */

static int      media_fd = -1;
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
	setenv("SDL_VIDEODRIVER", "dummy", 0);
	setenv("SDL_AUDIODRIVER", "dummy", 0);
	return NULL;
}

bool PistMedia_Enabled(void)
{
	return media_port > 0;
}

static void Disconnect(void)
{
	if (media_fd >= 0)
		close(media_fd);
	media_fd = -1;
	hello_seen = false;
	inlen = 0;
	pending_len = pending_off = 0;
}

static void Connect(void)
{
	struct sockaddr_in addr, local;
	socklen_t loclen = sizeof(local);
	int sndbuf = 4 * 1024 * 1024;
	int one = 1;

	if (media_fd >= 0 || MonoMs() < next_connect_ms)
		return;

	media_fd = socket(AF_INET, SOCK_STREAM, 0);
	if (media_fd < 0)
		return;

	addr.sin_family = AF_INET;
	addr.sin_port = htons(media_port);
	addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
	if (connect(media_fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
	{
		close(media_fd);
		media_fd = -1;
		stat_connect_failures++;
		next_connect_ms = MonoMs() + 1000; /* 1 Hz backoff */
		Log_Printf(LOG_INFO, "pist-media: connect to 127.0.0.1:%d failed (%s), "
		           "retry in 1s\n", media_port, strerror(errno));
		return;
	}

	/* self-connect guard: an ephemeral local port equal to the server port
	 * means the socket loops back to itself (docs/PLAN.md §12.7) */
	if (getsockname(media_fd, (struct sockaddr *)&local, &loclen) == 0
	    && ntohs(local.sin_port) == media_port)
	{
		Log_Printf(LOG_WARN, "pist-media: self-connect (local port == %d), dropping\n",
		           media_port);
		close(media_fd);
		media_fd = -1;
		stat_selfconnects++;
		next_connect_ms = MonoMs() + 1000;
		return;
	}

	setsockopt(media_fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
	setsockopt(media_fd, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));
	fcntl(media_fd, F_SETFL, fcntl(media_fd, F_GETFL) | O_NONBLOCK);

	/* frameskipping starves the panel under fast-forward: cap it off */
	ConfigureParams.Screen.nFrameSkips = 0;

	if (!pending)
	{
		pending = malloc(FRAME_MAX);
		pending_cap = pending ? FRAME_MAX : 0;
	}

	Log_Printf(LOG_INFO, "pist-media: connected to 127.0.0.1:%d\n", media_port);
}

/* non-blocking write; returns bytes consumed (0 on EAGAIN), -1 on error */
static ssize_t WriteSome(const uint8_t *buf, size_t len)
{
	ssize_t n = send(media_fd, buf, len, MSG_DONTWAIT);
	if (n < 0)
	{
		if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)
			return 0;
		return -1;
	}
	return n;
}

/* flush the buffered frame remainder; false if the connection died */
static bool FlushPending(void)
{
	while (pending_off < pending_len)
	{
		ssize_t n = WriteSome(pending + pending_off, pending_len - pending_off);
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

void PistMedia_PushFrame(SDL_Surface *surface, int w, int h, bool changed)
{
	uint32_t header[9];
	size_t payload, total, done;
	uint64_t start;
	ssize_t n1, n2;

	if (media_port <= 0)
		return;
	if (media_fd < 0)
		Connect();
	if (media_fd < 0)
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

	if (media_fd < 0)
		return;

	for (;;)
	{
		ssize_t n = recv(media_fd, buf, sizeof(buf), MSG_DONTWAIT);
		if (n <= 0)
		{
			if (n == 0)
				Disconnect(); /* peer closed */
			break;
		}
		for (ssize_t i = 0; i < n; i++)
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
			     : (inbuf[0] == 'K') ? 3 : 0;
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

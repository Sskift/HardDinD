/*
 * udock-net: unprivileged userspace NAT for the udock UML guest.
 *
 * UML's vector network driver (transport=bess) exchanges raw ethernet frames
 * with us over a UNIX SOCK_SEQPACKET socket, one frame per message. We feed
 * them to libslirp, which turns guest TCP/UDP flows into ordinary host
 * sockets: no tap device, no CAP_NET_ADMIN, no namespaces.
 *
 *   udock-net -s SOCKET [-m MTU] [-f [udp:]HOSTADDR:HOSTPORT:GUESTPORT]...
 *
 * Guest view: 10.0.2.15/24, gateway 10.0.2.2 (= host loopback), DNS 10.0.2.3.
 */
#include <arpa/inet.h>
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <slirp/libslirp.h>

#define MAX_FRAME (65536 + 64)
#define MAX_POLLFDS 4096
#define MAX_TIMERS 64

static int guest_fd = -1;

struct timer {
	int used;
	int64_t expire_ms; /* -1 = not armed */
	SlirpTimerCb cb;
	void *cb_opaque;
};
static struct timer timers[MAX_TIMERS];

static struct pollfd pfds[MAX_POLLFDS];
static int npfds;

static void die(const char *what)
{
	perror(what);
	exit(1);
}

static int64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
}

/* ---- libslirp callbacks ---- */

static ssize_t send_packet(const void *buf, size_t len, void *opaque)
{
	(void)opaque;
	if (guest_fd < 0)
		return len; /* guest not attached: drop */
	if (send(guest_fd, buf, len, MSG_NOSIGNAL) < 0 && errno != EAGAIN)
		fprintf(stderr, "udock-net: send to guest: %s\n", strerror(errno));
	return len;
}

static void guest_error(const char *msg, void *opaque)
{
	(void)opaque;
	fprintf(stderr, "udock-net: guest error: %s\n", msg);
}

static int64_t clock_get_ns(void *opaque)
{
	(void)opaque;
	return now_ns();
}

static void *timer_new(SlirpTimerCb cb, void *cb_opaque, void *opaque)
{
	(void)opaque;
	for (int i = 0; i < MAX_TIMERS; i++) {
		if (!timers[i].used) {
			timers[i] = (struct timer){ 1, -1, cb, cb_opaque };
			return &timers[i];
		}
	}
	fprintf(stderr, "udock-net: out of timers\n");
	abort();
}

static void timer_free(void *t, void *opaque)
{
	(void)opaque;
	((struct timer *)t)->used = 0;
}

static void timer_mod(void *t, int64_t expire_ms, void *opaque)
{
	(void)opaque;
	((struct timer *)t)->expire_ms = expire_ms;
}

static void register_poll_fd(int fd, void *opaque) { (void)fd; (void)opaque; }
static void unregister_poll_fd(int fd, void *opaque) { (void)fd; (void)opaque; }
static void notify(void *opaque) { (void)opaque; }

static const SlirpCb callbacks = {
	.send_packet = send_packet,
	.guest_error = guest_error,
	.clock_get_ns = clock_get_ns,
	.timer_new = timer_new,
	.timer_free = timer_free,
	.timer_mod = timer_mod,
	.register_poll_fd = register_poll_fd,
	.unregister_poll_fd = unregister_poll_fd,
	.notify = notify,
};

/* ---- poll glue ---- */

static int add_poll(int fd, int events, void *opaque)
{
	(void)opaque;
	if (npfds == MAX_POLLFDS) {
		fprintf(stderr, "udock-net: too many fds\n");
		abort();
	}
	short ev = 0;
	if (events & SLIRP_POLL_IN) ev |= POLLIN;
	if (events & SLIRP_POLL_OUT) ev |= POLLOUT;
	if (events & SLIRP_POLL_PRI) ev |= POLLPRI;
	pfds[npfds] = (struct pollfd){ .fd = fd, .events = ev };
	return npfds++;
}

static int get_revents(int idx, void *opaque)
{
	(void)opaque;
	short re = pfds[idx].revents;
	int ev = 0;
	if (re & POLLIN) ev |= SLIRP_POLL_IN;
	if (re & POLLOUT) ev |= SLIRP_POLL_OUT;
	if (re & POLLPRI) ev |= SLIRP_POLL_PRI;
	if (re & POLLERR) ev |= SLIRP_POLL_ERR;
	if (re & POLLHUP) ev |= SLIRP_POLL_HUP;
	return ev;
}

/* Fire expired timers; return ms until the next one (or -1). */
static int run_timers(void)
{
	int64_t now = now_ns() / 1000000, next = -1;
	for (int i = 0; i < MAX_TIMERS; i++) {
		struct timer *t = &timers[i];
		if (!t->used || t->expire_ms < 0)
			continue;
		if (t->expire_ms <= now) {
			t->expire_ms = -1;
			t->cb(t->cb_opaque);
		}
		if (t->used && t->expire_ms >= 0 && (next < 0 || t->expire_ms - now < next))
			next = t->expire_ms > now ? t->expire_ms - now : 0;
	}
	return (int)next;
}

/* -f [udp:]HOSTADDR:HOSTPORT:GUESTPORT */
static void add_forward(Slirp *slirp, const char *spec)
{
	char buf[128], *host, *hport, *gport;
	int udp = 0;
	struct in_addr haddr, gaddr;

	if (strncmp(spec, "udp:", 4) == 0) {
		udp = 1;
		spec += 4;
	} else if (strncmp(spec, "tcp:", 4) == 0) {
		spec += 4;
	}
	snprintf(buf, sizeof(buf), "%s", spec);
	host = buf;
	if (!(hport = strchr(host, ':')) || !(gport = strchr(hport + 1, ':'))) {
		fprintf(stderr, "udock-net: bad forward '%s'\n", spec);
		exit(2);
	}
	*hport++ = 0;
	*gport++ = 0;
	if (!inet_aton(host, &haddr)) {
		fprintf(stderr, "udock-net: bad address '%s'\n", host);
		exit(2);
	}
	inet_aton("10.0.2.15", &gaddr);
	if (slirp_add_hostfwd(slirp, udp, haddr, atoi(hport), gaddr, atoi(gport)) < 0) {
		fprintf(stderr, "udock-net: cannot forward %s:%s (port in use?)\n", host, hport);
		exit(1);
	}
}

int main(int argc, char **argv)
{
	const char *path = NULL;
	const char *fwds[256];
	int nfwds = 0, mtu = 1500, opt;
	static uint8_t frame[MAX_FRAME];

	while ((opt = getopt(argc, argv, "s:m:f:")) != -1) {
		switch (opt) {
		case 's': path = optarg; break;
		case 'm': mtu = atoi(optarg); break;
		case 'f':
			if (nfwds < 256)
				fwds[nfwds++] = optarg;
			break;
		default: goto usage;
		}
	}
	if (!path) {
usage:
		fprintf(stderr, "usage: udock-net -s SOCKET [-m MTU] [-f [udp:]HOSTADDR:HOSTPORT:GUESTPORT]...\n");
		return 2;
	}
	signal(SIGPIPE, SIG_IGN);

	SlirpConfig cfg = {
		.version = 1,
		.in_enabled = true,
		.if_mtu = mtu,
		.if_mru = mtu,
	};
	inet_aton("10.0.2.0", &cfg.vnetwork);
	inet_aton("255.255.255.0", &cfg.vnetmask);
	inet_aton("10.0.2.2", &cfg.vhost);
	inet_aton("10.0.2.15", &cfg.vdhcp_start);
	inet_aton("10.0.2.3", &cfg.vnameserver);
	Slirp *slirp = slirp_new(&cfg, &callbacks, NULL);
	if (!slirp)
		die("slirp_new");
	for (int i = 0; i < nfwds; i++)
		add_forward(slirp, fwds[i]);

	int lfd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
	if (lfd < 0)
		die("socket");
	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
	unlink(path);
	if (bind(lfd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
		die("bind");
	if (listen(lfd, 1) < 0)
		die("listen");
	fprintf(stderr, "udock-net: listening on %s, mtu %d, %d forwards\n", path, mtu, nfwds);

	for (;;) {
		uint32_t timeout = 1000;
		int t;

		npfds = 0;
		/* slot 0: the guest link, or the listener while detached */
		add_poll(guest_fd >= 0 ? guest_fd : lfd, SLIRP_POLL_IN, NULL);
		slirp_pollfds_fill(slirp, &timeout, add_poll, NULL);
		t = run_timers();
		if (t >= 0 && (uint32_t)t < timeout)
			timeout = t;

		int n = poll(pfds, npfds, timeout);
		if (n < 0 && errno != EINTR)
			die("poll");

		if (n > 0 && pfds[0].revents) {
			if (guest_fd < 0) {
				guest_fd = accept(lfd, NULL, NULL);
				if (guest_fd >= 0)
					fprintf(stderr, "udock-net: guest attached\n");
			} else {
				/* drain everything queued from the guest */
				for (;;) {
					ssize_t len = recv(guest_fd, frame, sizeof(frame), MSG_DONTWAIT);
					if (len > 0) {
						slirp_input(slirp, frame, len);
						continue;
					}
					if (len == 0 || (errno != EAGAIN && errno != EINTR)) {
						fprintf(stderr, "udock-net: guest detached\n");
						close(guest_fd);
						guest_fd = -1;
					}
					break;
				}
			}
		}
		slirp_pollfds_poll(slirp, n < 0, get_revents, NULL);
		run_timers();
	}
}

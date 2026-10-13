/*
 * udock-net: unprivileged userspace NAT for the udock UML guest.
 *
 * UML's vector network driver (transport=bess) exchanges raw ethernet frames
 * with us over a UNIX SOCK_SEQPACKET socket, one frame per message. We feed
 * them to libslirp, which turns guest TCP/UDP flows into ordinary host
 * sockets: no tap device, no CAP_NET_ADMIN, no namespaces.
 *
 *   udock-net -s SOCKET [-c CTLSOCK] [-m MTU] [-f tcp|udp:HOSTADDR:HOSTPORT:GUESTPORT]...
 *   udock-net -C CTLSOCK add|del|list ...      (talk to a running instance)
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
#include <sys/time.h>
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

/* ---- port forwards ---- */

struct fwd {
	int udp;
	struct in_addr haddr;
	int hport, gport;
};
static struct fwd fwds[1024];
static int nfwds;

static struct fwd *fwd_find(int udp, struct in_addr haddr, int hport)
{
	for (int i = 0; i < nfwds; i++)
		if (fwds[i].udp == udp && fwds[i].haddr.s_addr == haddr.s_addr && fwds[i].hport == hport)
			return &fwds[i];
	return NULL;
}

/* Returns NULL on success, else a reason. Adding an identical forward is a no-op. */
static const char *fwd_add(Slirp *slirp, int udp, struct in_addr haddr, int hport, int gport)
{
	struct fwd *f = fwd_find(udp, haddr, hport);
	struct in_addr gaddr;

	if (f && f->gport == gport)
		return NULL;
	if (f)
		return "host port already forwarded to another guest port";
	if (nfwds == (int)(sizeof(fwds) / sizeof(fwds[0])))
		return "too many forwards";
	inet_aton("10.0.2.15", &gaddr);
	if (slirp_add_hostfwd(slirp, udp, haddr, hport, gaddr, gport) < 0)
		return "cannot bind host port (in use?)";
	fwds[nfwds++] = (struct fwd){ udp, haddr, hport, gport };
	return NULL;
}

static const char *fwd_del(Slirp *slirp, int udp, struct in_addr haddr, int hport)
{
	struct fwd *f = fwd_find(udp, haddr, hport);

	if (!f)
		return "no such forward";
	slirp_remove_hostfwd(slirp, udp, haddr, hport);
	*f = fwds[--nfwds];
	return NULL;
}

/* "tcp|udp ADDR HOSTPORT [GUESTPORT]" -> fields; returns token count or -1 */
static int parse_fwd(char *proto, char *addr, char *hport, char *gport,
		     int *udp, struct in_addr *haddr, int *hp, int *gp)
{
	if (!proto || !addr || !hport)
		return -1;
	if (strcmp(proto, "tcp") == 0)
		*udp = 0;
	else if (strcmp(proto, "udp") == 0)
		*udp = 1;
	else
		return -1;
	if (!inet_aton(addr, haddr))
		return -1;
	*hp = atoi(hport);
	*gp = gport ? atoi(gport) : *hp;
	return (*hp > 0 && *hp < 65536 && *gp > 0 && *gp < 65536) ? 0 : -1;
}

/*
 * Control socket: one line per connection, one reply.
 *   add tcp|udp HOSTADDR HOSTPORT GUESTPORT  -> "ok" | "err REASON"
 *   del tcp|udp HOSTADDR HOSTPORT            -> "ok" | "err REASON"
 *   list                                     -> "tcp 127.0.0.1 8080 80" lines, then "ok"
 */
static void handle_ctl(Slirp *slirp, int fd)
{
	char line[256], reply[64 * 1024], *save, *cmd, *a[4];
	struct timeval tv = { 1, 0 };
	struct in_addr haddr;
	const char *err = NULL;
	int udp, hp, gp, len = 0, n;
	ssize_t r;

	setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
	while (len < (int)sizeof(line) - 1 && (r = read(fd, line + len, sizeof(line) - 1 - len)) > 0) {
		len += r;
		if (memchr(line, '\n', len))
			break;
	}
	line[len] = 0;
	line[strcspn(line, "\r\n")] = 0;

	cmd = strtok_r(line, " ", &save);
	for (int i = 0; i < 4; i++)
		a[i] = strtok_r(NULL, " ", &save);
	reply[0] = 0;

	if (!cmd) {
		err = "empty command";
	} else if (strcmp(cmd, "list") == 0) {
		n = 0;
		for (int i = 0; i < nfwds; i++)
			n += snprintf(reply + n, sizeof(reply) - n, "%s %s %d %d\n", fwds[i].udp ? "udp" : "tcp",
				      inet_ntoa(fwds[i].haddr), fwds[i].hport, fwds[i].gport);
	} else if (strcmp(cmd, "add") == 0) {
		if (!a[3] || parse_fwd(a[0], a[1], a[2], a[3], &udp, &haddr, &hp, &gp) < 0)
			err = "usage: add tcp|udp HOSTADDR HOSTPORT GUESTPORT";
		else if ((err = fwd_add(slirp, udp, haddr, hp, gp)) == NULL)
			fprintf(stderr, "udock-net: forward %s %s:%d -> %d\n", a[0], a[1], hp, gp);
	} else if (strcmp(cmd, "del") == 0) {
		if (parse_fwd(a[0], a[1], a[2], NULL, &udp, &haddr, &hp, &gp) < 0)
			err = "usage: del tcp|udp HOSTADDR HOSTPORT";
		else if ((err = fwd_del(slirp, udp, haddr, hp)) == NULL)
			fprintf(stderr, "udock-net: unforward %s %s:%d\n", a[0], a[1], hp);
	} else {
		err = "unknown command";
	}

	n = strlen(reply);
	snprintf(reply + n, sizeof(reply) - n, err ? "err %s\n" : "ok\n", err);
	if (write(fd, reply, strlen(reply)) < 0)
		fprintf(stderr, "udock-net: ctl reply: %s\n", strerror(errno));
	close(fd);
}

static int unix_listen(const char *path, int type)
{
	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	int fd = socket(AF_UNIX, type, 0);

	if (fd < 0)
		die("socket");
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
	unlink(path);
	if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
		die(path);
	if (listen(fd, 8) < 0)
		die("listen");
	return fd;
}

/* udock-net -C CTLSOCK COMMAND...: send one control command, print the reply */
static int ctl_client(const char *path, int argc, char **argv)
{
	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	char buf[4096];
	int fd, n = 0, ok = 0;
	ssize_t r;

	for (int i = 0; i < argc; i++)
		n += snprintf(buf + n, sizeof(buf) - n, "%s%s", i ? " " : "", argv[i]);
	snprintf(buf + n, sizeof(buf) - n, "\n");

	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
	if (fd < 0 || connect(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0)
		die(path);
	if (write(fd, buf, strlen(buf)) < 0)
		die("write");
	while ((r = read(fd, buf, sizeof(buf) - 1)) > 0) {
		buf[r] = 0;
		fputs(buf, stdout);
		ok = strstr(buf, "ok\n") != NULL;
	}
	return ok ? 0 : 1;
}

int main(int argc, char **argv)
{
	const char *path = NULL, *ctl = NULL, *specs[256];
	int nspecs = 0, mtu = 1500, opt, lfd, cfd = -1;
	static uint8_t frame[MAX_FRAME];

	while ((opt = getopt(argc, argv, "+s:c:C:m:f:")) != -1) {
		switch (opt) {
		case 's': path = optarg; break;
		case 'c': ctl = optarg; break;
		case 'C': return ctl_client(optarg, argc - optind, argv + optind);
		case 'm': mtu = atoi(optarg); break;
		case 'f':
			if (nspecs < 256)
				specs[nspecs++] = optarg;
			break;
		default: goto usage;
		}
	}
	if (!path) {
usage:
		fprintf(stderr,
			"usage: udock-net -s SOCKET [-c CTLSOCK] [-m MTU] [-f tcp|udp:HOSTADDR:HOSTPORT:GUESTPORT]...\n"
			"       udock-net -C CTLSOCK add|del|list ...\n");
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

	/* -f tcp:127.0.0.1:23760:2376 (same fields as the control "add" command) */
	for (int i = 0; i < nspecs; i++) {
		char buf[128], *save, *f[4];
		struct in_addr haddr;
		int udp, hp, gp;
		const char *err;

		snprintf(buf, sizeof(buf), "%s", specs[i]);
		f[0] = strtok_r(buf, ":", &save);
		for (int j = 1; j < 4; j++)
			f[j] = strtok_r(NULL, ":", &save);
		if (!f[3] || parse_fwd(f[0], f[1], f[2], f[3], &udp, &haddr, &hp, &gp) < 0) {
			fprintf(stderr, "udock-net: bad forward '%s'\n", specs[i]);
			return 2;
		}
		if ((err = fwd_add(slirp, udp, haddr, hp, gp))) {
			fprintf(stderr, "udock-net: %s: %s\n", specs[i], err);
			return 1;
		}
	}

	lfd = unix_listen(path, SOCK_SEQPACKET);
	if (ctl)
		cfd = unix_listen(ctl, SOCK_STREAM);
	fprintf(stderr, "udock-net: listening on %s, mtu %d, %d forwards\n", path, mtu, nfwds);

	for (;;) {
		uint32_t timeout = 1000;
		int t;

		npfds = 0;
		/* slot 0: the guest link, or the listener while detached; slot 1: control */
		add_poll(guest_fd >= 0 ? guest_fd : lfd, SLIRP_POLL_IN, NULL);
		add_poll(cfd, SLIRP_POLL_IN, NULL);
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
		if (n > 0 && pfds[1].revents) {
			int c = accept(cfd, NULL, NULL);
			if (c >= 0)
				handle_ctl(slirp, c);
		}
		slirp_pollfds_poll(slirp, n < 0, get_revents, NULL);
		run_timers();
	}
}

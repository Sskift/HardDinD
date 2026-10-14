/*
 * NVIDIA 550.120 RM_CONTROL 0xd04 (NV0000_CTRL_CMD_CLIENT_SET_INHERITED_
 * SHARE_POLICY) ABI/behavior probe for the HardDinD GPU bridge.
 *
 * Talks raw RM escape ioctls against either a native /dev/nvidiactl node or
 * the guest /dev/harddind-nvidiactl node, with no CUDA dependency.  Every
 * result line keeps raw header/status/bytes so a native run and a guest run
 * can be diffed mechanically.  This is not CUDA acceptance.
 *
 * Build (executable):
 *   gcc -O2 -Wall -Wextra -Werror -static -pthread \
 *       gpu_share_policy_probe.c -o gpu-share-policy-probe
 * Usage: gpu-share-policy-probe <device-prefix> <iterations>
 *   e.g. prefix /dev/nvidia  -> opens /dev/nvidiactl
 *        prefix /dev/harddind-nvidia -> opens /dev/harddind-nvidiactl
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <unistd.h>

/* NVOS escape blocks, fixed against nvidia-550.120 nvos.h. */
struct nvos64 {          /* NVOS64_PARAMETERS (RM_ALLOC) */
	uint32_t hRoot;
	uint32_t hObjectParent;
	uint32_t hObjectNew;
	uint32_t hClass;
	uint64_t pAllocParms;
	uint64_t pRightsRequested;
	uint32_t paramsSize;
	uint32_t flags;
	uint32_t status;
};
struct nvos00 {           /* NVOS00_PARAMETERS (RM_FREE) */
	uint32_t hRoot;
	uint32_t hObjectParent;
	uint32_t hObjectOld;
	uint32_t status;
};
struct nvos54 {           /* NVOS54_PARAMETERS (RM_CONTROL) */
	uint32_t client;
	uint32_t object;
	uint32_t command;
	uint32_t flags;
	uint64_t params;
	uint32_t size;
	uint32_t status;
};

/* rs_access.h:267-272 (550.120). 12 input bytes; no pointers, no FDs. */
struct rs_share_policy {
	uint32_t target;
	uint32_t accessMask;   /* RS_ACCESS_MASK: one NvU32 limb */
	uint16_t type;         /* RS_SHARE_TYPE_* */
	uint8_t  action;       /* RS_SHARE_ACTION_FLAG_* */
	uint8_t  padding;
};
_Static_assert(sizeof(struct nvos64) == 48, "NVOS64");
_Static_assert(sizeof(struct nvos00) == 16, "NVOS00");
_Static_assert(sizeof(struct nvos54) == 32, "NVOS54");
_Static_assert(offsetof(struct nvos54, params) == 16, "NVOS54 params");
_Static_assert(offsetof(struct nvos54, size) == 24, "NVOS54 size");
_Static_assert(offsetof(struct nvos54, status) == 28, "NVOS54 status");
_Static_assert(sizeof(struct rs_share_policy) == 12, "RS_SHARE_POLICY size");
_Static_assert(offsetof(struct rs_share_policy, target) == 0, "target");
_Static_assert(offsetof(struct rs_share_policy, accessMask) == 4, "mask");
_Static_assert(offsetof(struct rs_share_policy, type) == 8, "type");
_Static_assert(offsetof(struct rs_share_policy, action) == 10, "action");

#define ALLOC   _IOWR('F', 0x2b, struct nvos64)
#define FREE    _IOWR('F', 0x29, struct nvos00)
#define CONTROL _IOWR('F', 0x2a, struct nvos54)

#define CMD_SET_INHERITED_SHARE_POLICY 0xd04u

/* rs_access.h share types/action flags, mirrored verbatim. */
#define SH_T_NONE              0u
#define SH_T_ALL               1u
#define SH_T_OS_SECURITY_TOKEN 2u
#define SH_T_CLIENT            3u
#define SH_T_PID               4u
#define SH_T_SMC_PARTITION     5u
#define SH_T_GPU               6u
#define SH_T_FM_CLIENT         7u
#define SH_T_MAX               8u
#define SH_A_REVOKE  0x1u
#define SH_A_REQUIRE 0x2u
#define SH_A_COMPOSE 0x4u

static const char *prefix;
static int failures;
static size_t page_size;

static void hexbuf(char *out, const unsigned char *p, size_t n)
{
	static const char c[] = "0123456789abcdef";
	for (size_t i = 0; i < n; i++) {
		out[2 * i] = c[p[i] >> 4];
		out[2 * i + 1] = c[p[i] & 15];
	}
	out[2 * n] = 0;
}

static void check(const char *name, int ok)
{
	printf("GPU_SHARE_CHECK %s %s\n", name, ok ? "pass" : "fail");
	if (!ok)
		failures = 1;
}

static int open_control(void)
{
	char path[256];

	snprintf(path, sizeof(path), "%sctl", prefix);
	int fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0)
		perror(path);
	return fd;
}

static uint32_t alloc_client(int fd)
{
	struct nvos64 p;

	memset(&p, 0, sizeof(p));
	if (ioctl(fd, ALLOC, &p) != 0 || p.status != 0)
		return 0;
	return p.hObjectNew;
}

static int free_client(int fd, uint32_t client)
{
	struct nvos00 p = { client, client, client, 0 };

	return ioctl(fd, FREE, &p) == 0 && p.status == 0;
}

/*
 * Issue one 0xd04 control in place on the caller's header.  The child pre-
 * image is supplied separately (pointer cases may protect or unmap its pages
 * before the call); read_after says its bytes can still be sampled after.
 * gate: 1 expect success, 0 expect (rc or status) failure, -1 record only.
 */
struct policy_result {
	int rc;
	int err;
	uint32_t status;
};

static struct policy_result
policy_call(const char *name, int fd, struct nvos54 *h,
	    const struct rs_share_policy *child_before, int read_after,
	    int gate)
{
	struct policy_result r;
	struct nvos54 h_before;
	struct rs_share_policy child_after;
	char hb[65], ha[65], cb[25], ca[25];
	const void *child = (const void *)(uintptr_t)h->params;
	int child_changed = -1;

	memcpy(&h_before, h, sizeof(h_before));
	hexbuf(hb, (const unsigned char *)&h_before, 32);
	hexbuf(cb, (const unsigned char *)child_before, 12);
	errno = 0;
	r.rc = ioctl(fd, CONTROL, h);
	r.err = errno;
	r.status = h->status;
	hexbuf(ha, (const unsigned char *)h, 32);
	if (read_after) {
		memcpy(&child_after, child, sizeof(child_after));
		hexbuf(ca, (const unsigned char *)&child_after, 12);
		child_changed = memcmp(child_before, &child_after, 12) != 0;
	} else {
		strcpy(ca, "n/a");
	}
	printf("GPU_SHARE_RESULT {\"case\":\"%s\",\"rc\":%d,\"errno\":%d,"
	       "\"status\":%u,\"client\":\"0x%08x\",\"object\":\"0x%08x\","
	       "\"flags\":%u,\"size\":%u,\"params\":\"0x%016llx\","
	       "\"header_pointer_unchanged\":%s,\"child_changed\":%s,"
	       "\"policy\":{\"target\":\"0x%08x\",\"mask\":\"0x%08x\","
	       "\"type\":%u,\"action\":%u},\"header_before\":\"%s\","
	       "\"header_after\":\"%s\",\"child_before\":\"%s\","
	       "\"child_after\":\"%s\"}\n",
	       name, r.rc, r.err, r.status, h_before.client, h_before.object,
	       h_before.flags, h_before.size,
	       (unsigned long long)h_before.params,
	       h->params == h_before.params ? "true" : "false",
	       child_changed < 0 ? "null" :
	       (child_changed ? "true" : "false"),
	       child_before->target, child_before->accessMask,
	       child_before->type, child_before->action, hb, ha, cb, ca);
	if (gate >= 0) {
		char pname[80];

		check(name, gate ? (r.rc == 0 && r.status == 0)
				: (r.rc != 0 || r.status != 0));
		snprintf(pname, sizeof(pname), "%s_pointer_preserved", name);
		check(pname, r.rc != 0 || h->params == h_before.params);
	}
	return r;
}

static struct rs_share_policy pol(uint32_t target, uint32_t mask,
				  uint16_t type, uint8_t action)
{
	struct rs_share_policy p;

	memset(&p, 0, sizeof(p));
	p.target = target;
	p.accessMask = mask;
	p.type = type;
	p.action = action;
	return p;
}

static struct nvos54 hdr(uint32_t client, uint32_t object,
			 const struct rs_share_policy *child, uint32_t flags,
			 uint32_t size)
{
	struct nvos54 h;

	memset(&h, 0, sizeof(h));
	h.client = client;
	h.object = object;
	h.command = CMD_SET_INHERITED_SHARE_POLICY;
	h.flags = flags;
	h.params = (uintptr_t)child;
	h.size = size;
	return h;
}

/* One lifecycle case against an ordinary writable stack child. */
static struct policy_result
simple_case(const char *name, int fd, uint32_t client, uint32_t object,
	    struct rs_share_policy in, int gate)
{
	struct nvos54 h = hdr(client, object, &in, 0, sizeof(in));

	return policy_call(name, fd, &h, &in, 1, gate);
}

/* ---- pointer / protection cases --------------------------------------- */

struct guard {
	unsigned char *mem;
	size_t len;
};

static struct guard map_guard(size_t pages)
{
	struct guard g;

	g.len = pages * page_size;
	g.mem = mmap(NULL, g.len, PROT_READ | PROT_WRITE,
		     MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (g.mem == MAP_FAILED) {
		perror("mmap");
		exit(2);
	}
	memset(g.mem, 0xa5, g.len);
	return g;
}

static void unmap_guard(struct guard *g)
{
	if (munmap(g->mem, g->len))
		perror("munmap");
}

static void raw_header_case(const char *name, int fd, struct nvos54 *hp)
{
	errno = 0;
	int rc = ioctl(fd, CONTROL, hp);
	int err = errno;

	printf("GPU_SHARE_RESULT {\"case\":\"%s\",\"rc\":%d,\"errno\":%d}\n",
	       name, rc, err);
	check(name, rc < 0 && err == EFAULT);
}

static void pointer_cases(int fd, uint32_t client)
{
	struct rs_share_policy p = pol(0, 1, SH_T_ALL, 0);
	struct nvos54 h;

	/* Unreachable child addresses: header stays on the stack; the driver
	 * must reject the nested pointer itself. */
	h = hdr(client, client, NULL, 0, 12);
	policy_call("child_null", fd, &h, &p, 0, 0);

	h = hdr(client, client, (void *)1, 0, 12);
	policy_call("child_low_1", fd, &h, &p, 0, 0);

	h = hdr(client, client, (void *)(1ULL << 63), 0, 12);
	policy_call("child_noncanonical_2_63", fd, &h, &p, 0, 0);

	h.params = UINT64_MAX;
	policy_call("child_all_ones", fd, &h, &p, 0, 0);

	/* Read-only reachable child (input only): recorded, not gated, so the
	 * runner can compare the driver's exact copy-back behaviour. */
	{
		struct guard g = map_guard(2);
		struct rs_share_policy *cp = (void *)(g.mem + page_size / 2);
		struct rs_share_policy before;
		struct nvos54 hh;

		*cp = p;
		before = *cp;
		if (mprotect(g.mem, g.len, PROT_READ))
			exit(2);
		hh = hdr(client, client, cp, 0, 12);
		policy_call("child_readonly", fd, &hh, &before, 1, -1);
		unmap_guard(&g);
	}

	/* Fully unreachable child page. */
	{
		struct guard g = map_guard(2);
		struct rs_share_policy *cp = (void *)(g.mem + page_size / 2);
		struct rs_share_policy before = *cp;
		struct nvos54 hh;

		*cp = p;
		before = *cp;
		hh = hdr(client, client, cp, 0, 12);
		if (mprotect(g.mem, g.len, PROT_NONE))
			exit(2);
		policy_call("child_protnone", fd, &hh, &before, 0, 0);
		unmap_guard(&g);
	}

	/* Child straddling a page boundary: last 6 bytes land on page 2. */
	struct {
		const char *name;
		int first_prot;
		int second_prot;
		int gate;
		int read_after;
	} cross[] = {
		{ "child_crosspage_rw_rw", PROT_READ | PROT_WRITE,
		  PROT_READ | PROT_WRITE, 1, 1 },
		{ "child_crosspage_rw_ro", PROT_READ | PROT_WRITE,
		  PROT_READ, -1, 1 },
		{ "child_crosspage_rw_none", PROT_READ | PROT_WRITE,
		  PROT_NONE, 0, 0 },
		{ "child_crosspage_ro_rw", PROT_READ,
		  PROT_READ | PROT_WRITE, -1, 1 },
	};
	for (size_t i = 0; i < sizeof(cross) / sizeof(cross[0]); i++) {
		struct guard g = map_guard(2);
		unsigned char *at = g.mem + page_size - 6;
		struct rs_share_policy *cp = (void *)at;
		struct rs_share_policy before;
		struct nvos54 hh;

		memcpy(at, &p, 12);
		before = p;
		if (mprotect(g.mem, page_size, cross[i].first_prot))
			exit(2);
		if (mprotect(g.mem + page_size, page_size,
			     cross[i].second_prot))
			exit(2);
		hh = hdr(client, client, cp, 0, 12);
		policy_call(cross[i].name, fd, &hh, &before,
			    cross[i].read_after, cross[i].gate);
		if (mprotect(g.mem, g.len, PROT_READ | PROT_WRITE))
			exit(2);
		unmap_guard(&g);
	}

	/* Header faults are ordinary ioctl -EFAULT on both driver paths. */
	{
		struct guard g = map_guard(2);
		struct nvos54 *hp = (void *)(g.mem + page_size / 2);
		struct rs_share_policy cp = p;

		*hp = hdr(client, client, &cp, 0, 12);
		if (mprotect(g.mem, g.len, PROT_READ))
			exit(2);
		raw_header_case("header_readonly", fd, hp);
		if (mprotect(g.mem, g.len, PROT_READ | PROT_WRITE))
			exit(2);
		unmap_guard(&g);
	}
	{
		struct guard g = map_guard(2);
		struct nvos54 *hp = (void *)(g.mem + page_size / 2);
		struct rs_share_policy cp = p;

		*hp = hdr(client, client, &cp, 0, 12);
		if (mprotect(g.mem, g.len, PROT_NONE))
			exit(2);
		raw_header_case("header_protnone", fd, hp);
		unmap_guard(&g);
	}

	/*
	 * Size/flags variants are recorded raw.  The native driver answers
	 * through its RM status word; the bridge whitelists size==12/flags==0
	 * and returns -EOPNOTSUPP, so guest lines intentionally differ here.
	 * The child sits at the start of a page followed by a PROT_NONE guard,
	 * making any oversized driver copy fault deterministically.
	 */
	static const uint32_t sizes[] = { 0, 4, 8, 11, 13, 16, 32, 4096 };
	for (size_t i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
		struct guard g = map_guard(2);
		struct rs_share_policy *cp = (void *)g.mem;
		struct rs_share_policy before;
		struct nvos54 hh;
		char name[40];

		*cp = p;
		before = p;
		if (mprotect(g.mem + page_size, page_size, PROT_NONE))
			exit(2);
		hh = hdr(client, client, cp, 0, sizes[i]);
		snprintf(name, sizeof(name), "size_%u", sizes[i]);
		policy_call(name, fd, &hh, &before, sizes[i] >= 12, -1);
		if (mprotect(g.mem + page_size, page_size,
			     PROT_READ | PROT_WRITE))
			exit(2);
		unmap_guard(&g);
	}
	static const uint32_t flags_v[] = { 1, 2, 0x80000000u, 0xffffffffu };
	for (size_t i = 0; i < sizeof(flags_v) / sizeof(flags_v[0]); i++) {
		char name[40];
		struct rs_share_policy in = p;
		struct nvos54 hh = hdr(client, client, &in, flags_v[i], 12);

		snprintf(name, sizeof(name), "flags_0x%x", flags_v[i]);
		policy_call(name, fd, &hh, &in, 1, -1);
	}
}

/* ---- lifecycle, identity and error matrix ----------------------------- */

static void lifecycle(int fd, uint32_t client, uint32_t other, int gate)
{
	/* Replace list with an ALL/DUP policy. */
	simple_case("replace_all_dup", fd, client, client,
		    pol(0, 1, SH_T_ALL, 0), gate);
	/* Compose an ALL/DEBUG policy: both owner-grantable rights. */
	simple_case("compose_all_debug", fd, client, client,
		    pol(0, 4, SH_T_ALL, SH_A_COMPOSE), gate);
	/* Require/allow policies keyed by PID; target is informational here. */
	simple_case("require_pid_dup", fd, client, client,
		    pol((uint32_t)getpid(), 1, SH_T_PID, SH_A_REQUIRE), gate);
	simple_case("allow_pid_dup", fd, client, client,
		    pol((uint32_t)getpid(), 1, SH_T_PID, 0), gate);
	/* Security-token policy (target field unused by the driver). */
	simple_case("allow_token_dup", fd, client, client,
		    pol(0, 1, SH_T_OS_SECURITY_TOKEN, 0), gate);
	/* Self-client share, then sibling-client targeting and revocation. */
	simple_case("client_self_dup", fd, client, client,
		    pol(client, 1, SH_T_CLIENT, 0), gate);
	if (other) {
		simple_case("client_existing_dup", fd, client, client,
			    pol(other, 1, SH_T_CLIENT, 0), gate);
		simple_case("client_existing_revoke", fd, client, client,
			    pol(other, 1, SH_T_CLIENT, SH_A_REVOKE), gate);
	}
	simple_case("client_unknown_dup", fd, client, client,
		    pol(0xdeadbeef, 1, SH_T_CLIENT, 0), 0);
	simple_case("revoke_all_dup", fd, client, client,
		    pol(0, 1, SH_T_ALL, SH_A_REVOKE), gate);
	simple_case("revoke_pid_nonexistent", fd, client, client,
		    pol((uint32_t)getpid(), 1, SH_T_PID, SH_A_REVOKE), gate);
	/* Reinstall so the client is left with a usable policy list. */
	simple_case("restore_all_dup", fd, client, client,
		    pol(0, 1, SH_T_ALL, 0), gate);
}

static void type_and_rights_matrix(int fd, uint32_t client)
{
	static const uint16_t types[] = {
		SH_T_NONE, SH_T_ALL, SH_T_OS_SECURITY_TOKEN, SH_T_CLIENT,
		SH_T_PID, SH_T_SMC_PARTITION, SH_T_GPU, SH_T_FM_CLIENT,
		SH_T_MAX, 9, 0xffff
	};
	for (size_t i = 0; i < sizeof(types) / sizeof(types[0]); i++) {
		char name[40];
		uint32_t target = types[i] == SH_T_CLIENT ? client :
				  types[i] == SH_T_PID ? (uint32_t)getpid() : 0;

		snprintf(name, sizeof(name), "type_%u", types[i]);
		/* Driver rejects type >= RS_SHARE_TYPE_MAX with RM status. */
		simple_case(name, fd, client, client,
			    pol(target, 1, types[i], 0),
			    types[i] < SH_T_MAX ? 1 : 0);
	}

	/* DUP_OBJECT (bit0) and DEBUG (bit2) are owner-grantable; NICE (bit1)
	 * requires a privileged host client. */
	simple_case("mask_dup_only", fd, client, client,
		    pol(0, 1, SH_T_ALL, 0), 1);
	simple_case("mask_debug_only", fd, client, client,
		    pol(0, 4, SH_T_ALL, SH_A_COMPOSE), 1);
	{
		struct policy_result r = simple_case(
			"mask_nice_only", fd, client, client,
			pol(0, 2, SH_T_ALL, 0), -1);
		printf("GPU_SHARE_NOTE mask_nice_only uid=%d euid=%d rc=%d "
		       "status=0x%x (nonzero status means the host client "
		       "lacks privilege)\n",
		       getuid(), geteuid(), r.rc, r.status);
	}
	simple_case("mask_zero", fd, client, client,
		    pol(0, 0, SH_T_ALL, 0), -1);
	simple_case("mask_unknown_bits", fd, client, client,
		    pol(0, 0xfffffff8u, SH_T_ALL, 0), -1);

	static const uint8_t actions[] = {
		0, SH_A_REVOKE, SH_A_REQUIRE, SH_A_REVOKE | SH_A_REQUIRE,
		SH_A_COMPOSE, SH_A_COMPOSE | SH_A_REQUIRE,
		SH_A_COMPOSE | SH_A_REVOKE, 7, 0xff
	};
	for (size_t i = 0; i < sizeof(actions) / sizeof(actions[0]); i++) {
		char name[32];

		snprintf(name, sizeof(name), "action_%u", actions[i]);
		simple_case(name, fd, client, client,
			    pol(0, 1, SH_T_ALL, actions[i]), -1);
		/* Reset to a known policy list after odd combinations. */
		simple_case("replace_all_dup", fd, client, client,
			    pol(0, 1, SH_T_ALL, 0), 1);
	}
}

static void client_error_cases(int fd, uint32_t client, uint32_t other)
{
	struct rs_share_policy p = pol(0, 1, SH_T_ALL, 0);

	/* Header-level identity errors, decided by the host driver. */
	simple_case("zero_client_object", fd, 0, 0, p, 0);
	simple_case("zero_object", fd, client, 0, p, 0);
	simple_case("unknown_client", fd, 0xdeadbeef, 0xdeadbeef, p, 0);
	if (other) {
		simple_case("object_other_client", fd, client, other, p, 0);
		simple_case("client_object_swapped", fd, other, client, p, 0);
	}

	/* An unknown command stays unavailable on both paths; raw lines let
	 * the runner compare RM status vs bridge errno. */
	struct rs_share_policy in = p;
	struct nvos54 h = hdr(client, client, &in, 0, 12);
	h.command = 0xd0e;
	policy_call("unknown_command_d0e", fd, &h, &in, 1, -1);
}

/* ---- concurrency ------------------------------------------------------- */

struct thread_args {
	int fd;
	uint32_t client;
	int own_client;
	unsigned iterations;
	int okay;
};

static void *thread_worker(void *arg)
{
	struct thread_args *t = arg;
	int fd = t->fd;
	uint32_t client = t->client;

	t->okay = 1;
	if (t->own_client) {
		fd = open_control();
		if (fd < 0) {
			t->okay = 0;
			return NULL;
		}
		client = alloc_client(fd);
		if (!client) {
			t->okay = 0;
			close(fd);
			return NULL;
		}
	}
	for (unsigned i = 0; i < t->iterations; i++) {
		struct rs_share_policy a = pol(0, 1, SH_T_ALL, 0);
		struct rs_share_policy b = pol(0, 1, SH_T_ALL, SH_A_REVOKE);
		struct nvos54 h1 = hdr(client, client, &a, 0, 12);
		struct nvos54 h2 = hdr(client, client, &b, 0, 12);

		if (ioctl(fd, CONTROL, &h1) != 0 || h1.status != 0 ||
		    ioctl(fd, CONTROL, &h2) != 0 || h2.status != 0) {
			t->okay = 0;
			break;
		}
	}
	if (t->own_client) {
		if (!free_client(fd, client))
			t->okay = 0;
		close(fd);
	}
	return NULL;
}

static void thread_cases(int fd, uint32_t client, unsigned iterations)
{
	enum { N = 4 };
	pthread_t th[N];
	struct thread_args t[N];
	int okay;

	for (int i = 0; i < N; i++) {
		t[i] = (struct thread_args){ fd, client, 0, iterations, 0 };
		if (pthread_create(&th[i], NULL, thread_worker, &t[i]))
			exit(2);
	}
	okay = 1;
	for (int i = 0; i < N; i++) {
		pthread_join(th[i], NULL);
		okay = okay && t[i].okay;
	}
	check("four_threads_shared_client", okay);

	for (int i = 0; i < N; i++) {
		t[i] = (struct thread_args){ fd, 0, 1, iterations, 0 };
		if (pthread_create(&th[i], NULL, thread_worker, &t[i]))
			exit(2);
	}
	okay = 1;
	for (int i = 0; i < N; i++) {
		pthread_join(th[i], NULL);
		okay = okay && t[i].okay;
	}
	check("four_threads_own_clients", okay);
}

/* Fresh child opens must work. Inherited-FD use is a separately reported
 * contract: the current bridge rejects it, which remains a failed case. */
static void process_operation(const char *name, int fd, uint32_t target)
{
	struct nvos64 a = {0};
	struct nvos00 f = {0};
	struct nvos54 h = {0};
	struct rs_share_policy p = pol(target, 1,
				      target ? SH_T_CLIENT : SH_T_ALL, 0);
	int ar = -1, ae = 0, cr = -1, ce = 0, fr = -1, fe = 0;
	if (fd >= 0) {
		errno = 0; ar = ioctl(fd, ALLOC, &a); ae = errno;
		if (ar == 0 && a.status == 0 && a.hObjectNew) {
			h = hdr(a.hObjectNew, a.hObjectNew, &p, 0, 12);
			errno = 0; cr = ioctl(fd, CONTROL, &h); ce = errno;
			f = (struct nvos00){a.hObjectNew, a.hObjectNew, a.hObjectNew, 0};
			errno = 0; fr = ioctl(fd, FREE, &f); fe = errno;
		}
	}
	int good = fd >= 0 && ar == 0 && a.status == 0 && a.hObjectNew &&
		   cr == 0 && h.status == 0 && fr == 0 && f.status == 0;
	printf("GPU_SHARE_RESULT {\"case\":\"%s\",\"open_ok\":%s,"
	       "\"alloc_rc\":%d,\"alloc_errno\":%d,\"alloc_status\":%u,"
	       "\"allocated\":%s,\"control_called\":%s,\"control_rc\":%d,"
	       "\"control_errno\":%d,\"control_status\":%u,\"free_rc\":%d,"
	       "\"free_errno\":%d,\"free_status\":%u,\"ok\":%s}\n",
	       name, fd >= 0 ? "true" : "false", ar, ae, a.status,
	       a.hObjectNew ? "true" : "false", a.hObjectNew ? "true" : "false",
	       cr, ce, h.status, fr, fe, f.status, good ? "true" : "false");
	if (fd >= 0) close(fd);
	_exit(good ? 0 : 1);
}

static void process_cases(int fd, uint32_t client)
{
	for (int i = 0; i < 5; i++) {
		char name[64];
		if (i < 4) snprintf(name, sizeof(name), "independent_process_%d", i);
		else snprintf(name, sizeof(name), "inherited_fd_cross_client");
		pid_t pid = fork();
		if (pid < 0) exit(2);
		if (pid == 0) {
			if (i < 4) {
				close(fd);
				process_operation(name, open_control(), 0);
			} else process_operation(name, fd, client);
		}
		int status = 0;
		check(name, waitpid(pid, &status, 0) == pid &&
		      WIFEXITED(status) && WEXITSTATUS(status) == 0);
	}
}

int main(int argc, char **argv)
{
	if (argc != 3 || atoi(argv[2]) < 1) {
		fprintf(stderr,
			"usage: %s <device-prefix> <iterations>\n", argv[0]);
		return 2;
	}
	prefix = argv[1];
	unsigned iterations = (unsigned)atoi(argv[2]);
	page_size = (size_t)sysconf(_SC_PAGESIZE);
	setvbuf(stdout, NULL, _IOLBF, 0);

	struct utsname u;
	uname(&u);
	char link[256] = "";
	int fd = open_control();

	if (fd >= 0) {
		char p[300];
		snprintf(p, sizeof(p), "/proc/self/fd/%d", fd);
		ssize_t n = readlink(p, link, sizeof(link) - 1);
		if (n > 0)
			link[n] = 0;
	}
	printf("GPU_SHARE_BEGIN {\"pid\":%d,\"ppid\":%d,\"tid\":%ld,"
	       "\"uid\":%d,\"euid\":%d,\"prefix\":\"%s\",\"fd\":%d,"
	       "\"fd_link\":\"%s\",\"page_size\":%zu,\"kernel\":\"%s %s\","
	       "\"internal_cuda_verified\":0}\n",
	       getpid(), getppid(), (long)gettid(), getuid(), geteuid(),
	       prefix, fd, link, page_size, u.sysname, u.release);
	check("open_control", fd >= 0);
	if (fd < 0)
		return 1;

	uint32_t client = alloc_client(fd);
	check("alloc_client_a", client != 0);
	if (!client) {
		close(fd);
		return 1;
	}
	uint32_t other = alloc_client(fd);
	check("alloc_client_b", other != 0);
	printf("GPU_SHARE_IDENTITIES {\"client_a\":%u,\"client_b\":%u,\"pid\":%d}\n", client, other, getpid());

	lifecycle(fd, client, other, 1);
	type_and_rights_matrix(fd, client);
	client_error_cases(fd, client, other);
	pointer_cases(fd, client);

	/* Recovery: channel and ordinary RM calls still behave after every
	 * deliberately invalid input. */
	lifecycle(fd, client, other, 1);
	simple_case("post_fault_control", fd, client, client,
		    pol(0, 1, SH_T_ALL, 0), 1);

	thread_cases(fd, client, iterations);
	process_cases(fd, client);

	check("free_client_b", other ? free_client(fd, other) : 1);
	check("free_client_a", free_client(fd, client));
	close(fd);
	printf("GPU_SHARE_END status=%d internal_cuda_verified=0\n", failures);
	return failures;
}

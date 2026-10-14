// SPDX-License-Identifier: GPL-2.0
/* Experimental NVIDIA control transport: queries, FD association and RM
 * root-client alloc/free.  Not a CUDA device backend: device mmap, UVM,
 * pinned-memory and nested-object allocation are still absent. */
#include <linux/fs.h>
#include <linux/file.h>
#include <linux/init.h>
#include <linux/ioctl.h>
#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/pagemap.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <asm/tlbflush.h>
#include <as-layout.h>
#include <init.h>
#include <linux/completion.h>
#include <linux/interrupt.h>
#include <linux/kstrtox.h>
#include <linux/ktime.h>
#include <linux/math64.h>
#include <irq_kern.h>
#include <os.h>
#include <skas.h>
#include <stub-data.h>

/*
 * NVIDIA driver 550.120 RM parameter blocks, fixed against the open-gpu-
 * kernel-modules reference commit in sources.lock.json:
 *   src/common/sdk/nvidia/inc/nvos.h:159-165  NVOS00_PARAMETERS (RM_FREE)
 *   src/common/sdk/nvidia/inc/nvos.h:454-463  NVOS21_PARAMETERS (RM_ALLOC)
 *   src/common/sdk/nvidia/inc/nvos.h:471-480  NVOS64_PARAMETERS (RM_ALLOC)
 *   nvos.h:168/173/183  NV01_ROOT 0x00 / NV01_ROOT_NON_PRIV 0x01 /
 *                       NV01_ROOT_CLIENT 0x41
 *   src/nvidia/arch/nvalloc/unix/include/nv_escape.h:29/31  0x29/0x2b
 *   .../nv-ioctl-numbers.h:29  magic 'F', encoded with _IOWR
 * escape.c:373 accepts both 32-byte NVOS21 and 48-byte NVOS64 for
 * NV_ESC_RM_ALLOC; escape.c:424 requires the 16-byte NVOS00 for FREE.
 */
struct gpu_nvos00 {
	u32 hRoot;
	u32 hObjectParent;
	u32 hObjectOld;
	u32 status;
};

struct gpu_nvos21 {
	u32 hRoot;
	u32 hObjectParent;
	u32 hObjectNew;
	u32 hClass;
	u64 pAllocParms;
	u32 paramsSize;
	u32 status;
};

struct gpu_nvos64 {
	u32 hRoot;
	u32 hObjectParent;
	u32 hObjectNew;
	u32 hClass;
	u64 pAllocParms;
	u64 pRightsRequested;
	u32 paramsSize;
	u32 flags;
	u32 status;
};

static_assert(sizeof(struct gpu_nvos00) == 16);
static_assert(sizeof(struct gpu_nvos21) == 32);
static_assert(sizeof(struct gpu_nvos64) == 48);
static_assert(offsetof(struct gpu_nvos00, status) == 12);
static_assert(offsetof(struct gpu_nvos21, pAllocParms) == 16);
static_assert(offsetof(struct gpu_nvos21, paramsSize) == 24);
static_assert(offsetof(struct gpu_nvos21, status) == 28);
static_assert(offsetof(struct gpu_nvos64, pAllocParms) == 16);
static_assert(offsetof(struct gpu_nvos64, pRightsRequested) == 24);
static_assert(offsetof(struct gpu_nvos64, paramsSize) == 32);
static_assert(offsetof(struct gpu_nvos64, flags) == 36);
static_assert(offsetof(struct gpu_nvos64, status) == 40);

int uml_gpu_bridge;

/* 0017: schedulable completion of GPU worker ioctls.  Default off; the 0016
 * local_irq_save handoff stays the fallback. */
int uml_gpu_async_wait;
/* Bit 0: rate-limited wait latency log.  Bit 1: worker injects ~0.1 s of
 * host-side stall before each ioctl (scheduling probe).  Default 0. */
/* Bit 2 logs submissions for a controlled worker-death test. */
static unsigned int gpu_async_debug;

static atomic_t gpu_async_waits;
static atomic64_t gpu_async_total_ns;
static atomic_t gpu_async_max_ns;

static int __init gpu_setup(char *line, int *add)
{
	*add = 0;
	if (!strcmp(line, "1"))
		uml_gpu_bridge = 1;
	return 0;
}
__uml_setup("gpu_bridge=", gpu_setup,
"gpu_bridge=1\n"
"    Enable the experimental NVIDIA transport: control queries, REGISTER_FD\n"
"    and root-client RM_ALLOC/FREE. No device mmap or CUDA backend yet.\n");

static int __init gpu_async_setup(char *line, int *add)
{
	int value;

	*add = 0;
	if (!kstrtoint(line, 0, &value) && value == 1)
		uml_gpu_async_wait = 1;
	return 0;
}
__uml_setup("gpu_async_wait=", gpu_async_setup,
"gpu_async_wait=1\n"
"    Wait for GPU worker ioctls as a schedulable guest task with interrupts\n"
"    enabled, instead of a host futex wait under local_irq_save.  Requires\n"
"    gpu_bridge=1; defaults to off.  The wait is not interruptible-cancel:\n"
"    guest signals do not abandon an in-flight host ioctl.\n");

static int __init gpu_async_debug_setup(char *line, int *add)
{
	unsigned int value;

	*add = 0;
	if (!kstrtouint(line, 0, &value) && value <= 7)
		gpu_async_debug = value;
	return 0;
}
__uml_setup("gpu_async_debug=", gpu_async_debug_setup,
"gpu_async_debug=bits\n"
"    0017 diagnostics (default 0): bit0 logs aggregated wait latency every\n"
"    1024 commands; bit1 makes the GPU worker stall ~0.1 s before each ioctl\n"
"    so a uniprocessor guest can prove scheduling continues during waits.\n"
"    bit2 logs each submitted request for controlled worker-death tests.\n");

/* The worker writes exactly one zero datagram per finished batch.  gpu_lock
 * permits one outstanding batch per mm, so each datagram wakes one waiter.
 * Edge-triggered epoll requires draining the socket here. */
static irqreturn_t gpu_completion_irq(int irq, void *dev)
{
	struct mm_struct *mm = dev;
	char byte;
	int n, received = 0;

	while ((n = os_read_file(mm->context.gpu_id.sock, &byte, 1)) == 1)
		received++;
	if (n != -EAGAIN && n != -EWOULDBLOCK)
		pr_err_ratelimited("harddind-gpu: completion socket read error %d\n",
				   n);
	if (!received)
		return IRQ_NONE;
	if (received > 1 || byte != 0)
		pr_err_ratelimited("harddind-gpu: %d unexpected completion bytes\n",
				   received);
	complete(&mm->context.gpu_done);
	return IRQ_HANDLED;
}

static void gpu_async_record(u64 waited_ns)
{
	int n = atomic_inc_return(&gpu_async_waits);
	int old = atomic_read(&gpu_async_max_ns);
	int max_ns = min_t(u64, waited_ns, INT_MAX);
	u64 total;

	atomic64_add(waited_ns, &gpu_async_total_ns);
	while (max_ns > old) {
		if (atomic_try_cmpxchg(&gpu_async_max_ns, &old, max_ns))
			break;
	}
	if (n % 1024 == 0) {
		total = atomic64_read(&gpu_async_total_ns);
		pr_info("HARDDIND_GPU_ASYNC_WAIT n=%d avg_us=%llu max_us=%u\n",
			n, div64_u64(total, (u64)n * 1000),
			atomic_read(&gpu_async_max_ns) / 1000);
	}
}

/* Index 255 is the NVIDIA control minor; 0..254 are physical GPUs.
 * Nodes remain diagnostic names until CUDA and device mappings are supported. */
struct gpu_device {
	struct cdev cdev;
	struct device *device;
	unsigned int index;
};
/* libcuda validates st_rdev and recreates mismatched nodes as 195:minor.
 * Use the NVIDIA ABI in this guest only; no outer device rules change. */
#define GPU_MAJOR 195
static struct class *gpu_class;
static struct gpu_device gpu_devices[256];
static const struct file_operations gpu_fops;

struct gpu_file {
	struct mm_struct *mm;
	int host_fd;
};

/* gpu_lock serializes the device channel independently of the mapping broker. */
static int gpu_start(struct mm_struct *mm)
{
	struct mm_id *id = &mm->context.gpu_id;
	int error;

	if (id->stack)
		return id->pid > 1 ? 0 : -ECHILD;
	id->stack = __get_free_pages(GFP_KERNEL | __GFP_ZERO,
				    ilog2(STUB_THREAD_PAGES));
	if (!id->stack)
		return -ENOMEM;
	id->cached_phys_fd = -1;
	scoped_guard(spinlock_irqsave, &mm->context.sync_tlb_lock)
		error = start_userspace_cpu(&mm->context.id, id, STUB_GPU_DATA);
	if (error) {
		/* Keep a failed slot for destroy_context to reap and free. */
		return error;
	}
	/* Set the delay for both paths so the scheduling A/B has the same
	 * worker workload. It remains disabled unless explicitly requested. */
	((struct stub_data *)id->stack)->gpu_debug = (unsigned char)gpu_async_debug;
	if (uml_gpu_async_wait) {
		int irq;

		init_completion(&mm->context.gpu_done);
		irq = um_request_irq(UM_IRQ_ALLOC, id->sock, IRQ_READ,
				     gpu_completion_irq, 0,
				     "harddind-gpu", mm);
		if (irq < 0) {
			pr_err("harddind-gpu: completion IRQ setup failed: %d\n",
			       irq);
			os_close_file(id->sock);
			id->sock = 0;
			os_kill_ptraced_process(id->pid, 1);
			free_pages(id->stack, ilog2(STUB_THREAD_PAGES));
			memset(id, 0, sizeof(*id));
			return irq;
		}
		mm->context.gpu_async_irq = irq;
		pr_info("HARDDIND_GPU_ASYNC guest_tgid=%d host_pid=%d irq=%d\n",
			task_tgid_nr(current), id->pid, irq);
	}
	mm->context.gpu_owner = get_task_pid(current, PIDTYPE_TGID);
	pr_info("HARDDIND_GPU_WORKER guest_tgid=%d host_pid=%d\n",
		 task_tgid_nr(current), id->pid);
	return 0;
}

static long gpu_command(struct mm_struct *mm, int operation, int fd,
			unsigned int command, unsigned long argument,
			void *payload, size_t payload_size)
{
	struct mm_id *id = &mm->context.gpu_id;
	unsigned long flags;
	long result;

	if (!uml_gpu_async_wait) {
		/* Small synchronous commands are exposed: control queries,
		 * REGISTER_FD and root-client alloc/free; there is still no
		 * device mmap or CUDA path.  The handoff waits with guest
		 * interrupts disabled, so anything that can block in the host
		 * GPU driver cannot use this transport. */
		local_irq_save(flags);
		result = os_gpu_command(id, operation, fd, command,
					argument, payload, payload_size);
		local_irq_restore(flags);
		return result;
	}

	/* Asynchronous path: the submission is a short, nonblocking handoff
	 * (SCM_RIGHTS plus futex wake), still under local_irq_save; only the
	 * wait for the host ioctl becomes schedulable. */
	reinit_completion(&mm->context.gpu_done);
	local_irq_save(flags);
	result = os_gpu_command_submit(id, operation, fd, command,
				       argument, payload, payload_size);
	local_irq_restore(flags);
	if (result < 0)
		return result;
	if (gpu_async_debug & 4)
		pr_info("HARDDIND_GPU_ASYNC_SUBMIT host_pid=%d operation=%d command=0x%x\n",
			id->pid, operation, command);

	{
		u64 started = 0;

		if (gpu_async_debug & 1)
			started = ktime_get_ns();
		/* Schedulable, interrupts-enabled wait: other guest tasks and
		 * timers keep running on this vCPU while the dedicated worker
		 * (same host TGID/mm/FD identity) executes the ioctl.  This is
		 * NOT interruptible cancellation: a guest signal must never
		 * drop a payload or host descriptor the worker is still using,
		 * and an abandoned batch could not be safely reclaimed.  The
		 * SIGCHLD reaper and mm teardown complete the wait explicitly,
		 * which then surfaces as -ECHILD. */
		wait_for_completion(&mm->context.gpu_done);
		if (gpu_async_debug & 1)
			gpu_async_record(ktime_get_ns() - started);
	}

	if (READ_ONCE(id->pid) < 0)
		return -ECHILD;
	return os_gpu_command_result(id, payload, payload_size);
}

static int gpu_open(struct inode *inode, struct file *file)
{
	struct mm_struct *mm = current->mm;
	struct gpu_device *device = container_of(inode->i_cdev, struct gpu_device, cdev);
	struct gpu_file *state;
	long result;

	if (!uml_gpu_bridge || !parallel_userspace || !mm)
		return -EOPNOTSUPP;
	state = kzalloc(sizeof(*state), GFP_KERNEL);
	if (!state)
		return -ENOMEM;
	mutex_lock(&mm->context.gpu_lock);
	result = gpu_start(mm);
	if (!result && task_tgid(current) != mm->context.gpu_owner)
		result = -EXDEV;
	if (!result)
		result = gpu_command(mm, 0, -1, device->index, 0, NULL, 0);
	if (result >= 0) {
		mmgrab(mm);
		state->mm = mm;
		state->host_fd = result;
		file->private_data = state;
	}
	mutex_unlock(&mm->context.gpu_lock);
	if (result < 0) {
		kfree(state);
		return result;
	}
	return 0;
}

/* REGISTER_FD has one embedded guest FD. Never rewrite the user's buffer:
 * another thread may read it, and a host FD is meaningless in the guest. */
static long gpu_register_fd(struct gpu_file *state, unsigned int command,
                            unsigned long arg)
{
	struct file *source;
	struct gpu_file *other;
	int guest_fd, host_fd = -1;
	long result;

	if (copy_from_user(&guest_fd, (void __user *)arg, sizeof(guest_fd)))
		return -EFAULT;
	source = fget(guest_fd);
	if (source && source->f_op == &gpu_fops) {
		other = source->private_data;
		if (other->mm != state->mm) {
			fput(source);
			return -EXDEV;
		}
		host_fd = other->host_fd;
	}
	/* Invalid or unrelated descriptors are represented by -1, never by a
	 * guest integer that could accidentally name an unrelated host file. */
	mutex_lock(&state->mm->context.gpu_lock);
	result = gpu_command(state->mm, 3, state->host_fd, command, host_fd,
			     NULL, 0);
	mutex_unlock(&state->mm->context.gpu_lock);
	if (source)
		fput(source);
	/* NVIDIA performs the registration before copying the argument back.
	 * Preserve its EFAULT-after-side-effect semantics for read-only pages. */
	if (result != -EFAULT &&
	    copy_to_user((void __user *)arg, &guest_fd, sizeof(guest_fd)))
		return -EFAULT;
	return result;
}

/* Copy the RM block back unless the host ioctl failed with -EFAULT, in which
 * case the driver never performed its copyout (nv.c:2689-2700).  The RM side
 * effect may already have happened before a copyout failure, so a failed
 * copy here surfaces as -EFAULT just like the native driver path. */
static long gpu_rm_copyback(unsigned long arg, const void *payload,
			    size_t size, long result)
{
	if (result != -EFAULT &&
	    copy_to_user((void __user *)arg, payload, size))
		return -EFAULT;
	return result;
}

/* Issue an ioctl against the fixed stub payload.  payload holds a kernel-side
 * copy of one NVOS block; the worker only sees the bytes in stub pages. */
static long gpu_rm_request(struct gpu_file *state, unsigned int command,
			   void *payload, size_t size)
{
	long result;

	mutex_lock(&state->mm->context.gpu_lock);
	result = gpu_command(state->mm, 4, state->host_fd, command, 0,
			     payload, size);
	mutex_unlock(&state->mm->context.gpu_lock);
	return result;
}

/* NVIDIA 550.120 class/clcb33.h: optional four-byte NvHandle hClient.
 * The non-FINN allocator derives its copy length from resource_list.h,
 * not paramsSize. RS_ACCESS_MASK is one u32 (rs_access.h). Neither child
 * carries another pointer or host FD. serverAllocApiCopyIn uses the
 * canonical class size even with FINN flags, before deserialization. The
 * fixed driver rejects this unsupported serialized class itself; preserve
 * its copy faults, error ordering and status instead of filtering flags. */
static void gpu_alloc_pointer(u64 *pointer, size_t size, bool writeable)
{
	void __user *nested = (void __user *)(unsigned long)*pointer;

	if (!*pointer)
		return;
	if (!access_ok(nested, size)) {
		*pointer = ~0ULL;
		return;
	}
	fault_in_readable(nested, size);
	if (writeable)
		fault_in_safe_writeable(nested, size);
}

static long gpu_rm_alloc(struct gpu_file *state, unsigned int command,
			 unsigned long arg, size_t size)
{
	/* The two layouts share their first 24 bytes only. */
	union {
		struct gpu_nvos21 v21;
		struct gpu_nvos64 v64;
	} params = {};
	u64 alloc_pointer, rights_pointer = 0;
	bool access_api = size == sizeof(struct gpu_nvos64);
	long result;

	if (copy_from_user(&params, (void __user *)arg, size))
		return -EFAULT;
	if (params.v64.hClass == 0xcb33) {
		alloc_pointer = params.v64.pAllocParms;
		gpu_alloc_pointer(&params.v64.pAllocParms, sizeof(u32), true);
		if (access_api) {
			rights_pointer = params.v64.pRightsRequested;
			gpu_alloc_pointer(&params.v64.pRightsRequested,
					  sizeof(u32), false);
		}
		result = um_tlb_sync(state->mm);
		if (result)
			return result;
		result = gpu_rm_request(state, command, &params, size);
		params.v64.pAllocParms = alloc_pointer;
		if (access_api)
			params.v64.pRightsRequested = rights_pointer;
		return gpu_rm_copyback(arg, &params, size, result);
	}
	if (params.v64.hClass != 0x00 && params.v64.hClass != 0x01 &&
	    params.v64.hClass != 0x41)
		return -EOPNOTSUPP;
	if (params.v64.pAllocParms ||
	    (access_api ? params.v64.paramsSize : params.v21.paramsSize))
		return -EOPNOTSUPP;
	/* Root-client parameter/FINN variants remain unimplemented. */
	if (access_api &&
	    (params.v64.pRightsRequested || params.v64.flags))
		return -EOPNOTSUPP;
	result = gpu_rm_request(state, command, &params, size);
	return gpu_rm_copyback(arg, &params, size, result);
}

/* NV_ESC_RM_FREE (0x29): NVOS00 contains numeric RM handles only; the host
 * driver validates them and reports status in the block. */
static long gpu_rm_free(struct gpu_file *state, unsigned int command,
			unsigned long arg)
{
	struct gpu_nvos00 params;
	long result;

	if (copy_from_user(&params, (void __user *)arg, sizeof(params)))
		return -EFAULT;
	result = gpu_rm_request(state, command, &params, sizeof(params));
	return gpu_rm_copyback(arg, &params, sizeof(params), result);
}

/* NVOS54_PARAMETERS, nvos.h:2208-2216 (NVIDIA 550.120).
 * Only these fixed root commands have bounded, pointer-free child structures:
 * ctrl0000gpu.h:66 (128 bytes), :162 (32), :325 (12), :628 (268), and the
 * 12-byte input-only RS_SHARE_POLICY used by client control 0xd04.
 * The legacy ID_INFO 0x202 has another pointer and is deliberately absent. */
struct gpu_nvos54 {
	u32 client, object, command, flags;
	u64 params;
	u32 size, status;
};
static_assert(sizeof(struct gpu_nvos54) == 32);
static_assert(offsetof(struct gpu_nvos54, params) == 16);
static_assert(offsetof(struct gpu_nvos54, size) == 24);
static_assert(offsetof(struct gpu_nvos54, status) == 28);

/* RS_SHARE_POLICY, rs_access.h:267-272 (NVIDIA 550.120): the entire body of
 * NV0000_CTRL_CLIENT_SET_INHERITED_SHARE_POLICY_PARAMS (ctrl0000client.h,
 * command 0xd04).  Pure input, twelve bytes, no pointers or FDs.
 *
 * Identity analysis for the share target under this transport (all guest RM
 * traffic is issued by the one fixed per-mm GPU worker TGID/mm/host fd):
 *  - RS_SHARE_TYPE_CLIENT (3): target is an RM client handle. Guest handles
 *    are issued verbatim by the host driver on this same worker fd, so a
 *    target name resolves to the identical host client; unknown targets get
 *    the real host RM status from serverGetClientUnderLock().
 *  - RS_SHARE_TYPE_PID (4): on Linux the stored u32 is never compared:
 *    cliresShareCallback matches RmClient.ProcID, and the fixed worker makes
 *    that value identical for every guest task sharing the mm. Cross-mm
 *    guest processes use separate workers, so their sharing stays denied at
 *    the open/REGISTER_FD boundary (-EXDEV), independent of this command.
 *  - RS_SHARE_TYPE_OS_SECURITY_TOKEN (2): matching uses the credentials
 *    captured when the host client was allocated, never a field here.
 * The driver itself rejects type >= RS_SHARE_TYPE_MAX (8) with an RM status
 * (client_resource.c:4708), so the bytes are forwarded unmodified instead of
 * being filtered or synthesised. */
struct gpu_rs_share_policy {
	u32 target;
	u32 access_mask; /* RS_ACCESS_MASK: one RsAccessLimb (NvU32). */
	u16 type;        /* RS_SHARE_TYPE_* */
	u8 action;       /* RS_SHARE_ACTION_FLAG_* */
	u8 padding;
};
static_assert(sizeof(struct gpu_rs_share_policy) == 12);
static_assert(offsetof(struct gpu_rs_share_policy, target) == 0);
static_assert(offsetof(struct gpu_rs_share_policy, access_mask) == 4);
static_assert(offsetof(struct gpu_rs_share_policy, type) == 8);
static_assert(offsetof(struct gpu_rs_share_policy, action) == 10);

/* ctrl0000system.h:117-124; deprecated/rmapi_deprecated_control.c:453-570.
 * The legacy converter copies a fixed 40-byte child irrespective of paramsSize,
 * then writes three strings of at most 256 bytes. Snapshot both pointer levels:
 * never let a mutable guest child supply worker-private host addresses. */
struct gpu_build_version {
	u32 size, padding;
	u64 strings[3];
	u32 changelist, official_changelist;
};
static_assert(sizeof(struct gpu_build_version) == 40);
static_assert(offsetof(struct gpu_build_version, strings) == 8);
static_assert(offsetof(struct gpu_build_version, changelist) == 32);

static long gpu_rm_build_version(struct gpu_file *state, unsigned int command,
				unsigned long arg, struct gpu_nvos54 *header)
{
	struct {
		struct gpu_nvos54 header;
		struct gpu_build_version child;
	} payload = { .header = *header };
	u64 original_strings[3];
	void __user *child = (void __user *)(unsigned long)header->params;
	bool copied;
	long result;
	int i;

	static_assert(sizeof(payload) <= STUB_GPU_PAYLOAD_SIZE);
	static_assert(offsetof(typeof(payload), child) == 32);
	copied = !copy_from_user(&payload.child, child, sizeof(payload.child));
	if (copied) {
		memcpy(original_strings, payload.child.strings, sizeof(original_strings));
		for (i = 0; i < ARRAY_SIZE(payload.child.strings); i++) {
			void __user *leaf = (void __user *)(unsigned long)original_strings[i];
			size_t length = min_t(u32, payload.child.size, 256);

			/* Preserve NULL: any NULL leaf selects the driver's size query.
			 * Protection faults remain real host-driver copyout failures.
			 * Never forward a range that could reach the private stub. */
			if (!leaf)
				continue;
			if (!access_ok(leaf, max_t(size_t, length, 1))) {
				payload.child.strings[i] = ~0ULL;
				continue;
			}
			fault_in_safe_writeable(leaf, length);
		}
		payload.header.params = STUB_GPU_DATA + offsetof(struct stub_data, gpu_payload)
					+ offsetof(typeof(payload), child);
	} else {
		/* The actual driver decides invalid-client versus invalid-pointer
		 * ordering. It must never retry a mutable, unchecked child. */
		payload.header.params = child ? ~0ULL : 0;
	}
	result = um_tlb_sync(state->mm);
	if (result)
		return result;
	result = gpu_rm_request(state, command, &payload, sizeof(payload));
	if (copied && result != -EFAULT) {
		unsigned long uncopied;

		memcpy(payload.child.strings, original_strings, sizeof(original_strings));
		/* ctrlparamRelease intentionally ignores child copyout failures.
		 * Preserve partial writes and success on a read-only child too. */
		uncopied = copy_to_user(child, &payload.child, sizeof(payload.child));
		(void)uncopied;
	}
	payload.header.params = header->params;
	return gpu_rm_copyback(arg, &payload.header, sizeof(payload.header), result);
}

static long gpu_rm_control(struct gpu_file *state, unsigned int command,
			   unsigned long arg)
{
	struct gpu_nvos54 params;
	u64 original_pointer;
	void __user *nested;
	size_t expected;
	long result;

	if (copy_from_user(&params, (void __user *)arg, sizeof(params)))
		return -EFAULT;
	if (params.command == 0x101 && !params.flags)
		return gpu_rm_build_version(state, command, arg, &params);
	switch (params.command) {
	case 0x1f0: expected = 4; break; /* SYSTEM_GET_FEATURES: NvU32 */
	case 0x201: expected = 128; break; /* GET_ATTACHED_IDS */
	case 0x205: expected = 32; break;  /* GET_ID_INFO_V2 */
	case 0x21b: expected = 12; break;  /* GET_PCI_INFO */
	case 0x275: expected = 268; break; /* GET_UUID_FROM_GPU_ID */
	/* SET_INHERITED_SHARE_POLICY: fixed 12-byte input child, validated by
	 * the host driver; see struct gpu_rs_share_policy for target identity. */
	case 0xd04: expected = sizeof(struct gpu_rs_share_policy); break;
	default: return -EOPNOTSUPP;
	}
	/* 550.120 _rmapiRmControl validates these fixed commands' flagged modes
	 * and declared size before copying their children. Fault in only the
	 * canonical guest range; let RM preserve its own validation
	 * order and status, including accepted nonzero flags. These methods'
	 * deserialized structures have no embedded addresses or descriptors.
	 * Other commands retain their existing restrictions. */
	if (params.command != 0xd04 && params.command != 0x1f0 &&
	    (params.flags || params.size != expected))
		return -EOPNOTSUPP;
	original_pointer = params.params;
	nested = (void __user *)(unsigned long)original_pointer;

	/* Bring demand-faulted guest pages into the shared host mm. Do not turn
	 * protection faults into an ioctl errno: RM_CONTROL reports nested
	 * copy faults in its own status field. The real host driver must see
	 * the same absent/read-only mapping and produce that result itself.
	 * These bounded child structures contain no additional pointers/FDs;
	 * the 0xd04 child is input-only, so its read-only/cross-page behaviour
	 * is likewise decided by the host driver without local emulation.
	 * Header fields are copied once, so concurrent user edits cannot turn
	 * an allowed command into a different host operation or change size. */
	if (access_ok(nested, expected)) {
		fault_in_readable(nested, expected);
		fault_in_safe_writeable(nested, expected);
	} else {
		/* Never let a guest pointer outside TASK_SIZE name the UML stub
		 * or other worker-private mappings. Use a non-null host-invalid
		 * address so the real driver still decides error ordering/status.
		 * NULL is not equivalent: NVIDIA reports INVALID_ARGUMENT for
		 * NULL, but INVALID_ADDRESS for an inaccessible non-null buffer. */
		params.params = ~0ULL;
	}
	result = um_tlb_sync(state->mm);
	if (result)
		return result;
	result = gpu_rm_request(state, command, &params, sizeof(params));
	params.params = original_pointer;
	return gpu_rm_copyback(arg, &params, sizeof(params), result);
}

static long gpu_ioctl(struct file *file, unsigned int command, unsigned long arg)
{
	struct gpu_file *state = file->private_data;
	struct mm_struct *mm = state->mm;
	size_t size = _IOC_SIZE(command);
	long result;

	/* FD transfer/fork semantics are not implemented yet: fail explicitly. */
	if (current->mm != mm || task_tgid(current) != mm->context.gpu_owner)
		return -EXDEV;
	if (command == _IOWR('F', 201, int))
		return gpu_register_fd(state, command, arg);
	if (command == _IOWR('F', 0x2a, struct gpu_nvos54))
		return gpu_rm_control(state, command, arg);
	/* Whitelisted RM root-client transport; exact _IOWR encodings with
	 * the fixed NVOS block sizes.  Anything else falls through to the
	 * query filter and stays unavailable. */
	if (command == _IOWR('F', 0x2b, struct gpu_nvos21) ||
	    command == _IOWR('F', 0x2b, struct gpu_nvos64))
		return gpu_rm_alloc(state, command, arg, size);
	if (command == _IOWR('F', 0x29, struct gpu_nvos00))
		return gpu_rm_free(state, command, arg);
	/* Match the driver's size error without issuing malformed host calls. */
	if (_IOC_TYPE(command) == 'F' &&
	    _IOC_DIR(command) == (_IOC_READ | _IOC_WRITE) &&
	    (_IOC_NR(command) == 0x29 || _IOC_NR(command) == 0x2b))
		return -EINVAL;
	/* SYS_PARAMS (214) mutates global host state; it is not a query.
	 * Other embedded pointers/FDs and mmap remain unavailable. */
	if (_IOC_TYPE(command) != 'F' ||
	    (_IOC_NR(command) != 200 && _IOC_NR(command) != 210) || !size || size > PAGE_SIZE)
		return -EOPNOTSUPP;
	if (fault_in_readable((void __user *)arg, size) ||
	    fault_in_safe_writeable((void __user *)arg, size))
		return -EFAULT;
	result = um_tlb_sync(mm);
	if (result)
		return result;
	mutex_lock(&mm->context.gpu_lock);
	result = gpu_command(mm, 1, state->host_fd, command, arg, NULL, 0);
	mutex_unlock(&mm->context.gpu_lock);
	return result;
}

static int gpu_release(struct inode *inode, struct file *file)
{
	struct gpu_file *state = file->private_data;
	struct mm_struct *mm = state->mm;
	long result;

	mutex_lock(&mm->context.gpu_lock);
	result = gpu_command(mm, 2, state->host_fd, 0, 0, NULL, 0);
	mutex_unlock(&mm->context.gpu_lock);
	mmdrop(mm);
	kfree(state);
	return result < 0 ? result : 0;
}

static const struct file_operations gpu_fops = {
	.owner = THIS_MODULE,
	.open = gpu_open,
	.unlocked_ioctl = gpu_ioctl,
	.release = gpu_release,
};


static char *gpu_devnode(const struct device *device, umode_t *mode)
{
	if (mode)
		*mode = 0600;
	return NULL;
}

static int __init gpu_init(void)
{
	int index, error;
	dev_t first = MKDEV(GPU_MAJOR, 0);

	if (!uml_gpu_bridge) {
		if (uml_gpu_async_wait)
			pr_warn("harddind-gpu: gpu_async_wait=1 ignored without gpu_bridge=1\n");
		return 0;
	}
	if (!parallel_userspace)
		return -EOPNOTSUPP;
	error = register_chrdev_region(first, ARRAY_SIZE(gpu_devices),
				       "nvidia-frontend");
	if (error)
		return error;
	gpu_class = class_create("harddind-nvidia");
	if (IS_ERR(gpu_class)) {
		error = PTR_ERR(gpu_class);
		goto unregister;
	}
	gpu_class->devnode = gpu_devnode;
	for (index = 0; index < ARRAY_SIZE(gpu_devices); index++) {
		struct gpu_device *device = &gpu_devices[index];
		dev_t number = MKDEV(GPU_MAJOR, index);

		if (!os_gpu_device_present(index))
			continue;
		device->index = index;
		cdev_init(&device->cdev, &gpu_fops);
		device->cdev.owner = THIS_MODULE;
		error = cdev_add(&device->cdev, number, 1);
		if (error)
			goto unwind;
		if (index == 255)
			device->device = device_create(gpu_class, NULL, number,
						      NULL, "harddind-nvidiactl");
		else
			device->device = device_create(gpu_class, NULL, number,
						      NULL, "harddind-nvidia%d", index);
		if (IS_ERR(device->device)) {
			error = PTR_ERR(device->device);
			device->device = NULL;
			cdev_del(&device->cdev);
			goto unwind;
		}
	}
	return 0;
unwind:
	while (index--) {
		struct gpu_device *device = &gpu_devices[index];

		if (device->device) {
			device_destroy(gpu_class, MKDEV(GPU_MAJOR, index));
			cdev_del(&device->cdev);
			device->device = NULL;
		}
	}
	class_destroy(gpu_class);
unregister:
	unregister_chrdev_region(first, ARRAY_SIZE(gpu_devices));
	return error;
}
device_initcall(gpu_init);

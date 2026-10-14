// SPDX-License-Identifier: GPL-2.0
/* Experimental NVIDIA CUDA bridge (0024), built on the 0012/0013/0016/
 * 0017/0019/0020/0021/0022/0023 control transport.
 *
 * One fixed-TGID GPU worker host process per guest mm forwards:
 *   - RM ioctls on /dev/nvidiactl and /dev/nvidiaN, translating embedded
 *     guest FDs into worker descriptors, snapshotting NVOS54 headers and
 *     screening every nested guest pointer against guest TASK_SIZE so a
 *     guest address can never name worker-private stub mappings;
 *   - UVM ioctls on /dev/nvidia-uvm with a fixed, header-compiled command
 *     table (exact request numbers, sizes and embedded FD offsets);
 *   - device mmap/munmap/mprotect at the guest virtual address (UML maps
 *     every guest userspace page at the identical host address in the
 *     shared mm, which is what the UVM offset==VA rule and the RM address
 *     keyed mapping tables require);
 *   - OS event completion: a CLONE_THREAD relay inside the worker polls
 *     worker eventfds/device FDs and an IRQ-driven guest kthread signals
 *     guest eventfds and wait queues with lossless generation protocols.
 *
 * Large ioctl blocks bounce through STUB_GPU_SHADOW (16 KiB): the kernel
 * fills its own direct map of those pages and names only the worker VA
 * STUB_GPU_SHADOW in syscall arguments; the worker never receives a kernel
 * pointer.  The whole fill -> translate -> ioctl -> copyback section runs
 * under the per-mm gpu_lock, so the shared bounce block is an immutable
 * per-call snapshot.
 *
 * Still incomplete (fail closed, see REPORT.md): a handful of controls not
 * present in the pinned 550.120 open headers, guest kernel access into
 * host-only GPU mappings (SIGBUS), fabric nested-descriptor controls,
 * fork inheritance of device VMAs.
 */
#include <linux/fs.h>
#include <linux/file.h>
#include <linux/init.h>
#include <linux/ioctl.h>
#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/kthread.h>
#include <linux/mm.h>
#include <linux/mman.h>
#include <linux/module.h>
#include <linux/pagemap.h>
#include <linux/poll.h>
#include <linux/sched/signal.h>
#include <linux/slab.h>
#include <linux/uaccess.h>
#include <linux/eventfd.h>
#include <linux/completion.h>
#include <linux/interrupt.h>
#include <linux/kstrtox.h>
#include <linux/ktime.h>
#include <linux/math64.h>
#include <linux/refcount.h>
#include <asm/tlbflush.h>
#include <as-layout.h>
#include <init.h>
#include <irq_kern.h>
#include <os.h>
#include <skas.h>
#include <stub-data.h>

/*
 * Fixed 550.120 RM parameter blocks (see REPORT.md and tests/models for the
 * header extraction evidence).
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
static_assert(offsetof(struct gpu_nvos21, pAllocParms) == 16);
static_assert(offsetof(struct gpu_nvos21, paramsSize) == 24);
static_assert(offsetof(struct gpu_nvos21, status) == 28);
static_assert(offsetof(struct gpu_nvos64, pAllocParms) == 16);
static_assert(offsetof(struct gpu_nvos64, pRightsRequested) == 24);
static_assert(offsetof(struct gpu_nvos64, paramsSize) == 32);
static_assert(offsetof(struct gpu_nvos64, flags) == 36);
static_assert(offsetof(struct gpu_nvos64, status) == 40);

/* NVOS02/NVOS33 trailing-descriptor wrappers,
 * nv-unix-nvos-params-wrappers.h:30-45 (550.120). */
struct gpu_nvos02 {
	u32 hRoot, hObjectParent, hObjectNew, hClass;
	u32 flags;
	u64 pMemory;
	u64 limit;
	u32 status, pad0, fd, pad1;
};
struct gpu_nvos33 {
	u32 hClient, hDevice, hMemory, pad0;
	u64 offset, length, pLinearAddress;
	u32 status, flags, fd, pad1;
};
/* NVOS34 (RM_UNMAP_MEMORY) and NVOS56 (UPDATE_DEVICE_MAPPING_INFO), no
 * descriptor wrapper. */
struct gpu_nvos34 {
	u32 hClient, hDevice, hMemory;
	u64 pLinearAddress;
	u32 status, flags;
};
struct gpu_nvos56 {
	u32 hClient, hDevice, hMemory;
	u64 pOldCpuAddress, pNewCpuAddress;
	u32 status;
};
static_assert(sizeof(struct gpu_nvos02) == 56);
static_assert(offsetof(struct gpu_nvos02, pMemory) == 24);
static_assert(offsetof(struct gpu_nvos02, limit) == 32);
static_assert(offsetof(struct gpu_nvos02, fd) == 48);
static_assert(sizeof(struct gpu_nvos33) == 56);
static_assert(offsetof(struct gpu_nvos33, offset) == 16);
static_assert(offsetof(struct gpu_nvos33, length) == 24);
static_assert(offsetof(struct gpu_nvos33, pLinearAddress) == 32);
static_assert(offsetof(struct gpu_nvos33, fd) == 48);
static_assert(sizeof(struct gpu_nvos34) == 32);
static_assert(offsetof(struct gpu_nvos34, pLinearAddress) == 16);
static_assert(sizeof(struct gpu_nvos56) == 40);
static_assert(offsetof(struct gpu_nvos56, pOldCpuAddress) == 16);
static_assert(offsetof(struct gpu_nvos56, pNewCpuAddress) == 24);

/* nv_ioctl_alloc_os_event_t, nv-ioctl.h:69-75 (550.120). */
struct gpu_os_event {
	u32 hClient, hDevice, fd, status;
};
static_assert(sizeof(struct gpu_os_event) == 16);
static_assert(offsetof(struct gpu_os_event, fd) == 8);
static_assert(offsetof(struct gpu_os_event, status) == 12);

/* NVOS54_PARAMETERS, nvos.h:2208-2216 (550.120). */
struct gpu_nvos54 {
	u32 client, object, command, flags;
	u64 params;
	u32 size, status;
};
static_assert(sizeof(struct gpu_nvos54) == 32);
static_assert(offsetof(struct gpu_nvos54, params) == 16);
static_assert(offsetof(struct gpu_nvos54, size) == 24);
static_assert(offsetof(struct gpu_nvos54, status) == 28);

/* RS_SHARE_POLICY for client control 0xd04 (0023). */
struct gpu_rs_share_policy {
	u32 target;
	u32 access_mask;
	u16 type;
	u8 action;
	u8 padding;
};
static_assert(sizeof(struct gpu_rs_share_policy) == 12);

/* ctrl0000system.h:117-124; 0022 two-level build-version snapshot. */
struct gpu_build_version {
	u32 size, padding;
	u64 strings[3];
	u32 changelist, official_changelist;
};
static_assert(sizeof(struct gpu_build_version) == 40);
static_assert(offsetof(struct gpu_build_version, strings) == 8);
static_assert(offsetof(struct gpu_build_version, changelist) == 32);

/* NV0000_CTRL_GPU_GET_ID_INFO (0x202): szName is the only nested pointer
 * and names a fixed NV0000_CTRL_GPU_MAX_SZNAME (128) output buffer. */
struct gpu_id_info {
	u32 gpuId, gpuFlags, deviceInstance, subDeviceInstance;
	u64 szName;
	u32 sliStatus, boardId, gpuInstance;
	s32 numaId;
};
static_assert(sizeof(struct gpu_id_info) == 40);
#define GPU_ID_INFO_NAME_LEN 128

/* NV0080_CTRL_FIFO_GET_CHANNELLIST_PARAMS (0x80170d): two variable output
 * arrays, numChannels elements of u32 each. */
struct gpu_channellist {
	u32 numChannels;
	u64 pChannelHandleList, pChannelList;
};
static_assert(sizeof(struct gpu_channellist) == 24);
#define GPU_CHANNEL_LIST_CAP 4096

/* NV2080_CTRL_GR_GET_INFO_PARAMS (0x20801201): one variable byte array
 * plus the inline NV2080_CTRL_GR_ROUTE_INFO (16 bytes). */
struct gpu_gr_info {
	u32 grInfoListSize;
	u64 grInfoList;
	u64 grRouteInfo0, grRouteInfo1;
};
static_assert(sizeof(struct gpu_gr_info) == 32);

/* Audited fixed, pointer-free NVOS54 child structures.  Sizes match the
 * observed native workload and the pinned control headers; the matching
 * tests/models/gpu_ctl_abi_model.c prints driver struct sizes for these. */
struct gpu_ctl_flat {
	u32 command;
	u16 size;
};

#define GPU_CTL_FLAT(cmd, sz) { (cmd), (sz) }
static const struct gpu_ctl_flat gpu_ctl_flat_table[] = {
	/* Root/GPU queries retained from 0019. */
	GPU_CTL_FLAT(0x201, 128),	/* GPU_GET_ATTACHED_IDS */
	GPU_CTL_FLAT(0x205, 32),	/* GPU_GET_ID_INFO_V2 */
	GPU_CTL_FLAT(0x21b, 12),	/* GPU_GET_PCI_INFO */
	GPU_CTL_FLAT(0x275, 268),	/* GPU_GET_UUID_FROM_GPU_ID */
	/* 0023 client policy. */
	GPU_CTL_FLAT(0xd04, sizeof(struct gpu_rs_share_policy)),
	/* Root/system queries observed in the pinned native workload. */
	GPU_CTL_FLAT(0x136, 4),		/* SYSTEM_GET_FABRIC_STATUS */
	GPU_CTL_FLAT(0x13a, 1352),	/* SYSTEM_GET_P2P_CAPS_MATRIX */
	GPU_CTL_FLAT(0x1f0, 4),		/* SYSTEM_GET_FEATURES */
	GPU_CTL_FLAT(0x214, 256),	/* GPU_GET_PROBED_IDS */
	GPU_CTL_FLAT(0x215, 132),	/* GPU_ATTACH_IDS */
	GPU_CTL_FLAT(0x27b, 4),		/* GPU_GET_MEMOP_ENABLE */
	GPU_CTL_FLAT(0xa04, 2244),	/* SYNC_GPU_BOOST_GROUP_INFO */
	GPU_CTL_FLAT(0xd01, 12),	/* CLIENT_GET_ADDR_SPACE_TYPE */
	/* NV01_DEVICE / NV01_SUBDEVICE controls. */
	GPU_CTL_FLAT(0x800280, 4),	/* GPU_GET_NUM_SUBDEVICES */
	GPU_CTL_FLAT(0x800289, 4),	/* GPU_GET_VIRTUALIZATION_MODE */
	GPU_CTL_FLAT(0x800292, 700),	/* GPU_GET_CLASSLIST_V2 */
	GPU_CTL_FLAT(0x801307, 3),	/* FB_GET_CAPS_V2 */
	GPU_CTL_FLAT(0x801402, 3),	/* HOST_GET_CAPS_V2 */
	GPU_CTL_FLAT(0x801909, 1),	/* PERF_CUDA_LIMIT_SET_CONTROL */
	GPU_CTL_FLAT(0x80170d, 24),	/* handled as a variable-pointer child */
	/* NV20 subdevice controls. */
	GPU_CTL_FLAT(0x20800102, 524),	/* GPU_GET_INFO_V2 */
	GPU_CTL_FLAT(0x20800110, 132),	/* GPU_GET_NAME_STRING */
	GPU_CTL_FLAT(0x20800111, 64),	/* GPU_GET_SHORT_NAME_STRING */
	GPU_CTL_FLAT(0x20800119, 4),	/* GPU_GET_SIMULATION_INFO */
	GPU_CTL_FLAT(0x2080012f, 1024),	/* GPU_QUERY_ECC_STATUS */
	GPU_CTL_FLAT(0x20800131, 4),	/* GPU_QUERY_COMPUTE_MODE_RULES */
	GPU_CTL_FLAT(0x2080014a, 268),	/* GPU_GET_GID_INFO */
	GPU_CTL_FLAT(0x20800170, 260),	/* GPU_GET_ENGINES_V2 */
	GPU_CTL_FLAT(0x2080018b, 40),	/* GPU_GET_ACTIVE_PARTITION_IDS */
	GPU_CTL_FLAT(0x20800195, 260),	/* GPU_GET_COMPUTE_POLICY_CONFIG */
	GPU_CTL_FLAT(0x20801201, 32),	/* variable-pointer child */
	GPU_CTL_FLAT(0x20801210, 32),	/* GR_SET_CTXSW_PREEMPTION_MODE */
	GPU_CTL_FLAT(0x20801218, 16),	/* GR_GET_CTX_BUFFER_SIZE */
	GPU_CTL_FLAT(0x2080121b, 6168),	/* GR_GET_GLOBAL_SM_ORDER */
	GPU_CTL_FLAT(0x20801227, 48),	/* GR_GET_CAPS_V2 */
	GPU_CTL_FLAT(0x2080122a, 24),	/* GR_GET_GPC_MASK */
	GPU_CTL_FLAT(0x2080122b, 24),	/* GR_GET_TPC_MASK */
	GPU_CTL_FLAT(0x20801303, 444),	/* FB_GET_INFO_V2 */
	GPU_CTL_FLAT(0x20801701, 16),	/* MC_GET_ARCH_INFO */
	GPU_CTL_FLAT(0x20801801, 16),	/* BUS_GET_PCI_INFO */
	GPU_CTL_FLAT(0x20801803, 200),	/* BUS_GET_PCI_BAR_INFO */
	GPU_CTL_FLAT(0x20801823, 420),	/* BUS_GET_INFO_V2 */
	GPU_CTL_FLAT(0x2080182a, 104),	/* BUS_GET_PCIE_GPU_ATOMICS */
	GPU_CTL_FLAT(0x2080182b, 28),	/* BUS_GET_C2C_INFO */
	GPU_CTL_FLAT(0x2080200a, 8),	/* PERF_BOOST */
	GPU_CTL_FLAT(0x20802a0a, 136),	/* CE_GET_ALL_CAPS */
	GPU_CTL_FLAT(0x20803002, 5896),	/* NVLINK_GET_NVLINK_STATUS */
	GPU_CTL_FLAT(0x20803601, 72),	/* GSP_GET_FEATURES */
	/* Third-party P2P, debug, virtualization, channels, conf compute. */
	GPU_CTL_FLAT(0x503c0102, 16),	/* 503C REGISTER_VA_SPACE */
	GPU_CTL_FLAT(0x503c0104, 32),	/* 503C REGISTER_VIDMEM */
	GPU_CTL_FLAT(0x503c0105, 4),	/* 503C UNREGISTER_VIDMEM */
	GPU_CTL_FLAT(0x83de0309, 4),	/* DEBUG_SET_EXCEPTION_MASK */
	GPU_CTL_FLAT(0x90e60102, 8),	/* MASTER_GET_VF_ERROR_INTR_MASK */
	GPU_CTL_FLAT(0xa06c0101, 2),	/* GPFIFO_SCHEDULE */
	GPU_CTL_FLAT(0xa06c0103, 8),	/* SET_TIMESLICE */
	GPU_CTL_FLAT(0xa06c0105, 8),	/* PREEMPT */
	GPU_CTL_FLAT(0xc36f0101, 16),	/* GET_CLASS_ENGINEID */
	GPU_CTL_FLAT(0xc36f0108, 4),	/* GPFIFO_GET_WORK_SUBMIT_TOKEN */
	GPU_CTL_FLAT(0xcb330101, 6),	/* CONF_COMPUTE GET_CAPABILITIES */
	/* Size-zero commands: the host ignores params. */
	GPU_CTL_FLAT(0x20800145, 0),
	GPU_CTL_FLAT(0x20800146, 0),
	GPU_CTL_FLAT(0x2080220c, 0),	/* RC_RELEASE_WATCHDOG_REQUESTS */
	GPU_CTL_FLAT(0x20802210, 0),	/* RC_SOFT_DISABLE_WATCHDOG */
};

/* Observed controls whose layout is not published in the pinned open
 * headers (private/BINAPI): fail closed instead of guessing. */
static bool gpu_ctl_unverified(u32 command)
{
	switch (command) {
	case 0x80028b:		/* unknown NV01 device subcommand */
	case 0x20808159:	/* unpublished subdevice command */
	case 0x20808162:
	case 0x20810108:	/* NV2081 BINAPI payload */
		return true;
	default:
		return false;
	}
}

/* Typed RM_ALLOC class table.  Canonical parameter-block sizes are
 * compiler-verified against the pinned 550.120 headers
 * (tests/models/class_alloc_abi_model.c) and agree with the driver's own
 * resource_list.h RS_REQUIRED metadata; the non-FINN allocator copies that
 * canonical size from pAllocParms even when paramsSize is 0.  A canonical
 * size of 0 means the resource has NO allocation parameters (driver copies
 * nothing; pAllocParms must be ignored).  inner_handle marks classes whose
 * block contains an OS-event HANDLE value (0x79 data@16): it is not a
 * guest VA and is never rewritten.  Classes absent from this table are not
 * blindly forwarded; see REPORT.md for the resulting gap list. */
struct gpu_alloc_class {
	u32 hClass;
	u16 canonical_size;
	u16 inner_handle_off;	/* 0xffff if absent */
};
static const struct gpu_alloc_class gpu_alloc_class_table[] = {
	{ 0x003e, 128, 0xffff },	/* NV01_MEMORY_SYSTEM */
	{ 0x0040, 128, 0xffff },	/* NV01_MEMORY_LOCAL_USER */
	{ 0x0079, 24, 16 },	/* NV01_EVENT_OS_EVENT, data=handle */
	{ 0x0080, 56, 0xffff },	/* NV0080 device */
	{ 0x2080, 4, 0xffff },	/* NV20_SUBDEVICE */
	{ 0x2081, 4, 0xffff },	/* BINAPI */
	{ 0x503c, 4, 0xffff },	/* third-party P2P */
	{ 0x50a0, 128, 0xffff },/* memory class, same params */
	{ 0x83de, 12, 0xffff },/* DEBUG */
	{ 0x9067, 12, 0xffff },/* CTXSHARE */
	{ 0x90e6, 0, 0xffff },	/* no alloc params; NULL block */
	{ 0x90f1, 48, 0xffff },/* VASPACE */
	{ 0xa06c, 20, 0xffff },/* channel group */
	{ 0xc461, 0, 0xffff },	/* no alloc params; NULL block */
	{ 0xc56f, 360, 0xffff },/* channel */
	{ 0xc6b5, 8, 0xffff },	/* B0B5 */
	{ 0xc6c0, 16, 0xffff },/* GR */
	{ 0xcb33, 4, 0xffff },	/* confidential compute */
};

/* Fixed UVM command table.  Every size and embedded FD offset comes from
 * tests/models/uvm_abi_model.c compiled against 550.120 uvm_ioctl.h:
 * UVM_INITIALIZE is the distinct request 0x30000001 (16 bytes), plain nr 1
 * is UVM_RESERVE_VA (24 bytes), nr 34 is FREE (24) and nr 66 is
 * UNMAP_EXTERNAL (40).  Test/debug/tools ioctls stay unsupported. */
struct gpu_uvm_cmd {
	u32 request;		/* full ioctl request number */
	u16 size;
	s16 fd_off;		/* embedded descriptor offset, -1 if none */
	u8 fd_is_uvm;		/* descriptor must be the UVM node */
};
#define UVM_NF (-1)
static const struct gpu_uvm_cmd gpu_uvm_table[] = {
	{ 0x30000001U, 16, UVM_NF, 0 },	/* UVM_INITIALIZE */
	{ 0x30000002U, 0, UVM_NF, 0 },	/* UVM_DEINITIALIZE: no params */
	{ 1, 24, UVM_NF, 0 },	/* RESERVE_VA */
	{ 23, 16, UVM_NF, 0 },	/* CREATE_RANGE_GROUP */
	{ 24, 16, UVM_NF, 0 },	/* DESTROY_RANGE_GROUP */
	{ 25, 32, 16, 0 },	/* REGISTER_GPU_VASPACE */
	{ 26, 20, UVM_NF, 0 },	/* UNREGISTER_GPU_VASPACE */
	{ 27, 56, 16, 0 },	/* REGISTER_CHANNEL */
	{ 28, 28, UVM_NF, 0 },	/* UNREGISTER_CHANNEL */
	{ 29, 36, UVM_NF, 0 },	/* ENABLE_PEER_ACCESS */
	{ 30, 36, UVM_NF, 0 },	/* DISABLE_PEER_ACCESS */
	{ 31, 32, UVM_NF, 0 },	/* SET_RANGE_GROUP */
	{ 33, 9264, 9248, 0 },	/* MAP_EXTERNAL_ALLOCATION */
	{ 34, 24, UVM_NF, 0 },	/* FREE */
	{ 35, 24, UVM_NF, 0 },	/* MEM_MAP */
	{ 37, 40, 24, 0 },	/* REGISTER_GPU */
	{ 38, 20, UVM_NF, 0 },	/* UNREGISTER_GPU */
	{ 39, 8, UVM_NF, 0 },	/* PAGEABLE_MEM_ACCESS */
	{ 40, 272, UVM_NF, 0 },/* PREVENT_MIGRATION_RANGE_GROUPS */
	{ 41, 272, UVM_NF, 0 },/* ALLOW_MIGRATION_RANGE_GROUPS */
	{ 42, 40, UVM_NF, 0 },	/* SET_PREFERRED_LOCATION */
	{ 43, 24, UVM_NF, 0 },	/* UNSET_PREFERRED_LOCATION */
	{ 44, 24, UVM_NF, 0 },	/* ENABLE_READ_DUPLICATION (u64,u64,status) */
	{ 45, 24, UVM_NF, 0 },	/* DISABLE_READ_DUPLICATION */
	{ 46, 40, UVM_NF, 0 },	/* SET_ACCESSED_BY (+16B NvProcessorUuid) */
	{ 47, 40, UVM_NF, 0 },	/* UNSET_ACCESSED_BY */
	{ 51, 80, UVM_NF, 0 },	/* MIGRATE */
	{ 53, 32, UVM_NF, 0 },	/* MIGRATE_RANGE_GROUP */
	{ 54, 20, UVM_NF, 0 },	/* ENABLE_SYSTEM_WIDE_ATOMICS */
	{ 55, 20, UVM_NF, 0 },	/* DISABLE_SYSTEM_WIDE_ATOMICS */
	{ 65, 40, UVM_NF, 0 },	/* MAP_DYNAMIC_PARALLELISM_REGION */
	{ 66, 40, UVM_NF, 0 },	/* UNMAP_EXTERNAL */
	{ 68, 9248, UVM_NF, 0 },/* ALLOC_SEMAPHORE_POOL */
	{ 69, 4, UVM_NF, 0 },	/* CLEAN_UP_ZOMBIE_RESOURCES */
	{ 70, 24, UVM_NF, 0 },	/* PAGEABLE_MEM_ACCESS_ON_GPU */
	{ 71, 24, UVM_NF, 0 },	/* POPULATE_PAGEABLE */
	{ 72, 24, UVM_NF, 0 },	/* VALIDATE_VA_RANGE */
	{ 73, 24, UVM_NF, 0 },	/* CREATE_EXTERNAL_RANGE */
	{ 74, 40, UVM_NF, 0 },	/* MAP_EXTERNAL_SPARSE */
	{ 75, 8, 0, 1 },	/* MM_INITIALIZE: uvmFd */
};

int uml_gpu_bridge;
int uml_gpu_async_wait;
static unsigned int gpu_async_debug;
static atomic_t gpu_async_waits;
static atomic64_t gpu_async_total_ns;
static atomic_t gpu_async_max_ns;

/* Index 255 is nvidiactl, 0..254 physical GPUs, 256 nvidia-uvm. */
#define GPU_KIND_CTL 255
#define GPU_KIND_UVM 256
#define GPU_INDEX_MAX 256
#define GPU_MAJOR 195

/* One cdev for each name of a GPU minor.  Canonical nodes live at major
 * 195; the historical harddind-nvidia* names live in a separately
 * allocated dynamic range.  device_create rejects a second name on the
 * SAME dev_t with EEXIST (duplicate /dev/char/195:0 sysfs link), so the
 * compatibility names must never share the canonical dev_t. */
struct gpu_cdev {
	struct cdev cdev;
	unsigned int index;
};

struct gpu_device {
	struct gpu_cdev canon;		/* /dev/nvidia* at major GPU_MAJOR */
	struct gpu_cdev compat;		/* /dev/harddind-nvidia* dynamic */
	struct device *device;
	struct device *alias;
	unsigned int index;
	bool canon_added;
	bool compat_added;
};

static struct class *gpu_class;
static struct gpu_device gpu_devices[GPU_INDEX_MAX + 1];
static dev_t gpu_compat_first;		/* dynamic compatibility range */
static const struct file_operations gpu_fops;

struct gpu_file {
	struct mm_struct *mm;
	struct list_head node;
	unsigned int index;
	int host_fd;
	int slot;			/* relay slot, -1 until polled */
	unsigned int slot_gen;
	wait_queue_head_t waitq;
	atomic_t poll_mask;
	unsigned int last_level;
};

/* OS event bookkeeping.  The worker driver keys its event_list by
 * (hClient from ALLOC_OS_EVENT, worker fd); the bridge keeps the same
 * scope, plus the guest eventfd context as an unforgeable identity (the
 * integer guest fd can be closed and reused, so it is never the sole key).
 * Proven on 550.120 osapi.c:484-503 (allocate_os_event) and os.c:1440-1465
 * (osUserHandleToKernelPtr). */
struct gpu_event_map {
	struct list_head node;
	struct eventfd_ctx *guest_ctx;
	u32 h_client;			/* ALLOC_OS_EVENT hClient scope */
	int guest_fd;
	int host_fd;
	int slot;
	unsigned int gen;
	u64 delivered;
	unsigned int value_gen;
};

struct gpu_vma {
	struct mm_struct *mm;
	refcount_t refs;
};

static int __init gpu_setup(char *line, int *add)
{
	*add = 0;
	if (!strcmp(line, "1"))
		uml_gpu_bridge = 1;
	return 0;
}
__uml_setup("gpu_bridge=", gpu_setup,
"gpu_bridge=1\n"
"    Enable the experimental NVIDIA RM/UVM CUDA bridge (default off).\n");

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
"    enabled.  Requires gpu_bridge=1; defaults to off.\n");

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
"    0017 diagnostics (default 0): bit0 logs aggregated wait latency,\n"
"    bit1 injects a ~0.1 s worker stall, bit2 logs submissions.\n");

static struct gpu_relay_page *gpu_relay_page(struct mm_struct *mm)
{
	return (void *)mm->context.gpu_relay_pages;
}

/* The worker writes exactly one zero datagram per finished batch. */
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

/* The relay thread writes one datagram whenever the relay table changed.
 * Counters live in the page, so a merged or lost datagram loses nothing. */
static irqreturn_t gpu_relay_irq(int irq, void *dev)
{
	struct mm_struct *mm = dev;
	char byte;
	int n, received = 0;

	while ((n = os_read_file(mm->context.gpu_notify_fd, &byte, 1)) == 1)
		received++;
	if (n != -EAGAIN && n != -EWOULDBLOCK)
		pr_err_ratelimited("harddind-gpu: relay socket read error %d\n",
				   n);
	if (!received)
		return IRQ_NONE;
	/* Wake the event kthread: a waitqueue predicate on a pending bit plus
	 * the kthread stop flag makes kthread_stop reliable without blocking
	 * a vCPU. */
	WRITE_ONCE(mm->context.gpu_relay_pending, 1);
	wake_up_interruptible_all(&mm->context.gpu_relay_wq);
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

/* Publish/retire relay slots.  Callers hold gpu_lock. */
static int gpu_slot_alloc(struct mm_struct *mm)
{
	struct gpu_relay_page *rp = gpu_relay_page(mm);
	int i;

	if (!rp)
		return -ENOMEM;
	for (i = 0; i < UML_GPU_RELAY_MAX_ENTRIES; i++)
		if (rp->entry[i].state == GPU_RELAY_STATE_EMPTY)
			return i;
	return -EAGAIN;
}

/* True when every slot is ACTIVE (permanent exhaustion); if CLOSING slots
 * still exist the table will free up after the relay's ack (transient). */
static bool gpu_slots_all_active(struct mm_struct *mm)
{
	struct gpu_relay_page *rp = gpu_relay_page(mm);
	int i;

	if (!rp)
		return false;
	for (i = 0; i < UML_GPU_RELAY_MAX_ENTRIES; i++)
		if (rp->entry[i].state != GPU_RELAY_STATE_ACTIVE)
			return false;
	return true;
}

static void gpu_slot_publish(struct mm_struct *mm, int slot, int host_fd,
			     unsigned int kind, unsigned int *gen_out)
{
	struct gpu_relay_entry *e = &gpu_relay_page(mm)->entry[slot];

	e->fd = host_fd;
	e->kind = kind;
	e->ack = GPU_RELAY_ACK_NONE;
	e->ack_gen = 0;
	e->level = 0;
	e->level_gen = 0;
	e->value = 0;
	e->value_gen = 0;
	e->sticky = 0;
	/* gen must be visible before state: the relay snapshots gen while
	 * scanning, and the kick socket provides the wake ordering. */
	smp_wmb();
	e->gen++;
	if (gen_out)
		*gen_out = e->gen;
	e->state = GPU_RELAY_STATE_ACTIVE;
	os_gpu_relay_kick(mm->context.gpu_kick_fd);
}

static void gpu_slot_retire(struct mm_struct *mm, int slot)
{
	struct gpu_relay_entry *e;

	if (slot < 0)
		return;
	e = &gpu_relay_page(mm)->entry[slot];
	if (e->state == GPU_RELAY_STATE_EMPTY)
		return;
	e->ack = GPU_RELAY_ACK_NONE;
	e->gen++;
	e->state = GPU_RELAY_STATE_CLOSING;
	os_gpu_relay_kick(mm->context.gpu_kick_fd);
}

/* Consume relay page updates.  Runs in the event kthread under gpu_lock. */
static void gpu_relay_consume(struct mm_struct *mm)
{
	struct gpu_relay_page *rp = gpu_relay_page(mm);
	struct gpu_event_map *ev, *tmp;
	struct gpu_file *gf;

	if (!rp)
		return;
	list_for_each_entry_safe(ev, tmp, &mm->context.gpu_events, node) {
		struct gpu_relay_entry *e;

		if (ev->slot < 0)
			continue;
		e = &rp->entry[ev->slot];
		/* Event mappings are removed explicitly on alloc rollback /
		 * FREE_OS_EVENT / teardown; the relay's CLOSED ack only gates
		 * worker-fd closure, so there is no ack-driven free here. */
		if (e->state == GPU_RELAY_STATE_ACTIVE && e->gen == ev->gen &&
		    e->value_gen != ev->value_gen) {
			u64 now, delta;

			/* value is monotonic per generation; deliver exactly
			 * the delta.  7.2 has no batched eventfd producer
			 * API, so signal once per count. */
			now = e->value;
			delta = now - ev->delivered;
			ev->delivered = now;
			ev->value_gen = e->value_gen;
			while (delta--)
				eventfd_signal_mask(ev->guest_ctx, 0);
		}
	}
	list_for_each_entry(gf, &mm->context.gpu_files, node) {
		struct gpu_relay_entry *e;

		if (gf->slot < 0)
			continue;
		e = &rp->entry[gf->slot];
		if (e->state != GPU_RELAY_STATE_ACTIVE ||
		    e->gen != gf->slot_gen ||
		    e->level_gen == gf->last_level)
			continue;
		/* Relay reports the complete current mask only on level
		 * transitions, which is exactly level-triggered poll
		 * semantics: repeated polls keep returning the bits until a
		 * clean poll round clears them. */
		atomic_set(&gf->poll_mask, e->level);
		gf->last_level = e->level_gen;
		if (e->level)
			wake_up_interruptible(&gf->waitq);
	}
}

/* Schedulable event delivery: never an os_poll() blocking a vCPU.  The
 * notify socket IRQ sets the pending bit and wakes the relay waitqueue;
 * all fd/event state is read with gpu_lock held. */
static int gpu_eventd_fn(void *arg)
{
	struct mm_struct *mm = arg;

	for (;;) {
		/* wait_event re-checks the predicate after every wake, so
		 * the wake_up_process from kthread_stop terminates the wait
		 * deterministically; no os_poll and no completion reinit race. */
		wait_event_interruptible(mm->context.gpu_relay_wq,
			kthread_should_stop() ||
			READ_ONCE(mm->context.gpu_relay_pending));
		if (kthread_should_stop())
			break;
		WRITE_ONCE(mm->context.gpu_relay_pending, 0);
		mutex_lock(&mm->context.gpu_lock);
		gpu_relay_consume(mm);
		mutex_unlock(&mm->context.gpu_lock);
	}
	return 0;
}

void gpu_bridge_teardown(struct mm_struct *mm)
{
	struct gpu_event_map *ev, *tmp;

	if (!mm->context.gpu_id.stack)
		return;
	/* Join the event thread first: it holds no mm reference, so this join
	 * is also what makes its mm access safe (destroy_context frees the
	 * mm only after returning). */
	if (mm->context.gpu_eventd) {
		/* Sets KTHREAD_SHOULD_STOP and wakes the kthread; the wait
		 * predicate makes it exit without any completion dance. */
		kthread_stop(mm->context.gpu_eventd);
		mm->context.gpu_eventd = NULL;
	}
	if (mm->context.gpu_relay_irq) {
		um_free_irq(mm->context.gpu_relay_irq, mm);
		mm->context.gpu_relay_irq = 0;
	}
	if (mm->context.gpu_kick_fd >= 0)
		os_close_file(mm->context.gpu_kick_fd);
	if (mm->context.gpu_notify_fd >= 0)
		os_close_file(mm->context.gpu_notify_fd);
	mm->context.gpu_kick_fd = mm->context.gpu_notify_fd = -1;
	list_for_each_entry_safe(ev, tmp, &mm->context.gpu_events, node) {
		list_del(&ev->node);
		eventfd_ctx_put(ev->guest_ctx);
		kfree(ev);
	}
}

static int gpu_start(struct mm_struct *mm)
{
	struct mm_id *id = &mm->context.gpu_id;
	int error;

	if (id->stack)
		return id->pid > 1 ? 0 : -ECHILD;
	id->stack = __get_free_pages(GFP_KERNEL | __GFP_ZERO,
				    ilog2(STUB_THREAD_PAGES));
	mm->context.gpu_relay_pages =
		__get_free_pages(GFP_KERNEL | __GFP_ZERO,
				 ilog2(STUB_THREAD_PAGES));
	mm->context.gpu_shadow_pages =
		__get_free_pages(GFP_KERNEL | __GFP_ZERO,
				 ilog2(STUB_GPU_SHADOW_PAGES));
	if (!id->stack || !mm->context.gpu_relay_pages ||
	    !mm->context.gpu_shadow_pages)
		goto err_free;

	id->cached_phys_fd = -1;
	/* The relay/shadow pages and the worker clone all append to and flush
	 * the broker's syscall queue, so every operation stays in the same
	 * sync_tlb_lock critical section as start_userspace_cpu. */
	scoped_guard(spinlock_irqsave, &mm->context.sync_tlb_lock) {
		error = os_gpu_pages_map(&mm->context.id,
					 mm->context.gpu_relay_pages,
					 STUB_GPU_RELAY, STUB_THREAD_PAGES);
		if (error)
			goto err_free;
		error = os_gpu_pages_map(&mm->context.id,
					 mm->context.gpu_shadow_pages,
					 STUB_GPU_SHADOW, STUB_GPU_SHADOW_PAGES);
		if (error)
			goto err_unmap_relay;
		error = start_userspace_cpu(&mm->context.id, id,
					    STUB_GPU_DATA);
		if (error)
			/* A clone/handoff failure leaves the worker parked on
			 * STUB_GPU_DATA: kill it and drop that broker mapping
			 * (inside the same lock) before freeing the pages. */
			goto err_kill_worker;
	}
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
			error = irq;
			goto err_kill;
		}
		mm->context.gpu_async_irq = irq;
		pr_info("HARDDIND_GPU_ASYNC guest_tgid=%d host_pid=%d irq=%d\n",
			task_tgid_nr(current), id->pid, irq);
	}

	gpu_relay_page(mm)->magic = GPU_RELAY_MAGIC;
	error = os_gpu_relay_start(id, &mm->context.gpu_kick_fd,
				   &mm->context.gpu_notify_fd);
	if (error)
		goto err_irq;
	{
		int irq = um_request_irq(UM_IRQ_ALLOC,
					 mm->context.gpu_notify_fd, IRQ_READ,
					 gpu_relay_irq, 0,
					 "harddind-gpu-events", mm);

		if (irq < 0) {
			error = irq;
			goto err_sockets;
		}
		mm->context.gpu_relay_irq = irq;
	}
	mm->context.gpu_eventd = kthread_run(gpu_eventd_fn, mm,
					    "uml-gpu-events");
	if (IS_ERR(mm->context.gpu_eventd)) {
		error = PTR_ERR(mm->context.gpu_eventd);
		mm->context.gpu_eventd = NULL;
		goto err_relay_irq;
	}

	mm->context.gpu_owner = get_task_pid(current, PIDTYPE_TGID);
	pr_info("HARDDIND_GPU_WORKER guest_tgid=%d host_pid=%d\n",
		 task_tgid_nr(current), id->pid);
	return 0;

err_relay_irq:
	um_free_irq(mm->context.gpu_relay_irq, mm);
	mm->context.gpu_relay_irq = 0;
err_sockets:
	os_close_file(mm->context.gpu_kick_fd);
	os_close_file(mm->context.gpu_notify_fd);
	mm->context.gpu_kick_fd = mm->context.gpu_notify_fd = -1;
err_irq:
	if (mm->context.gpu_async_irq) {
		complete_all(&mm->context.gpu_done);
		um_free_irq(mm->context.gpu_async_irq, mm);
		mm->context.gpu_async_irq = 0;
	}
err_kill:
	os_kill_ptraced_process(id->pid, 1);
	if (id->sock)
		os_close_file(id->sock);
	id->sock = 0;
	id->pid = -1;
err_kill_worker:
	/* Clone/handoff failed after the STUB_GPU_DATA mapping was installed
	 * (all queue ops above ran under the same sync_tlb_lock).  Reap the
	 * ptraced worker, close its control socket and remove the worker
	 * stack mapping from the broker before freeing its pages. */
	os_kill_ptraced_process(id->pid, 1);
	if (id->sock)
		os_close_file(id->sock);
	id->sock = 0;
	id->pid = -1;
	scoped_guard(spinlock_irqsave, &mm->context.sync_tlb_lock)
		os_gpu_pages_unmap(&mm->context.id, STUB_GPU_DATA,
				   STUB_THREAD_PAGES);
	scoped_guard(spinlock_irqsave, &mm->context.sync_tlb_lock)
		os_gpu_pages_unmap(&mm->context.id, STUB_GPU_SHADOW,
				   STUB_GPU_SHADOW_PAGES);
err_unmap_relay:
	scoped_guard(spinlock_irqsave, &mm->context.sync_tlb_lock)
		os_gpu_pages_unmap(&mm->context.id, STUB_GPU_RELAY,
				   STUB_THREAD_PAGES);
err_free:
	if (mm->context.gpu_shadow_pages)
		free_pages(mm->context.gpu_shadow_pages,
			   ilog2(STUB_GPU_SHADOW_PAGES));
	if (mm->context.gpu_relay_pages)
		free_pages(mm->context.gpu_relay_pages,
			   ilog2(STUB_THREAD_PAGES));
	free_pages(id->stack, ilog2(STUB_THREAD_PAGES));
	mm->context.gpu_shadow_pages = 0;
	mm->context.gpu_relay_pages = 0;
	id->stack = 0;
	id->pid = -1;
	return error;
}

static long gpu_command(struct mm_struct *mm, int operation, int fd,
			unsigned int command, unsigned long argument,
			void *payload, size_t payload_size)
{
	struct mm_id *id = &mm->context.gpu_id;
	unsigned long flags;
	long result;

	if (!uml_gpu_async_wait) {
		local_irq_save(flags);
		result = os_gpu_command(id, operation, fd, command,
					argument, payload, payload_size);
		local_irq_restore(flags);
		return result;
	}

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
	/* Both the canonical (195) and compatibility (dynamic) cdevs resolve
	 * to the same index, so opening either name is equivalent. */
	struct gpu_cdev *gc = container_of(inode->i_cdev, struct gpu_cdev,
					   cdev);
	struct gpu_device *device;
	struct gpu_file *state;
	long result;

	if (gc->index > GPU_INDEX_MAX)
		return -ENXIO;
	device = &gpu_devices[gc->index];

	if (!uml_gpu_bridge || !parallel_userspace || !mm)
		return -EOPNOTSUPP;
	state = kzalloc(sizeof(*state), GFP_KERNEL);
	if (!state)
		return -ENOMEM;
	init_waitqueue_head(&state->waitq);
	atomic_set(&state->poll_mask, 0);
	state->slot = -1;
	mutex_lock(&mm->context.gpu_lock);
	result = gpu_start(mm);
	if (!result && task_tgid(current) != mm->context.gpu_owner)
		result = -EXDEV;
	if (!result)
		result = gpu_command(mm, GPU_OP_OPEN, -1, device->index, 0,
				     NULL, 0);
	if (result >= 0) {
		mmgrab(mm);
		state->mm = mm;
		state->index = device->index;
		state->host_fd = result;
		file->private_data = state;
		list_add(&state->node, &mm->context.gpu_files);
	}
	mutex_unlock(&mm->context.gpu_lock);
	if (result < 0) {
		kfree(state);
		return result;
	}
	return 0;
}

static int gpu_release(struct inode *inode, struct file *file)
{
	struct gpu_file *state = file->private_data;
	struct mm_struct *mm = state->mm;
	long result = 0;

	mutex_lock(&mm->context.gpu_lock);
	list_del(&state->node);
	if (READ_ONCE(mm->context.gpu_id.pid) > 1) {
		if (state->slot >= 0)
			/* The relay closes this worker fd after excluding it
			 * from its poll set; never close it twice. */
			gpu_slot_retire(mm, state->slot);
		else
			result = gpu_command(mm, GPU_OP_CLOSE,
					     state->host_fd, 0, 0, NULL, 0);
	}
	mutex_unlock(&mm->context.gpu_lock);
	mmdrop(mm);
	kfree(state);
	return result < 0 ? result : 0;
}

/* ---- shadow transport ------------------------------------------------ */

/* One immutable per-call snapshot.  The user block is copied into the
 * per-mm shadow exactly ONCE while gpu_lock is held; every validation,
 * field translation and the worker ioctl read those same bytes, and poked
 * fields are restored from the same snapshot.  A second user copy never
 * happens, so concurrent guest mutation cannot swap a validated pointer
 * for a worker-private address after validation. */
struct gpu_nested_ptr {
	u16 ptr_off;			/* u64 guest-pointer field offset */
	u16 len_off;			/* length field offset, or 0xffff */
	u8 len_width;			/* 4 or 8 bytes */
	u8 pad;
	u32 fixed_len;			/* used when len_off == 0xffff */
	bool writable;
	bool unbounded_ok;	/* length U64_MAX = driver decides; no probe */
};

struct gpu_fd_field {
	u16 off;			/* s32 embedded descriptor offset */
	bool uvm;			/* must name the UVM node */
};

/* A descriptor reference kept strong for the whole ioctl.  The guest file
 * is fget()'d from the snapshot value and fput()'d only after the worker
 * ioctl and copyback, outside gpu_lock (a final put runs gpu_release, which
 * takes gpu_lock).  A concurrent guest close can therefore neither close
 * nor recycle the worker descriptor before the ioctl. */
struct gpu_fd_hold {
	struct file *file;
	int host_fd;
};

static long gpu_hold_fd(struct gpu_file *state, const void *shadow,
			const struct gpu_fd_field *f, struct gpu_fd_hold *hold)
{
	s32 guest_fd;

	hold->file = NULL;
	hold->host_fd = -1;
	memcpy(&guest_fd, shadow + f->off, sizeof(guest_fd));
	if (guest_fd < 0)
		return 0;
	hold->file = fget(guest_fd);
	if (!hold->file)
		return -EBADF;
	if (hold->file->f_op != &gpu_fops) {
		fput(hold->file);
		hold->file = NULL;
		return -EINVAL;
	}
	{
		struct gpu_file *other = hold->file->private_data;

		if (other->mm != state->mm) {
			fput(hold->file);
			hold->file = NULL;
			return -EXDEV;
		}
		if (f->uvm && other->index != GPU_KIND_UVM) {
			fput(hold->file);
			hold->file = NULL;
			return -EINVAL;
		}
		hold->host_fd = other->host_fd;
	}
	return 0;
}

static void gpu_put_holds(struct gpu_fd_hold *holds, unsigned int n)
{
	unsigned int i;

	/* Intentionally called after gpu_lock is released. */
	for (i = 0; i < n; i++)
		if (holds[i].file)
			fput(holds[i].file);
}

/* Fault in a nested guest range named by a snapshot pointer field.  The
 * driver resolves these addresses in the shared mm; out-of-guest-range
 * values become ~0ULL so they can never name worker-private stub pages. */
static long gpu_check_nested(const void *shadow,
			     const struct gpu_nested_ptr *d, u64 *poke)
{
	u64 ptr;
	u64 len = d->fixed_len;
	void __user *uptr;

	memcpy(&ptr, shadow + d->ptr_off, sizeof(ptr));
	*poke = ptr;
	if (!ptr)
		return 0;
	if (d->len_off != 0xffff) {
		if (d->len_width == 4) {
			u32 l32;

			memcpy(&l32, shadow + d->len_off, sizeof(l32));
			len = l32;
		} else {
			memcpy(&len, shadow + d->len_off, sizeof(len));
		}
	}
	if (!len)
		return 0;
	if (len == U64_MAX && d->unbounded_ok)
		return 0;
	len = min_t(u64, len, TASK_SIZE & PAGE_MASK);
	uptr = (void __user *)(unsigned long)ptr;
	/* Returns 1 when the pointer is out of guest range and the caller
	 * must rewrite it to ~0ULL; never name worker-private mappings. */
	if (!access_ok(uptr, max_t(u64, len, 1)))
		return 1;
	fault_in_readable(uptr, len);
	if (d->writable)
		fault_in_safe_writeable(uptr, len);
	return 0;
}

/* Explicit bounded capacities: array parameters decay to pointers, so
 * ARRAY_SIZE() cannot size them inside the forwarder.  More pokes or
 * embedded descriptors than these constants must fail closed rather than
 * silently truncate. */
#define GPU_MAX_POKES 8
#define GPU_MAX_FIELDS 4

/* The single-snapshot forward.  Returns held descriptor references in
 * @holds (caller drops them outside gpu_lock); poke_out records, per
 * modified field, the snapshot value for restoration. */
static long gpu_shadow_snapshot(struct gpu_file *state, unsigned int command,
				unsigned long arg, size_t size,
				const struct gpu_nested_ptr *nesteds,
				unsigned int nnested,
				const struct gpu_fd_field *fds, unsigned int nfds,
				struct gpu_fd_hold *holds,
				u64 poke_off[GPU_MAX_POKES],
				u64 poke_val[GPU_MAX_POKES],
				unsigned int *npokes)
{
	void *shadow = (void *)state->mm->context.gpu_shadow_pages;
	struct mm_struct *mm = state->mm;
	unsigned int i, n = 0;
	long result;

	if (size > STUB_GPU_SHADOW_PAGES * UM_KERN_PAGE_SIZE ||
	    nfds > GPU_MAX_FIELDS)
		return -EINVAL;
	mutex_lock(&mm->context.gpu_lock);
	if (size && copy_from_user(shadow, (void __user *)arg, size)) {
		mutex_unlock(&mm->context.gpu_lock);
		return -EFAULT;
	}
	/* Validate nested ranges from the snapshot only. */
	for (i = 0; i < nnested; i++) {
		u64 orig = 0;

		if (gpu_check_nested(shadow, &nesteds[i], &orig)) {
			/* Fail closed if the poke table is exhausted: never
			 * forward the unreachable pointer unrewritten. */
			if (n >= GPU_MAX_POKES - nfds) {
				result = -EINVAL;
				goto err_unlocked;
			}
			poke_off[n] = nesteds[i].ptr_off;
			poke_val[n] = orig;
			*(u64 *)(shadow + nesteds[i].ptr_off) = ~0ULL;
			n++;
		}
	}
	/* Translate embedded descriptors from the snapshot; keep every
	 * referenced guest file until after the ioctl. */
	for (i = 0; i < nfds; i++) {
		result = gpu_hold_fd(state, shadow, &fds[i], &holds[i]);
		if (result < 0)
			goto err_unlocked;
		if (holds[i].file) {
			if (n >= GPU_MAX_POKES) {
				result = -EINVAL;
				goto err_unlocked;
			}
			poke_off[n] = fds[i].off;
			poke_val[n] = 0;
			memcpy(&poke_val[n], shadow + fds[i].off, sizeof(s32));
			*(s32 *)(shadow + fds[i].off) = holds[i].host_fd;
			n++;
		}
	}
	*npokes = n;
	/* Pages prefaulted above must be visible in the broker/worker page
	 * tables before the worker dereferences them. */
	result = um_tlb_sync(mm);
	if (result)
		goto err_locked;
	result = gpu_command(mm, GPU_OP_IOCTL_ADDR, state->host_fd, command,
			     STUB_GPU_SHADOW, NULL, 0);
	/* Restore every modified field from the immutable snapshot: FD
	 * fields are s32, rewritten pointer fields are u64. */
	for (i = 0; i < n; i++) {
		bool is_fd = false;
		unsigned int j;

		for (j = 0; j < nfds; j++)
			if (poke_off[i] == fds[j].off)
				is_fd = true;
		if (is_fd) {
			s32 v = (s32)poke_val[i];

			memcpy(shadow + poke_off[i], &v, sizeof(v));
		} else {
			memcpy(shadow + poke_off[i], &poke_val[i],
			       sizeof(u64));
		}
	}
	if (size && result != -EFAULT &&
	    copy_to_user((void __user *)arg, shadow, size))
		result = -EFAULT;
err_locked:
	mutex_unlock(&mm->context.gpu_lock);
	return result;
err_unlocked:
	mutex_unlock(&mm->context.gpu_lock);
	return result;
}

/* Convenience wrapper for callers with zero or few nested fields. */
static long gpu_forward_block(struct gpu_file *state, unsigned int command,
			      unsigned long arg, size_t size,
			      const struct gpu_nested_ptr *nesteds,
			      unsigned int nnested,
			      const struct gpu_fd_field *fds, unsigned int nfds)
{
	u64 poke_off[GPU_MAX_POKES], poke_val[GPU_MAX_POKES];
	struct gpu_fd_hold holds[GPU_MAX_FIELDS] = {};
	unsigned int npokes = 0;
	long result;

	result = gpu_shadow_snapshot(state, command, arg, size, nesteds,
				    nnested, fds, nfds, holds, poke_off,
				    poke_val, &npokes);
	gpu_put_holds(holds, nfds);
	return result;
}

/* REGISTER_FD (0xC9): owner 0013 path, numeric guest fd translated. */
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
	mutex_lock(&state->mm->context.gpu_lock);
	result = gpu_command(state->mm, GPU_OP_REGISTER_FD, state->host_fd,
			     command, host_fd, NULL, 0);
	mutex_unlock(&state->mm->context.gpu_lock);
	if (source)
		fput(source);
	if (result != -EFAULT &&
	    copy_to_user((void __user *)arg, &guest_fd, sizeof(guest_fd)))
		return -EFAULT;
	return result;
}

/* ---- OS events -------------------------------------------------------- */

/* Resolve a guest (hClient, eventfd) identity to the registered worker fd.
 * Caller holds gpu_lock.  The guest eventfd context is the unforgeable
 * identity; hClient is the driver-side scope (worker event_list key), so a
 * reused guest fd number for a different eventfd can never select it.
 * Proven on 550.120 osapi.c:484-503 and os.c:1440-1465. */
static struct gpu_event_map *gpu_event_lookup(struct mm_struct *mm,
					      struct eventfd_ctx *ctx,
					      u32 h_client, int guest_fd)
{
	struct gpu_event_map *it;

	if (!ctx)
		return NULL;
	list_for_each_entry(it, &mm->context.gpu_events, node)
		if (it->guest_ctx == ctx && it->h_client == h_client &&
		    it->guest_fd == guest_fd)
			return it;
	return NULL;
}

/* Class 0x79 attach lookup: the worker event_list is keyed by
 * (hClient, fd) only (os.c:1452-1458); the unforgeable eventfd context
 * supplies the fd identity.  One (ctx, hClient) registration exists at a
 * time, so no guest numeric fd is required here. */
static struct gpu_event_map *gpu_event_lookup_ctx(struct mm_struct *mm,
						  struct eventfd_ctx *ctx,
						  u32 h_client)
{
	struct gpu_event_map *it;

	if (!ctx)
		return NULL;
	list_for_each_entry(it, &mm->context.gpu_events, node)
		if (it->guest_ctx == ctx && it->h_client == h_client)
			return it;
	return NULL;
}

/* Undo a registration that did not complete: unpublish the relay slot (the
 * relay closes the worker eventfd after excluding it from its poll set) and
 * release the bridge mapping.  Caller holds gpu_lock. */
static void gpu_event_abort(struct mm_struct *mm, struct gpu_event_map *ev)
{
	gpu_slot_retire(mm, ev->slot);
	list_del(&ev->node);
	eventfd_ctx_put(ev->guest_ctx);
	kfree(ev);
}

static long gpu_alloc_os_event(struct gpu_file *state, unsigned int command,
			       unsigned long arg)
{
	struct gpu_event_map *ev;
	struct gpu_os_event params;
	struct eventfd_ctx *ctx;
	struct file *f;
	void *shadow;
	int host_fd, slot, guest_fd, result = 0;
	unsigned int gen;
	u32 h_client;

	/* Immutable input snapshot. */
	if (copy_from_user(&params, (void __user *)arg, sizeof(params)))
		return -EFAULT;
	if (params.fd < 0)
		return -EBADF;
	guest_fd = params.fd;
	h_client = params.hClient;
	f = fget(guest_fd);
	if (!f)
		return -EBADF;
	ctx = eventfd_ctx_fileget(f);
	fput(f);
	if (IS_ERR(ctx))
		return PTR_ERR(ctx);

	ev = kzalloc(sizeof(*ev), GFP_KERNEL);
	if (!ev) {
		eventfd_ctx_put(ctx);
		return -ENOMEM;
	}

	mutex_lock(&state->mm->context.gpu_lock);
	/* The worker driver rejects a second registration for the same
	 * (hClient, fd) pair with ioctl rc 0 and embedded Status
	 * NV_ERR_INVALID_ARGUMENT (escape.c:691 stores the RM status;
	 * osapi.c:494-498 returns it; 550.120 value 0x1F).  Reproduce that
	 * exact error channel: never convert it to a syscall errno. */
	if (gpu_event_lookup(state->mm, ctx, h_client, guest_fd)) {
		params.status = 0x1F;
		if (copy_to_user((void __user *)arg, &params, sizeof(params)))
			result = -EFAULT;
		else
			result = 0;
		mutex_unlock(&state->mm->context.gpu_lock);
		eventfd_ctx_put(ctx);
		kfree(ev);
		return result;
	}
	host_fd = os_gpu_eventfd(&state->mm->context.gpu_id);
	slot = host_fd >= 0 ? gpu_slot_alloc(state->mm) : host_fd;
	if (host_fd < 0 || slot < 0) {
		int code = host_fd < 0 ? host_fd : slot;

		if (host_fd >= 0)
			gpu_command(state->mm, GPU_OP_CLOSE, host_fd, 0, 0,
				   NULL, 0);
		mutex_unlock(&state->mm->context.gpu_lock);
		eventfd_ctx_put(ctx);
		kfree(ev);
		return code;
	}
	ev->guest_ctx = ctx;
	ev->h_client = h_client;
	ev->guest_fd = guest_fd;
	ev->host_fd = host_fd;
	ev->slot = slot;
	gpu_slot_publish(state->mm, slot, host_fd,
			UML_GPU_RELAY_KIND_EVENTFD, &gen);
	ev->gen = gen;
	list_add(&ev->node, &state->mm->context.gpu_events);

	/* The worker driver registers its event_list with (hClient, fd);
	 * this fd must be the worker eventfd descriptor. */
	shadow = (void *)state->mm->context.gpu_shadow_pages;
	params.fd = host_fd;
	memcpy(shadow, &params, sizeof(params));
	result = gpu_command(state->mm, GPU_OP_IOCTL_ADDR, state->host_fd,
			     command, STUB_GPU_SHADOW, NULL, 0);
	if (result < 0) {
		gpu_event_abort(state->mm, ev);
		mutex_unlock(&state->mm->context.gpu_lock);
		return result;
	}
	{
		struct gpu_os_event out;

		memcpy(&out, shadow, sizeof(out));
		/* RM-level failure lives in Status even on a successful
		 * syscall (escape.c returns 0): no worker registration
		 * happened, so roll the mapping back.  Restore the guest fd
		 * on every copyback - the host never modifies it. */
		if (out.status != 0)
			gpu_event_abort(state->mm, ev);
		out.fd = guest_fd;
		result = copy_to_user((void __user *)arg, &out, sizeof(out)) ?
			-EFAULT : 0;
	}
	mutex_unlock(&state->mm->context.gpu_lock);
	return result;
}

static long gpu_free_os_event(struct gpu_file *state, unsigned int command,
			      unsigned long arg)
{
	struct gpu_os_event params;
	struct gpu_event_map *ev = NULL, *doomed = NULL;
	struct eventfd_ctx *ctx = NULL;
	struct file *f;
	void *shadow;
	int result, guest_fd;
	u32 h_client;

	if (copy_from_user(&params, (void __user *)arg, sizeof(params)))
		return -EFAULT;
	guest_fd = params.fd;
	h_client = params.hClient;
	if (guest_fd >= 0) {
		f = fget(guest_fd);
		if (f) {
			ctx = eventfd_ctx_fileget(f);
			fput(f);
			if (IS_ERR(ctx))
				ctx = NULL;
		}
	}
	mutex_lock(&state->mm->context.gpu_lock);
	ev = gpu_event_lookup(state->mm, ctx, h_client, guest_fd);
	params.fd = ev ? ev->host_fd : -1;
	shadow = (void *)state->mm->context.gpu_shadow_pages;
	memcpy(shadow, &params, sizeof(params));
	result = gpu_command(state->mm, GPU_OP_IOCTL_ADDR, state->host_fd,
			     command, STUB_GPU_SHADOW, NULL, 0);
	if (result >= 0) {
		struct gpu_os_event out;

		memcpy(&out, shadow, sizeof(out));
		/* Retire only when both the syscall and the driver RM status
		 * report release; a failed free keeps the mapping for retry
		 * and the relay keeps delivering completion counts. */
		if (ev && out.status == 0)
			doomed = ev;
		out.fd = guest_fd;
		if (result != -EFAULT &&
		    copy_to_user((void __user *)arg, &out, sizeof(out)))
			result = -EFAULT;
	}
	if (doomed) {
		gpu_slot_retire(state->mm, doomed->slot);
		list_del(&doomed->node);
		eventfd_ctx_put(doomed->guest_ctx);
		kfree(doomed);
	}
	mutex_unlock(&state->mm->context.gpu_lock);
	if (ctx)
		eventfd_ctx_put(ctx);
	return result;
}

/* ---- RM alloc/free/memory --------------------------------------------- */

static long gpu_rm_request(struct gpu_file *state, unsigned int command,
			   void *payload, size_t size)
{
	long result;

	mutex_lock(&state->mm->context.gpu_lock);
	result = gpu_command(state->mm, GPU_OP_IOCTL_PAYLOAD,
			     state->host_fd, command, 0, payload, size);
	mutex_unlock(&state->mm->context.gpu_lock);
	return result;
}

static long gpu_rm_copyback(unsigned long arg, const void *payload,
			    size_t size, long result)
{
	if (result != -EFAULT &&
	    copy_to_user((void __user *)arg, payload, size))
		return -EFAULT;
	return result;
}

/* Screen a nested pointer held in the immutable local header snapshot:
 * fault in the canonical guest range and return the value the host driver
 * must see (~0ULL when out of range so worker-private pages cannot be
 * named).  Mirrors owner 0026 gpu_alloc_pointer. */
static u64 gpu_alloc_pointer(u64 pointer, size_t size, bool writeable)
{
	void __user *nested = (void __user *)(unsigned long)pointer;

	if (!pointer)
		return 0;
	if (!access_ok(nested, size))
		return ~0ULL;
	fault_in_readable(nested, size);
	if (writeable)
		fault_in_safe_writeable(nested, size);
	return pointer;
}

/* The two NV_ESC_RM_ALLOC layouts share their first 24 bytes only. */
union gpu_alloc_params {
	struct gpu_nvos21 v21;
	struct gpu_nvos64 v64;
};

/* NV0005_ALLOC_PARAMETERS (cl0005.h:39-47): the u64 data@16 is NOT a guest
 * VA.  For NV01_EVENT_OS_EVENT (0x79) the fixed driver casts it to NvU32 and
 * looks up its worker event_list by (allocating client hClient == NVOS64
 * hRoot, fd) - event.c:173-180 and os.c:1440-1465.  The bridge rewrites it
 * to the worker fd registered by ALLOC_OS_EVENT for the same scope, bounces
 * the 24-byte child through the stub payload, and restores the original
 * guest value on copyback regardless of the embedded RM status. */
struct gpu_nv0005 {
	u32 hParentClient;
	u32 hSrcResource;
	u32 hClass;
	u32 notifyIndex;
	u64 data;
};
static_assert(sizeof(struct gpu_nv0005) == 24);
static_assert(offsetof(struct gpu_nv0005, data) == 16);
static_assert(sizeof(struct gpu_nvos64) + sizeof(struct gpu_nv0005) <=
	      STUB_GPU_PAYLOAD_SIZE);

static long gpu_rm_alloc_event(struct gpu_file *state, unsigned int command,
			       unsigned long arg,
			       union gpu_alloc_params *header, size_t size)
{
	/* Always carry the 48-byte header form in the bounce block; the NVOS21
	 * ioctl still reads/writes its first 32 bytes, and the child pointer
	 * names the fixed offset after the header.  48+24 = 72 <= 128. */
	struct {
		struct gpu_nvos64 hdr;
		struct gpu_nv0005 child;
	} block = {};
	struct gpu_nv0005 child;	/* immutable one-read child snapshot */
	struct eventfd_ctx *ctx = NULL;
	struct file *f = NULL;
	struct gpu_event_map *ev;
	void __user *child_uptr;
	u64 palloc = header->v64.pAllocParms;
	u64 original_data;		/* full 64-bit guest value */
	s32 guest_data;
	long result;

	if (size != sizeof(struct gpu_nvos21) &&
	    size != sizeof(struct gpu_nvos64))
		return -EINVAL;
	if (!palloc)
		return -EINVAL;
	child_uptr = (void __user *)(unsigned long)palloc;

	/* Everything derived from userspace is snapshotted BEFORE gpu_lock.
	 * The single child copy is the only input; no later user read can
	 * change it. */
	if (copy_from_user(&child, child_uptr, sizeof(child)))
		return -EFAULT;
	fault_in_readable(child_uptr, sizeof(child));
	fault_in_safe_writeable(child_uptr, sizeof(child));
	original_data = child.data;
	guest_data = (s32)(u32)original_data;
	/* Pin the referenced guest file OUTSIDE gpu_lock: if it is a GPU file
	 * and this is its last ref, the eventual fput runs gpu_release which
	 * takes gpu_lock.  Keep both refs through command and copyback and
	 * drop them only after unlock.  A non-eventfd (wrong file type) and
	 * an invalid descriptor simply leave ctx NULL; the worker then
	 * reports its own NV_ERR_OBJECT_NOT_FOUND. */
	if (guest_data >= 0) {
		f = fget(guest_data);
		if (f) {
			ctx = eventfd_ctx_fileget(f);
			if (IS_ERR(ctx))
				ctx = NULL;
		}
	}

	if (!access_ok(child_uptr, sizeof(child))) {
		/* Unreachable child pointer: let the fixed driver report its
		 * copy-fault status with its own ordering. */
		header->v64.pAllocParms = ~0ULL;
		result = gpu_rm_request(state, command, &header->v64, size);
		header->v64.pAllocParms = palloc;
		result = gpu_rm_copyback(arg, &header->v64, size, result);
		goto out_puts;
	}

	mutex_lock(&state->mm->context.gpu_lock);
	/* Identity scope: allocating client (header hRoot) plus the
	 * unforgeable eventfd context.  A reused number for another eventfd
	 * has a different ctx and cannot match; the held file ref also makes
	 * a concurrent close/reopen irrelevant for this call. */
	ev = gpu_event_lookup_ctx(state->mm, ctx, header->v64.hRoot);
	block.child = child;
	block.child.data = ev ? (u64)(u32)ev->host_fd : (u64)(u32)~0U;

	/* Bounce header+child through the 128-byte stub payload. */
	memcpy(&block.hdr, &header->v64, size);
	block.hdr.pAllocParms = STUB_GPU_DATA +
		offsetof(struct stub_data, gpu_payload) +
		sizeof(block.hdr);
	result = um_tlb_sync(state->mm);
	if (!result)
		result = gpu_command(state->mm, GPU_OP_IOCTL_PAYLOAD,
				     state->host_fd, command, 0, &block,
				     sizeof(block.hdr) + sizeof(block.child));
	/* Raw driver header output MUST reach userspace: copy the bounced
	 * header (real RM Status, hObjectNew and any driver mutations) back
	 * over the local header for the exact wire size, then restore the
	 * original pAllocParms so the guest sees its own pointer.  This holds
	 * for success and embedded-RM-failure alike. */
	memcpy(&header->v64, &block.hdr, size);
	header->v64.pAllocParms = palloc;
	/* The driver uses only the low 32 bits of data for lookup; copyback
	 * restores the EXACT original 64-bit value (high bits included). */
	block.child.data = original_data;
	if (result != -EFAULT &&
	    copy_to_user(child_uptr, &block.child, sizeof(block.child)))
		result = -EFAULT;
	mutex_unlock(&state->mm->context.gpu_lock);
	result = gpu_rm_copyback(arg, &header->v64, size, result);
out_puts:
	if (f)
		fput(f);
	if (ctx)
		eventfd_ctx_put(ctx);
	return result;
}

/* 0x2b NV_ESC_RM_ALLOC.  The header is snapshotted once; 32-byte NVOS21 and
 * 48-byte NVOS64 keep distinct field offsets.  Root classes follow the
 * 0016 no-nested-data path (flags/pRights restrictions preserved).  cb33
 * keeps owner 0026/0027 semantics (canonical 4-byte hClient block plus
 * read-only 4-byte rights mask, FINN flags forwarded to the driver).  Every
 * other typed class faults its canonical pAllocParms block; unknown
 * classes fail explicitly rather than being forwarded blindly. */
static long gpu_rm_alloc(struct gpu_file *state, unsigned int command,
			 unsigned long arg, size_t size)
{
	union gpu_alloc_params params;
	u64 alloc_pointer, rights_pointer = 0;
	bool access_api = size == sizeof(struct gpu_nvos64);
	const struct gpu_alloc_class *cls = NULL;
	unsigned int i;
	long result;

	if (size != sizeof(struct gpu_nvos21) &&
	    size != sizeof(struct gpu_nvos64))
		return -EINVAL;
	if (copy_from_user(&params, (void __user *)arg, size))
		return -EFAULT;

	if (params.v64.hClass == 0x00 || params.v64.hClass == 0x01 ||
	    params.v64.hClass == 0x41) {
		if (params.v64.pAllocParms ||
		    (access_api ? params.v64.paramsSize :
		     params.v21.paramsSize))
			return -EOPNOTSUPP;
		if (access_api &&
		    (params.v64.pRightsRequested || params.v64.flags))
			return -EOPNOTSUPP;
		result = gpu_rm_request(state, command, &params, size);
		return gpu_rm_copyback(arg, &params, size, result);
	}

	for (i = 0; i < ARRAY_SIZE(gpu_alloc_class_table); i++)
		if (gpu_alloc_class_table[i].hClass == params.v64.hClass) {
			cls = &gpu_alloc_class_table[i];
			break;
		}
	if (!cls)
		/* Typed ABI gap: unknown/private class.  Recorded in
		 * REPORT.md; never guessed or raw-forwarded. */
		return -EOPNOTSUPP;
	if (cls->inner_handle_off != 0xffff)
		/* Inner handle semantics (class 0x79): the child field at
		 * inner_handle_off names a registered OS event, not a VA. */
		return gpu_rm_alloc_event(state, command, arg, &params, size);

	alloc_pointer = params.v64.pAllocParms;
	if (cls->canonical_size) {
		params.v64.pAllocParms = gpu_alloc_pointer(alloc_pointer,
					cls->canonical_size, true);
	} else {
		/* Resource carries no allocation parameters: the driver
		 * copies nothing at pAllocParms. */
		params.v64.pAllocParms = 0;
	}
	if (access_api && params.v64.pRightsRequested) {
		/* RS_ACCESS_MASK is one u32 (rs_access.h); numeric guest VA,
		 * read by the worker in the shared mm. */
		rights_pointer = params.v64.pRightsRequested;
		params.v64.pRightsRequested =
			gpu_alloc_pointer(rights_pointer, sizeof(u32), false);
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

/* 0x27 NV_ESC_RM_ALLOC_MEMORY.  hClass 0x71 OS descriptors pin the guest
 * range pMemory[0..limit]; the trailing descriptor at fd@48 is translated.
 * All values come from the single shadow snapshot. */
static long gpu_rm_alloc_memory(struct gpu_file *state, unsigned int command,
				unsigned long arg)
{
	/* U64_MAX length means "no range bound"; cap the prefault probe. */
	struct gpu_nested_ptr nested = {
		.ptr_off = offsetof(struct gpu_nvos02, pMemory),
		.len_off = offsetof(struct gpu_nvos02, limit),
		.len_width = 8, .fixed_len = 0, .writable = true,
		.unbounded_ok = true,
	};
	struct gpu_fd_field fd = {
		.off = offsetof(struct gpu_nvos02, fd), .uvm = false,
	};
	return gpu_forward_block(state, command, arg,
				 sizeof(struct gpu_nvos02), &nested, 1,
				 &fd, 1);
}

/* 0x4e NV_ESC_RM_MAP_MEMORY: descriptor at fd@48; pLinearAddress@32 names
 * the guest CPU range of length@24. */
static long gpu_rm_map_memory(struct gpu_file *state, unsigned int command,
			      unsigned long arg)
{
	struct gpu_nested_ptr nested = {
		.ptr_off = offsetof(struct gpu_nvos33, pLinearAddress),
		.len_off = offsetof(struct gpu_nvos33, length),
		.len_width = 8, .fixed_len = 0, .writable = true,
	};
	struct gpu_fd_field fd = {
		.off = offsetof(struct gpu_nvos33, fd), .uvm = false,
	};

	return gpu_forward_block(state, command, arg,
				 sizeof(struct gpu_nvos33), &nested, 1,
				 &fd, 1);
}

/* 0x4f NV_ESC_RM_UNMAP_MEMORY: pLinearAddress@16 points at the 8-byte
 * mapped-address value; no descriptor wrapper. */
static long gpu_rm_unmap_memory(struct gpu_file *state, unsigned int command,
				unsigned long arg)
{
	struct gpu_nested_ptr nested = {
		.ptr_off = offsetof(struct gpu_nvos34, pLinearAddress),
		.len_off = 0xffff, .fixed_len = sizeof(u64),
		.writable = false,
	};

	return gpu_forward_block(state, command, arg,
				 sizeof(struct gpu_nvos34), &nested, 1,
				 NULL, 0);
}

/* 0x5e NV_ESC_UPDATE_DEVICE_MAPPING_INFO: two numeric guest addresses. */
static long gpu_rm_update_mapping(struct gpu_file *state,
				  unsigned int command, unsigned long arg)
{
	struct gpu_nested_ptr nested[2] = {
		{ .ptr_off = offsetof(struct gpu_nvos56, pOldCpuAddress),
		  .len_off = 0xffff, .fixed_len = 1, .writable = false },
		{ .ptr_off = offsetof(struct gpu_nvos56, pNewCpuAddress),
		  .len_off = 0xffff, .fixed_len = 1, .writable = false },
	};

	return gpu_forward_block(state, command, arg,
				 sizeof(struct gpu_nvos56), nested, 2,
				 NULL, 0);
}

/* ---- RM_CONTROL (0x2a) ------------------------------------------------- */

/* 0022 build-version two-level snapshot, kept verbatim. */
static long gpu_rm_build_version(struct gpu_file *state, unsigned int command,
				 unsigned long arg, struct gpu_nvos54 *header)
{
	struct {
		struct gpu_nvos54 header;
		struct gpu_build_version child;
	} payload;
	u64 original_strings[3];
	void __user *child;
	bool copied;
	int i;
	long result;

	static_assert(sizeof(payload) <= STUB_GPU_PAYLOAD_SIZE);
	child = (void __user *)(unsigned long)header->params;
	copied = !copy_from_user(&payload.child, child, sizeof(payload.child));
	if (copied) {
		memcpy(original_strings, payload.child.strings,
		       sizeof(original_strings));
		for (i = 0; i < 3; i++) {
			void __user *leaf =
				(void __user *)(unsigned long)original_strings[i];
			size_t length = min_t(u32, payload.child.size, 256);

			if (!leaf)
				continue;
			if (!access_ok(leaf, max_t(size_t, length, 1))) {
				payload.child.strings[i] = ~0ULL;
				continue;
			}
			fault_in_safe_writeable(leaf, length);
		}
	}
	payload.header = *header;
	if (copied)
		payload.header.params = STUB_GPU_DATA +
			offsetof(struct stub_data, gpu_payload) +
			offsetof(typeof(payload), child);
	else
		payload.header.params = child ? ~0ULL : 0;
	result = um_tlb_sync(state->mm);
	if (result)
		return result;
	result = gpu_rm_request(state, command, &payload, sizeof(payload));
	if (copied && result != -EFAULT) {
		unsigned long uncopied;

		memcpy(payload.child.strings, original_strings,
		       sizeof(original_strings));
		/* ctrlparamRelease intentionally ignores child copyout
		 * failures: preserve partial writes and success on a
		 * read-only child (owner 0022 semantics). */
		uncopied = copy_to_user(child, &payload.child,
					sizeof(payload.child));
		(void)uncopied;
	}
	payload.header.params = header->params;
	return gpu_rm_copyback(arg, &payload.header,
			       sizeof(payload.header), result);
}

/* Descriptor for a child structure pointer member with a fixed or
 * count-scaled byte length.  len_off 0xff means fixed cap. */
struct gpu_child_ptr_desc {
	u8 ptr_off;
	u8 len_off;
	u8 scale;
	u16 cap;
};

/* Bounce a small pointer-bearing child next to the 32-byte header in the
 * 128-byte payload: same immutable-snapshot mechanics as 0x101.  Nested
 * guest pointers stay numeric (same-VA shared mm); out-of-TASK_SIZE ones
 * become ~0ULL so they cannot name worker-private mappings. */
static long gpu_rm_control_bounce(struct gpu_file *state, unsigned int command,
				  unsigned long arg,
				  const struct gpu_nvos54 *header,
				  u16 child_size,
				  const struct gpu_child_ptr_desc *descs,
				  unsigned int ndescs)
{
	u8 block[STUB_GPU_PAYLOAD_SIZE] __aligned(8);
	struct gpu_nvos54 *h = (void *)block;
	void *child_block = block + sizeof(*h);
	u64 saved_ptrs[4];
	void __user *child;
	unsigned int i;
	long result;

	if (sizeof(*h) + child_size > STUB_GPU_PAYLOAD_SIZE ||
	    ndescs > ARRAY_SIZE(saved_ptrs))
		return -EINVAL;
	child = (void __user *)(unsigned long)header->params;
	if (copy_from_user(child_block, child, child_size))
		return -EFAULT;
	for (i = 0; i < ndescs; i++) {
		u64 ptr;
		u32 count = 0;
		size_t bytes;
		void __user *leaf;

		memcpy(&ptr, child_block + descs[i].ptr_off, sizeof(ptr));
		saved_ptrs[i] = ptr;
		if (!ptr)
			continue;
		if (descs[i].len_off != 0xff)
			memcpy(&count, child_block + descs[i].len_off,
			       sizeof(count));
		if (descs[i].len_off == 0xff)
			bytes = descs[i].cap;
		else
			bytes = min_t(size_t,
				      (size_t)count * descs[i].scale,
				      descs[i].cap);
		leaf = (void __user *)(unsigned long)ptr;
		if (access_ok(leaf, max_t(size_t, bytes, 1)))
			fault_in_safe_writeable(leaf, bytes);
		else
			ptr = ~0ULL;
		memcpy(child_block + descs[i].ptr_off, &ptr, sizeof(ptr));
	}
	*h = *header;
	h->params = STUB_GPU_DATA + offsetof(struct stub_data, gpu_payload) +
		sizeof(*h);
	result = um_tlb_sync(state->mm);
	if (result)
		return result;
	result = gpu_rm_request(state, command, block,
				sizeof(*h) + child_size);
	for (i = 0; i < ndescs; i++)
		memcpy(child_block + descs[i].ptr_off, &saved_ptrs[i],
		       sizeof(u64));
	if (result != -EFAULT) {
		if (copy_to_user(child, child_block, child_size))
			return -EFAULT;
		h->params = header->params;
		if (copy_to_user((void __user *)arg, h, sizeof(*h)))
			return -EFAULT;
	}
	return result;
}

/* Commands whose flagged modes the fixed 550.120 driver validates itself;
 * the bridge must not pre-filter their flags (owner 0025/0027).  The other
 * root queries still require the zero-flags form. */
static bool gpu_ctl_flags_driver_checked(u32 command)
{
	return command == 0xd04 || command == 0x1f0;
}

static long gpu_rm_control(struct gpu_file *state, unsigned int command,
			   unsigned long arg)
{
	static const struct gpu_child_ptr_desc id_info_descs[] = {
		{ offsetof(struct gpu_id_info, szName), 0xff, 0,
		  GPU_ID_INFO_NAME_LEN },
	};
	static const struct gpu_child_ptr_desc channellist_descs[] = {
		{ offsetof(struct gpu_channellist, pChannelHandleList),
		  offsetof(struct gpu_channellist, numChannels), 4,
		  GPU_CHANNEL_LIST_CAP * 4 },
		{ offsetof(struct gpu_channellist, pChannelList),
		  offsetof(struct gpu_channellist, numChannels), 4,
		  GPU_CHANNEL_LIST_CAP * 4 },
	};
	static const struct gpu_child_ptr_desc gr_info_descs[] = {
		{ offsetof(struct gpu_gr_info, grInfoList),
		  offsetof(struct gpu_gr_info, grInfoListSize), 1, 8192 },
	};
	struct gpu_nvos54 params;
	u64 original_pointer;
	void __user *nested;
	const struct gpu_ctl_flat *entry = NULL;
	unsigned int i;
	long result;
	size_t expected;

	if (copy_from_user(&params, (void __user *)arg, sizeof(params)))
		return -EFAULT;
	if (params.command == 0x101 && !params.flags)
		return gpu_rm_build_version(state, command, arg, &params);
	if (params.command == 0x202)
		return gpu_rm_control_bounce(state, command, arg, &params,
					     sizeof(struct gpu_id_info),
					     id_info_descs,
					     ARRAY_SIZE(id_info_descs));
	if (params.command == 0x80170d)
		return gpu_rm_control_bounce(state, command, arg, &params,
					     sizeof(struct gpu_channellist),
					     channellist_descs,
					     ARRAY_SIZE(channellist_descs));
	if (params.command == 0x20801201)
		return gpu_rm_control_bounce(state, command, arg, &params,
					     sizeof(struct gpu_gr_info),
					     gr_info_descs,
					     ARRAY_SIZE(gr_info_descs));
	/* Fabric controls carry nested descriptors (devDescriptor); not in
	 * the pinned workload, never forward blindly. */
	if (params.command == 0xfd0104 || params.command == 0xe00101)
		return -EOPNOTSUPP;
	if (gpu_ctl_unverified(params.command))
		return -EOPNOTSUPP;
	for (i = 0; i < ARRAY_SIZE(gpu_ctl_flat_table); i++)
		if (gpu_ctl_flat_table[i].command == params.command) {
			entry = &gpu_ctl_flat_table[i];
			break;
		}
	if (!entry)
		return -EOPNOTSUPP;
	expected = entry->size;
	if (!gpu_ctl_flags_driver_checked(params.command) && params.flags)
		return -EOPNOTSUPP;
	if (params.size != expected)
		return -EOPNOTSUPP;

	original_pointer = params.params;
	if (!expected) {
		if (original_pointer &&
		    !access_ok((void __user *)(unsigned long)original_pointer,
			       1))
			params.params = ~0ULL;
	} else {
		nested = (void __user *)(unsigned long)original_pointer;
		if (access_ok(nested, expected)) {
			fault_in_readable(nested, expected);
			fault_in_safe_writeable(nested, expected);
		} else {
			params.params = ~0ULL;
		}
	}
	result = um_tlb_sync(state->mm);
	if (result)
		return result;
	result = gpu_rm_request(state, command, &params, sizeof(params));
	params.params = original_pointer;
	return gpu_rm_copyback(arg, &params, sizeof(params), result);
}

/* ---- UVM --------------------------------------------------------------- */

static long gpu_uvm_ioctl(struct gpu_file *state, unsigned int command,
			  unsigned long arg)
{
	const struct gpu_uvm_cmd *e = NULL;
	unsigned int i;
	struct gpu_fd_field fd = { 0 };

	if (command == 0x30000001U || command == 0x30000002U ||
	    command < 0x100) {
		for (i = 0; i < ARRAY_SIZE(gpu_uvm_table); i++)
			if (gpu_uvm_table[i].request == command) {
				e = &gpu_uvm_table[i];
				break;
			}
	}
	if (!e)
		return -EOPNOTSUPP;
	/* The block is snapshotted once inside gpu_forward_block; the
	 * embedded descriptor is read from that same snapshot and its guest
	 * file held until after the ioctl.  Numeric VA fields (requestedBase,
	 * length, gpuUuid structures) contain no userspace pointers beyond
	 * the fixed block, per the header audit. */
	if (e->fd_off >= 0) {
		fd.off = e->fd_off;
		fd.uvm = e->fd_is_uvm;
		return gpu_forward_block(state, command, arg, e->size,
					 NULL, 0, &fd, 1);
	}
	return gpu_forward_block(state, command, arg, e->size, NULL, 0,
				 NULL, 0);
}

/* ---- dispatch ---------------------------------------------------------- */

static long gpu_ioctl(struct file *file, unsigned int command,
		      unsigned long arg)
{
	struct gpu_file *state = file->private_data;
	size_t size = _IOC_SIZE(command);
	unsigned int nr;

	if (current->mm != state->mm ||
	    task_tgid(current) != state->mm->context.gpu_owner)
		return -EXDEV;

	if (state->index == GPU_KIND_UVM)
		return gpu_uvm_ioctl(state, command, arg);

	if (_IOC_TYPE(command) != 'F' ||
	    _IOC_DIR(command) != (_IOC_READ | _IOC_WRITE))
		return -EOPNOTSUPP;
	nr = _IOC_NR(command);

	switch (nr) {
	case 201:				/* NV_ESC_REGISTER_FD */
		return gpu_register_fd(state, command, arg);
	case 206:				/* NV_ESC_ALLOC_OS_EVENT */
		return gpu_alloc_os_event(state, command, arg);
	case 207:				/* NV_ESC_FREE_OS_EVENT */
		return gpu_free_os_event(state, command, arg);
	case 0x27:
		if (size != sizeof(struct gpu_nvos02))
			return -EINVAL;
		return gpu_rm_alloc_memory(state, command, arg);
	case 0x29:
		if (size != sizeof(struct gpu_nvos00))
			return -EINVAL;
		return gpu_rm_free(state, command, arg);
	case 0x2a:
		if (size != sizeof(struct gpu_nvos54))
			return -EINVAL;
		return gpu_rm_control(state, command, arg);
	case 0x2b:
		return gpu_rm_alloc(state, command, arg, size);
	case 0x4e:
		if (size != sizeof(struct gpu_nvos33))
			return -EINVAL;
		return gpu_rm_map_memory(state, command, arg);
	case 0x4f:
		if (size != sizeof(struct gpu_nvos34))
			return -EINVAL;
		return gpu_rm_unmap_memory(state, command, arg);
	case 0x5e:
		if (size != sizeof(struct gpu_nvos56))
			return -EINVAL;
		return gpu_rm_update_mapping(state, command, arg);
	default:
		/* Only the owner-audited card/status queries (200, 210)
		 * pass.  SYS_PARAMS (214), NUMA status (216), dma-buf
		 * export (217), interrupt query (213), XFER (211),
		 * wait-open (218), ATTACH_GPUS (212) and VID_HEAP (0x4a)
		 * stay closed. */
		if (nr != 200 && nr != 210)
			return -EOPNOTSUPP;
		if (!size || size > UM_KERN_PAGE_SIZE)
			return -EOPNOTSUPP;
		if (fault_in_readable((void __user *)arg, size) ||
		    fault_in_safe_writeable((void __user *)arg, size))
			return -EFAULT;
		{
			long result;

			result = um_tlb_sync(state->mm);
			if (result)
				return result;
			mutex_lock(&state->mm->context.gpu_lock);
			result = gpu_command(state->mm, GPU_OP_IOCTL,
					     state->host_fd, command,
					     arg, NULL, 0);
			mutex_unlock(&state->mm->context.gpu_lock);
			return result;
		}
	}
}

/* ---- mmap / mprotect / poll ------------------------------------------- */

static const struct vm_operations_struct gpu_vm_ops;

static void gpu_vma_close(struct vm_area_struct *vma)
{
	struct gpu_vma *gv = vma->vm_private_data;
	struct mm_struct *mm;
	bool last;

	if (!gv)
		return;
	/* Detach first so a split sibling's close never acts twice through
	 * the same VMA, then tear down THIS fragment's host mapping even if
	 * a split sibling keeps the record alive. */
	vma->vm_private_data = NULL;
	mm = gv->mm;
	last = refcount_dec_and_test(&gv->refs);
	if (mm->context.gpu_id.stack &&
	    READ_ONCE(mm->context.gpu_id.pid) > 1) {
		mutex_lock(&mm->context.gpu_lock);
		os_gpu_munmap(&mm->context.gpu_id, vma->vm_start,
			      vma->vm_end - vma->vm_start);
		mutex_unlock(&mm->context.gpu_lock);
	}
	/* The record, mm reference and free happen exactly once, when the
	 * last split fragment closes. */
	if (last) {
		mmdrop(mm);
		kfree(gv);
	}
}

static void gpu_vma_open(struct vm_area_struct *vma)
{
	struct gpu_vma *gv = vma->vm_private_data;

	/* __split_vma duplicated vm_private_data: share one record with an
	 * extra ref; each fragment gets its own close()-driven host unmap. */
	if (gv)
		refcount_inc(&gv->refs);
}

static vm_fault_t gpu_vma_fault(struct vm_fault *vmf)
{
	/* The host-only mapping is never installed in guest page tables:
	 * guest kernel access (process_vm_readv, ptrace, core dumps) cannot
	 * reach it and gets SIGBUS rather than silently reading the wrong
	 * page.  See REPORT.md for the native-semantics gap. */
	return VM_FAULT_SIGBUS;
}

static int gpu_vma_mremap(struct vm_area_struct *vma)
{
	/* RM mappings are address-keyed and UVM requires offset==VA: a
	 * transparent move can never be revalidated, so refuse it.  The
	 * caller rolls the page-table move back and keeps the old VMA. */
	return -EINVAL;
}

static int gpu_vma_mprotect(struct vm_area_struct *vma, unsigned long start,
			    unsigned long end, unsigned long newflags)
{
	struct gpu_vma *gv = vma->vm_private_data;
	int prot;
	long error;

	if (!gv)
		return -EINVAL;
	if (newflags & VM_EXEC)
		return -EACCES;
	prot = (newflags & VM_READ ? UM_PROT_READ : 0) |
	       (newflags & VM_WRITE ? UM_PROT_WRITE : 0);
	mutex_lock(&gv->mm->context.gpu_lock);
	error = os_gpu_mprotect(&gv->mm->context.gpu_id, start,
				end - start, prot);
	mutex_unlock(&gv->mm->context.gpu_lock);
	return error;
}

static const struct vm_operations_struct gpu_vm_ops = {
	.open = gpu_vma_open,
	.close = gpu_vma_close,
	.fault = gpu_vma_fault,
	.mremap = gpu_vma_mremap,
	.mprotect = gpu_vma_mprotect,
};

static int gpu_mmap(struct file *file, struct vm_area_struct *vma)
{
	struct gpu_file *state = file->private_data;
	struct mm_struct *mm = state->mm;
	struct gpu_vma *gv;
	int prot, map_flags, error;

	if (current->mm != mm ||
	    task_tgid(current) != mm->context.gpu_owner)
		return -EXDEV;
	if (vma->vm_flags & VM_EXEC)
		return -EINVAL;
	gv = kzalloc(sizeof(*gv), GFP_KERNEL);
	if (!gv)
		return -ENOMEM;
	gv->mm = mm;
	refcount_set(&gv->refs, 1);
	mmgrab(mm);

	prot = (vma->vm_flags & VM_READ ? UM_PROT_READ : 0) |
	       (vma->vm_flags & VM_WRITE ? UM_PROT_WRITE : 0);
	map_flags = (vma->vm_flags & VM_SHARED) ? MAP_SHARED : MAP_PRIVATE;

	error = um_tlb_sync(mm);
	if (error)
		goto err;
	mutex_lock(&mm->context.gpu_lock);
	error = os_gpu_mmap(&mm->context.gpu_id, state->host_fd,
			    vma->vm_start, vma->vm_end - vma->vm_start,
			    prot, map_flags, vma->vm_pgoff << PAGE_SHIFT);
	mutex_unlock(&mm->context.gpu_lock);
	if (error < 0)
		goto err;

	if (state->index != GPU_KIND_CTL && state->index != GPU_KIND_UVM)
		vm_flags_set(vma, VM_PFNMAP);
	vm_flags_set(vma, VM_IO | VM_DONTEXPAND | VM_DONTDUMP | VM_DONTCOPY);
	vma->vm_ops = &gpu_vm_ops;
	vma->vm_private_data = gv;
	return 0;

err:
	mmdrop(mm);
	kfree(gv);
	return error;
}

static unsigned int gpu_poll(struct file *file, poll_table *wait)
{
	struct gpu_file *state = file->private_data;
	struct mm_struct *mm = state->mm;
	unsigned int mask;

	if (current->mm != mm ||
	    task_tgid(current) != mm->context.gpu_owner)
		return POLLERR;
	poll_wait(file, &state->waitq, wait);
	mutex_lock(&mm->context.gpu_lock);
	/* Device relay slots are allocated lazily: only files actually
	 * polled consume one, so the 192-slot table cannot be exhausted by
	 * the transient nvidiactl opens alone. */
	if (state->slot < 0 && state->index != GPU_KIND_UVM &&
	    READ_ONCE(mm->context.gpu_id.pid) > 1) {
		int slot = gpu_slot_alloc(mm);

		if (slot >= 0) {
			gpu_slot_publish(mm, slot, state->host_fd,
					UML_GPU_RELAY_KIND_DEVICE,
					&state->slot_gen);
			state->slot = slot;
			state->last_level = 0;
		} else if (gpu_slots_all_active(mm)) {
			/* Permanent exhaustion: every slot ACTIVE.  Report an
			 * explicit error rather than polling forever with a
			 * zero mask; CLOSING-only state is transient and
			 * returns the current mask for a later retry. */
			mutex_unlock(&mm->context.gpu_lock);
			pr_err_ratelimited("harddind-gpu: relay slot table "
					   "exhausted (%d entries)\n",
					   UML_GPU_RELAY_MAX_ENTRIES);
			return POLLERR;
		}
	}
	mask = atomic_read(&state->poll_mask);
	mutex_unlock(&mm->context.gpu_lock);
	return mask;
}

static const struct file_operations gpu_fops = {
	.owner = THIS_MODULE,
	.open = gpu_open,
	.release = gpu_release,
	.unlocked_ioctl = gpu_ioctl,
	.mmap = gpu_mmap,
	.poll = gpu_poll,
};

/* ---- device nodes ------------------------------------------------------ */

static char *gpu_devnode(const struct device *dev, umode_t *mode)
{
	if (mode)
		*mode = 0600;
	return NULL;
}

/* Canonical driver node name at major 195 (or NULL to skip the index). */
static const char *gpu_std_name(unsigned int index)
{
	if (index == GPU_KIND_CTL)
		return "nvidiactl";
	if (index == GPU_KIND_UVM)
		return "nvidia-uvm";
	if (index <= 254)
		return NULL; /* caller formats "nvidia%u" */
	return NULL;
}

/* Historical compatibility name (separate dynamic dev_t range). */
static const char *gpu_alias_name(unsigned int index, char *buf, size_t n)
{
	if (index == GPU_KIND_CTL)
		return "harddind-nvidiactl";
	if (index == GPU_KIND_UVM)
		return "harddind-nvidia-uvm";
	if (index <= 254) {
		snprintf(buf, n, "harddind-nvidia%u", index);
		return buf;
	}
	return NULL;
}

/* Tear down one index in exact reverse registration order; safe to call
 * for a partially registered device. */
static void gpu_device_unwind(struct gpu_device *d)
{
	if (d->alias) {
		device_unregister(d->alias);
		d->alias = NULL;
	}
	if (d->compat_added) {
		cdev_del(&d->compat.cdev);
		d->compat_added = false;
	}
	if (d->device) {
		device_unregister(d->device);
		d->device = NULL;
	}
	if (d->canon_added) {
		cdev_del(&d->canon.cdev);
		d->canon_added = false;
	}
}

static int gpu_register_device(unsigned int index)
{
	struct gpu_device *d = &gpu_devices[index];
	char nbuf[16], abuf[32];
	const char *canon, *compat;
	dev_t cnum = MKDEV(GPU_MAJOR, index);
	dev_t anum = MKDEV(MAJOR(gpu_compat_first), MINOR(gpu_compat_first)
			  + index);
	struct device *dev;

	canon = gpu_std_name(index);
	if (!canon && index <= 254) {
		snprintf(nbuf, sizeof(nbuf), "nvidia%u", index);
		canon = nbuf;
	}
	compat = gpu_alias_name(index, abuf, sizeof(abuf));
	if (!canon || !compat)
		return -EINVAL;

	d->index = index;
	d->canon.index = index;
	d->compat.index = index;

	/* Canonical node at 195:index. */
	cdev_init(&d->canon.cdev, &gpu_fops);
	d->canon.cdev.owner = THIS_MODULE;
	if (cdev_add(&d->canon.cdev, cnum, 1))
		return -EINVAL;
	d->canon_added = true;
	dev = device_create(gpu_class, NULL, cnum, &d->canon, "%s", canon);
	if (IS_ERR(dev)) {
		gpu_device_unwind(d);
		return PTR_ERR(dev);
	}
	d->device = dev;

	/* Compatibility node at the dynamic range: distinct cdev/dev_t so
	 * the sysfs /dev/char links never collide. */
	cdev_init(&d->compat.cdev, &gpu_fops);
	d->compat.cdev.owner = THIS_MODULE;
	if (cdev_add(&d->compat.cdev, anum, 1)) {
		gpu_device_unwind(d);
		return -EINVAL;
	}
	d->compat_added = true;
	dev = device_create(gpu_class, NULL, anum, &d->compat, "%s", compat);
	if (IS_ERR(dev)) {
		gpu_device_unwind(d);
		return PTR_ERR(dev);
	}
	d->alias = dev;
	return 0;
}

static int __init gpu_init(void)
{
	unsigned int done[GPU_INDEX_MAX + 1];
	unsigned int index, ndone = 0;
	int error;
	dev_t first = MKDEV(GPU_MAJOR, 0);

	if (!uml_gpu_bridge)
		return 0;
	if (!parallel_userspace)
		return -EOPNOTSUPP;
	error = register_chrdev_region(first, ARRAY_SIZE(gpu_devices),
				      "nvidia-frontend");
	if (error)
		return error;
	/* Dedicated dynamic range for the compatibility names. */
	error = alloc_chrdev_region(&gpu_compat_first, 0,
				   ARRAY_SIZE(gpu_devices), "harddind-nvidia");
	if (error)
		goto unregister_canon_region;
	gpu_class = class_create("harddind-nvidia");
	if (IS_ERR(gpu_class)) {
		error = PTR_ERR(gpu_class);
		goto unregister_compat_region;
	}
	gpu_class->devnode = gpu_devnode;
	for (index = 0; index <= GPU_INDEX_MAX; index++) {
		if (!os_gpu_device_present(index))
			continue;
		error = gpu_register_device(index);
		if (error) {
			/* Reverse-order teardown of every completed index,
			 * then the partial failing one. */
			while (ndone--)
				gpu_device_unwind(&gpu_devices[done[ndone]]);
			gpu_device_unwind(&gpu_devices[index]);
			class_destroy(gpu_class);
			gpu_class = NULL;
			goto unregister_compat_region;
		}
		done[ndone++] = index;
	}
	return 0;

unregister_compat_region:
	unregister_chrdev_region(gpu_compat_first, ARRAY_SIZE(gpu_devices));
unregister_canon_region:
	unregister_chrdev_region(first, ARRAY_SIZE(gpu_devices));
	return error;
}

device_initcall(gpu_init);
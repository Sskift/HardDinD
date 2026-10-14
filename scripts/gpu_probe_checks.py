"""Expected diagnostic checks, separate from CUDA/full GPU acceptance."""
FD_CHECKS = {
    'pipe_read_from_protnone', 'pipe_write_to_readonly', 'pipe_recovery_preserves_data',
    'open_control_and_gpu', 'invalid_source_fd', 'non_device_source_fd',
    'gpu_source_rejected', 'control_self_reference', 'high_guest_fd',
    'high_fd_registration', 'duplicate_registration', 'source_close',
    'target_retains_registration', 'close_registered_target',
    'all_gpu_devices_registration', 'control_target_registration',
    'linked_control_as_source_rejected', 'copyin_fault',
    'copyin_fault_does_not_register', 'copyout_fault',
    'copyout_fault_keeps_registration', 'four_threads_shared_control',
    'reopen_lifecycle', 'independent_process_lifecycle',
}

# NV_ESC_RM_ALLOC/NV_ESC_RM_FREE root client lifecycle checks, emitted as
# GPU_RM_CHECK lines by src/gpu_rm_probe.c. Still diagnostic, not CUDA.
RM_CHECKS = {
    'open_control_and_gpu', 'register_gpu_control',
    'duplicate_registration_rejected',
    'alloc64_root_client', 'free_root_client',
    'alloc21_root_client', 'alloc21_free',
    'alloc64_nonpriv_client', 'alloc64_nonpriv_free',
    'independent_clients_distinct', 'independent_clients_freed',
    'invalid_class_status', 'double_free_rejected',
    'free_null_client_rejected', 'free_bogus_client_rejected',
    'free_unknown_object_compat',
    'cross_fd_free_rejected', 'cross_fd_owner_free',
    'alloc_on_gpu_device_rejected', 'free_on_gpu_device_rejected',
    'alloc_wrong_size_rejected', 'free_wrong_size_rejected',
    'alloc_unreadable_buffer', 'recovery_after_fault',
    'concurrent_client_handles_distinct', 'four_threads_shared_control',
    'reopen_lifecycle', 'close_releases_client',
    'independent_process_lifecycle',
}

CTRL_CHECKS = {
    'open_control', 'allocate_client', 'attached_ids', 'invalid_gpu_rejected',
    'invalid_client_rejected', 'nested_cross_page', 'nested_unreadable_rejected',
    'nested_readonly_rejected', 'header_readonly_rejected', 'recovery_after_faults',
    'four_threads_same_client', 'independent_process_queries', 'release_client',
}

CTRL_CHECKS |= {'nested_null', 'nested_low_unmapped', 'nested_wrap',
                'nested_noncanonical', 'nested_unmapped', 'nested_noncanonical_invalid_client'}

# Legacy build-version child and leaf pointers, including native partial writes.
BUILD_CHECKS = {
    'open_control', 'allocate_client', 'size_query', 'full', 'too_small',
    'partial_null', 'leaf_readonly', 'leaf_unreadable', 'leaf_low_unmapped',
    'leaf_noncanonical', 'leaf_wrap', 'leaf_cross_page', 'leaf_cross_none',
    'child_readonly', 'child_readonly_size_query', 'child_unreadable',
    'child_low_unmapped', 'child_null', 'child_noncanonical',
    'invalid_client_bad_child', 'child_cross_page', 'child_cross_readonly',
    'header_readonly', 'header_unreadable', 'size_short', 'size_long',
    'recovery_after_faults', 'four_threads_same_client',
    'independent_process_queries', 'release_client',
}

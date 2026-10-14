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

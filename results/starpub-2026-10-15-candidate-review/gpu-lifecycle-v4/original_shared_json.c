#define main lifecycle_main
#include "/root/harddind-rebuild/gpu-lifecycle-owner-review-v4/src/cuda_lifecycle_probe.c"
#undef main
int main(void) {
    struct worker_result w = {0};
    w.current_match = 1; emit_thread_worker("shared", &w);
    w.current_match = 0; emit_thread_worker("shared", &w);
    return 0;
}

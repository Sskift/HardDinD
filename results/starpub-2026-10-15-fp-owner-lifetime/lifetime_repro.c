#define main fp_original_model_main
#include "fp_model.c"
#undef main
int main(void) {
    struct runner runner = {0};
    struct desc *old = calloc(1, sizeof(*old));
    struct desc *next = calloc(1, sizeof(*next));
    if (!old || !next) return 2;
    desc_reset(old); desc_reset(next);
    if (fp_enter(old, &runner) != 1) return 3;
    fp_exit(old);
    free(old);
    /* Same mm still has another live task; its runner may be reused. */
    int result = fp_leave(next, &runner);
    free(next);
    return result < 0;
}

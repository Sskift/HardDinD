/* Cold sites, lazy trampoline allocation, fork inheritance and fresh exec. */
#define _GNU_SOURCE
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <unistd.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "core hot line=%d %s errno=%d\n", \
    __LINE__, #x, errno); exit(1); } } while (0)
extern unsigned char core_hot_site[];
extern long core_hot_call(void);
__asm__(".text\n.global core_hot_call,core_hot_site\n.rept 14; nop; .endr\n"
        "core_hot_call:\ncore_hot_site: mov $39,%eax; syscall; ret\n");
static const unsigned char original[] = {0xb8, 39, 0, 0, 0, 0x0f, 0x05, 0xc3};
static void wait_ok(pid_t child) {
    int status;
    CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
}
static int mapped(void) {
    unsigned char vec;
    int result = mincore((void *)0x06060000UL, 4096, &vec);
    CHECK(!result || errno == ENOMEM);
    return !result;
}
int main(int argc, char **argv) {
    unsigned int hot = 0, enabled = 0;
    FILE *f = fopen("/proc/uml_syscall_patch", "r");
    if (f) {
        char key[64]; unsigned long long value;
        while (fscanf(f, "%63s %llu", key, &value) == 2) {
            if (!strcmp(key, "enabled")) { CHECK(value <= 1); enabled = (unsigned int)value; }
            if (!strcmp(key, "hot_threshold")) { CHECK(value <= 65535); hot = (unsigned int)value; }
        }
        CHECK(feof(f) && !ferror(f) && !fclose(f));
    } else CHECK(errno == ENOENT);
    CHECK(!memcmp(core_hot_site, original, sizeof(original)));
    if (enabled && hot > 1) CHECK(!mapped());
    if (argc == 2) { CHECK(!strcmp(argv[1], "fresh")); return 0; }
    CHECK(argc == 1);
    long pid = getpid();
    unsigned int threshold = enabled ? (hot ? hot : 1) : 64;
    for (unsigned int n = 1; n < threshold; n++) {
        CHECK(core_hot_call() == pid);
        CHECK(!memcmp(core_hot_site, original, sizeof(original)));
    }
    if (enabled && hot > 1) CHECK(!mapped());
    /* mincore may evict a colliding hint; allow another complete window. */
    for (unsigned int n = 0; n < threshold; n++) CHECK(core_hot_call() == pid);
    CHECK((core_hot_site[0] == 0xff) == (int)enabled);
    if (enabled) CHECK(mapped());
    pid_t child = fork(); CHECK(child >= 0);
    if (!child) {
        CHECK(core_hot_call() == getpid());
        CHECK((core_hot_site[0] == 0xff) == (int)enabled);
        unsigned char *text = mmap(NULL, 4096, PROT_READ | PROT_WRITE,
                                   MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
        CHECK(text != MAP_FAILED);
        memset(text, 0x90, 32); memcpy(text + 32, original, sizeof(original));
        CHECK(!mprotect(text, 4096, PROT_READ | PROT_EXEC));
        long (*call)(void) = (void *)(text + 32);
        long expected = getpid();
        for (unsigned int n = 0; n < threshold * 2; n++) CHECK(call() == expected);
        if (hot || !enabled) CHECK(!memcmp(text + 32, original, sizeof(original)));
        CHECK(!munmap(text, 4096)); _exit(0);
    }
    wait_ok(child);
    child = fork(); CHECK(child >= 0);
    if (!child) { execl("/proc/self/exe", "core_hot", "fresh", (char *)NULL); _exit(127); }
    wait_ok(child);
    printf("{\"schema\":\"hd.core-hot/1\",\"enabled\":%s,\"threshold\":%u,"
           "\"cold_calls\":%u,\"lazy\":%s,\"fork\":true,\"exec_clean\":true,\"verified\":true}\n",
           enabled ? "true" : "false", hot, threshold - 1, enabled && hot > 1 ? "true" : "false");
    return 0;
}

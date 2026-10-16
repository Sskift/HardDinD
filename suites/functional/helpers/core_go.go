package main

import (
    "debug/elf"
    "encoding/json"
    "fmt"
    "os"
    "os/exec"
    "os/signal"
    "runtime"
    "sync"
    "syscall"
    "unsafe"
)

func check(ok bool) {
    if !ok { panic("core Go syscall or signal mismatch") }
}

func main() {
    expected := uintptr(os.Getpid())
    if len(os.Args) == 2 && os.Args[1] == "child" {
        value, _, errno := syscall.RawSyscall(syscall.SYS_GETPID, 0, 0, 0)
        check(errno == 0 && value == expected)
        fmt.Println("child-ok")
        return
    }
    runtime.GOMAXPROCS(4)
    signals := make(chan os.Signal, 1)
    signal.Notify(signals, syscall.SIGUSR1)
    var workers sync.WaitGroup
    for n := 0; n < 4; n++ {
        workers.Add(1)
        go func() {
            defer workers.Done()
            runtime.LockOSThread()
            defer runtime.UnlockOSThread()
            for i := 0; i < 4096; i++ {
                value, _, errno := syscall.RawSyscall(syscall.SYS_GETPID, 0, 0, 0)
                check(errno == 0 && value == expected)
            }
        }()
    }
    check(syscall.Kill(int(expected), syscall.SIGUSR1) == nil)
    check(<-signals == syscall.SIGUSR1)
    workers.Wait()
    for n := 0; n < 4; n++ {
        output, err := exec.Command("/proc/self/exe", "child").CombinedOutput()
        check(err == nil && string(output) == "child-ok\n")
    }
    signal.Stop(signals)
    enabled := false
    if file, err := os.Open("/proc/uml_syscall_patch"); err == nil {
        var key string
        var value int
        _, err = fmt.Fscan(file, &key, &value)
        check(err == nil && key == "enabled" && (value == 0 || value == 1))
        enabled = value == 1
        check(file.Close() == nil)
    } else { check(os.IsNotExist(err)) }
    binary, err := elf.Open("/proc/self/exe")
    check(err == nil && binary.Type == elf.ET_EXEC)
    symbols, err := binary.Symbols()
    check(err == nil)
    found := false
    for _, symbol := range symbols {
        if symbol.Name != "internal/runtime/syscall/linux.Syscall6" { continue }
        code := unsafe.Slice((*byte)(unsafe.Pointer(uintptr(symbol.Value))), 14)
        if enabled {
            check(code[0] == 0xff && code[1] == 0x24 && code[2] == 0x25 &&
                  code[3] == 6 && code[6] == 6 && code[9] == 6 && code[12] == 6)
        } else { check(code[0] == 0x49 && code[12] == 0x0f && code[13] == 0x05) }
        found = true
    }
    check(found && binary.Close() == nil)
    check(json.NewEncoder(os.Stdout).Encode(map[string]any{
        "schema": "hd.core-go/1", "calls": 16384, "children": 4,
        "signal": true, "patch_enabled": enabled, "verified": true,
    }) == nil)
}

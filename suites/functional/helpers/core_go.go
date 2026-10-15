package main

import (
    "encoding/json"
    "fmt"
    "os"
    "os/exec"
    "os/signal"
    "runtime"
    "sync"
    "syscall"
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
    check(json.NewEncoder(os.Stdout).Encode(map[string]any{
        "schema": "hd.core-go/1", "calls": 16384, "children": 4,
        "signal": true, "verified": true,
    }) == nil)
}

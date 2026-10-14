// Port-control HTTP transport for the Docker guest. The Docker API is unchanged.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

type request struct {
	ID     uint64          `json:"id"`
	Method string          `json:"method"`
	Path   string          `json:"path"`
	Body   json.RawMessage `json:"body,omitempty"`
}
type response struct {
	ID     uint64          `json:"id"`
	Status int             `json:"status"`
	Body   json.RawMessage `json:"body"`
}

func fatal(err error) { fmt.Fprintln(os.Stderr, err); os.Exit(1) }
func raw(fd uintptr) error {
	var term syscall.Termios
	if _, _, err := syscall.Syscall(syscall.SYS_IOCTL, fd, syscall.TCGETS, uintptr(unsafe.Pointer(&term))); err != 0 {
		return err
	}
	term.Iflag = 0
	term.Oflag = 0
	term.Lflag = 0
	term.Cflag = syscall.CLOCAL | syscall.CREAD | syscall.CS8 | syscall.B115200
	term.Cc[syscall.VMIN] = 1
	term.Cc[syscall.VTIME] = 0
	if _, _, err := syscall.Syscall(syscall.SYS_IOCTL, fd, syscall.TCSETS, uintptr(unsafe.Pointer(&term))); err != 0 {
		return err
	}
	return nil
}
func main() {
	if len(os.Args) != 3 {
		fatal(fmt.Errorf("usage: port-agent SERIAL UNIX_SOCKET"))
	}
	link, err := os.OpenFile(os.Args[1], os.O_RDWR, 0)
	if err != nil {
		fatal(err)
	}
	if err := raw(link.Fd()); err != nil {
		fatal(err)
	}
	responses := make(chan response, 1)
	go func() {
		scanner := bufio.NewScanner(link)
		scanner.Buffer(make([]byte, 4096), 131072)
		for scanner.Scan() {
			var result response
			if err := json.Unmarshal(scanner.Bytes(), &result); err != nil {
				fatal(err)
			}
			responses <- result
		}
		fatal(fmt.Errorf("port-control link closed: %v", scanner.Err()))
	}()
	var mu sync.Mutex
	var next uint64
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, 65537))
		if err != nil || len(body) > 65536 || (len(body) != 0 && !json.Valid(body)) {
			http.Error(w, "invalid request body", 400)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		next++
		message := request{ID: next, Method: r.Method, Path: r.URL.Path, Body: body}
		if err := json.NewEncoder(link).Encode(message); err != nil {
			fatal(err)
		}
		select {
		case result := <-responses:
			if result.ID != next || result.Status < 100 || result.Status > 599 {
				fatal(fmt.Errorf("invalid port-control response"))
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(result.Status)
			_, _ = w.Write(result.Body)
		case <-time.After(15 * time.Second):
			// A timed-out serial transaction cannot be safely reused by a later request.
			fatal(fmt.Errorf("port-control request timed out"))
		}
	})
	listener, err := net.Listen("unix", os.Args[2])
	if err != nil {
		fatal(err)
	}
	if err := os.Chmod(os.Args[2], 0600); err != nil {
		fatal(err)
	}
	fmt.Println("HARDDIND_PORT_AGENT_READY")
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 20 * time.Second, WriteTimeout: 20 * time.Second}
	fatal(server.Serve(listener))
}

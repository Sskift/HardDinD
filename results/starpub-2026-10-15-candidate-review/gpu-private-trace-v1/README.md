# Complete native private-control capture

The owner extended the previously used ioctl logger to capture full payloads
only for the exact observed command/size pairs: `0x20808159`/332,
`0x20810108`/992 and `0x20808162`/1. Other nested captures remain capped at
64 bytes. Both snapshots use the original pointer; the real syscall and its
errno are preserved. One bounded stderr line retains both complete buffers
and the before/after NVOS54 headers, without auxiliary `/tmp` files.

One native CUDA run computed and validated 1,048,576 elements on each of four
GPUs, including host/device transfers and cleanup. Each of the three target
commands occurred four times, with full before/after lengths, syscall rc 0
and embedded RM status 0. The 332-byte packet contains the observed integer
values 2, 12 and 37 followed by zeros; its bytes remain unchanged in these
captures. That observation does **not** establish the undocumented structure
or exclude pointers for other inputs. Successful native dispatch alone also
does not prove a particular GSP route.

Logger source: `24831bed263a050b05a4874eb293a80ea88108a80a6b82e193f7135c46452976`.
Logger binary: `1c1206c17effa9cc686773bdfbc0f9c25e823db157f23d3ba19a8a4209084653`.
Sources, CUDA probe binary and all recorded libraries stayed unchanged. Both
command groups were empty at completion. `python3 recheck.py` verifies the
archive and every full capture. This run is native diagnostic evidence;
logged timings are not a performance measurement or internal GPU acceptance.

FROM hd-base:latest AS compile
ARG CACHE_ID
ARG MARKER
RUN --mount=type=cache,id=${CACHE_ID},target=/cache n=$(cat /cache/n 2>/dev/null || echo 0); echo $((n+1)) >/cache/n; cp /cache/n /count; printf '%s\n' "$MARKER" >/artifact; cat /proc/sys/kernel/random/uuid >/nonce
FROM hd-base:latest
COPY --from=compile /count /artifact /nonce /
CMD ["cat", "/artifact"]

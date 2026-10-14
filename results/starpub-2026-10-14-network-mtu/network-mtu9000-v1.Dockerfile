FROM public.ecr.aws/docker/library/alpine@sha256:d56c381f961d307a21b3ca004cf1e3910f106644aefb1f43e654c8a56c4fd395
RUN wget -q -T 30 -O /tmp/alpine.tar.gz https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/x86_64/alpine-minirootfs-3.24.2-x86_64.tar.gz && echo 'c5ca053cfe1d85c5b96dff8b9bc57045f7f184a30ffb6b65776409ca90388677  /tmp/alpine.tar.gz' | sha256sum -c - && echo harddind-build-ok > /built
CMD ["cat", "/built"]

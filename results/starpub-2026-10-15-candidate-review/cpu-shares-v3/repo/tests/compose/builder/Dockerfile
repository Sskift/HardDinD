# Offline BuildKit fixture: the base is the pinned local image and the build
# performs no network access. Runner invokes `compose build` with pull off.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN printf 'harddind-compose-built\n' > /built

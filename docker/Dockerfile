ARG BUILD
ARG BUILD_DATE
ARG GITHUB_SHA

FROM python:3.13-slim-bookworm AS base

FROM base AS builder
ARG BUILD_DATE

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        gcc \
        libffi-dev \
        tar \
        xz-utils \
        ca-certificates; \
    update-ca-certificates; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

COPY /app/ /build/app/
RUN chmod +x /build/app/run

RUN --mount=type=cache,target=/root/.cache/pip \
    set -eux; \
    pip3 install --disable-pip-version-check --prefix=/build/usr/local -r /build/app/requirements.txt

RUN set -eux; \
    echo "BUILDING IMAGE FOR $(uname -m)"; \
    if [ "$(uname -m)" = "armv7l" ]; then \
        TARGETARCH=arm; FFMPEG_ARCH=arm32; MTX_ARCH=armv7; \
    elif [ "$(uname -m)" = "aarch64" ]; then \
        TARGETARCH=arm64; FFMPEG_ARCH=arm64; MTX_ARCH=arm64; \
    else \
        TARGETARCH=amd64; FFMPEG_ARCH=linux64; MTX_ARCH=amd64; \
    fi; \
    cd /build; \
    . app/.env; \
    # make sure these exist before extracting/copying
    mkdir -p tokens usr/local usr/local/bin usr/local/lib; \
    # FFmpeg (static builds that work on Debian)
    # options: linux64, linuxarm64, linuxarm32
    curl -SL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n6.1-latest-${FFMPEG_ARCH}-lgpl-6.1.tar.xz -o ffmpeg.tar.xz; \
    tar -Jxf ffmpeg.tar.xz --strip-components=1 -C usr/local --wildcards 'bin/ffmpeg' 'bin/ffprobe'; \
    mv usr/local/bin/ffmpeg usr/local/bin/ffmpeg && chmod +x usr/local/bin/ffmpeg; \
    mv usr/local/bin/ffprobe usr/local/bin/ffprobe && chmod +x usr/local/bin/ffprobe; \
    rm -f ffmpeg.tar.xz; \
    # MediaMTX
    curl -fsSL https://github.com/bluenviron/mediamtx/releases/download/v${MTX_TAG}/mediamtx_v${MTX_TAG}_linux_${MTX_ARCH}.tar.gz -o mediamtx.tar.gz; \
    tar tzf mediamtx.tar.gz >/dev/null || (echo "Invalid mediamtx tarball for ${MTX_ARCH}" && exit 1); \
    tar xzf mediamtx.tar.gz -C app; \
    rm -f mediamtx.tar.gz; \
    # TUTK lib
    cp app/lib/lib.${TARGETARCH} usr/local/lib/libIOTCAPIs_ALL.so; \
    # cleanup
    rm -rf app/*.txt app/lib/; \
    if [ -z "${BUILD_DATE}" ]; then echo BUILD_DATE=$(date) > .build_date; else echo BUILD_DATE=${BUILD_DATE} > .build_date; fi;

FROM base
ARG BUILD
ARG GITHUB_SHA
ARG BUILD_DATE
ARG BUILD_VERSION

RUN set -eux; \
    rm -f /var/log/*.log; \
    rm -rf /var/log/apt;

COPY --from=builder /build /

ENV PYTHONUNBUFFERED=1 \
    FLASK_APP=frontend \
    BUILD=$BUILD \
    BUILD_VERSION=$BUILD_VERSION \
    BUILD_DATE=$BUILD_DATE \
    GITHUB_SHA=$GITHUB_SHA \
    FLASK_ENV=production \
    FLASK_DEBUG=0

WORKDIR /app
ENTRYPOINT [ "/app/run" ]

LABEL \
  io.hass.name="Docker Wyze Bridge for Home Assistant" \
  io.hass.description="Bridges Wyze Cameras into Home Assistant using MediaMTX in Docker container" \
  io.hass.type="addon" \
  io.hass.version=${BUILD_VERSION} \
  maintainer="Marc Brooks <idisposable@gmail.com>" \
  org.label-schema.name="Docker Wyze Bridge HA" \
  org.label-schema.description="Docker Wyze Bridge for Home Assistant" \
  org.label-schema.build-date=${BUILD_DATE} \
  org.label-schema.schema-version="1.0" \
  org.label-schema.usage="https://github.com/IDisposable/docker-wyze-bridge/blob/main/README.md" \
  org.label-schema.vcs-ref=${GITHUB_SHA} \
  org.label-schema.vcs-url="https://github.com/IDisposable/docker-wyze-bridge/" \
  org.label-schema.vendor="HomeAssistant add-ons by Marc Brooks"

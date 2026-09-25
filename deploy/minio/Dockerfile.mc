FROM alpine:3.20

RUN apk add --no-cache ca-certificates curl

ARG MC_VERSION=RELEASE.2025-04-16T18-13-26Z
ARG MC_SHA256=ac90da87a35641be5a0ac75d49de5161ddb47d629b5ba01261b0ae9e00aea15f

RUN curl -fsSL     "https://github.com/minio/mc/releases/download/${MC_VERSION}/mc.linux-amd64.${MC_VERSION}"     -o /usr/local/bin/mc     && echo "${MC_SHA256}  /usr/local/bin/mc" | sha256sum -c -     && chmod +x /usr/local/bin/mc

ENTRYPOINT ["/usr/local/bin/mc"]

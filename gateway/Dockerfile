# syntax=docker/dockerfile:1
#
# MAOps Kubernetes Platform - Day 2 "gateway" workload container.
#
# Requirements:
#   - deterministic/pinned base image (digest-pinned distroless, no package manager)
#   - runs as non-root UID:GID 10001:10001
#   - binds to 8080
#   - zero third-party Python dependencies, zero package installation at build time
#
# Base image: gcr.io/distroless/python3-debian12:nonroot
# Pinned by digest so the build is reproducible regardless of tag drift.
# Same digest as the app workload/Day 1 - the DAY1-INT-I2 interpreter-path
# coupling (/usr/bin/python3.11) remains accepted/open, unchanged.
FROM gcr.io/distroless/python3-debian12@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5

WORKDIR /app
COPY server.py /app/server.py

USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["/usr/bin/python3.11", "/app/server.py"]

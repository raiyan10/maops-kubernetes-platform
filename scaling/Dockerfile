# syntax=docker/dockerfile:1
#
# MAOps Kubernetes Platform - Day 8 disposable "scaling" image (HPA
# target, load generator, VPA target, KEDA queue worker and producer -
# see scaling.py). Used only in the temporary maops-day8-scaling
# namespace; never part of the application release.
#
# Same contract as the three workload images: the same digest-pinned
# distroless base, non-root UID:GID 10001:10001, port 8080, zero
# third-party Python dependencies, no package installation.
FROM gcr.io/distroless/python3-debian12@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5

WORKDIR /scaling
COPY scaling.py /scaling/scaling.py

USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["/usr/bin/python3.11", "/scaling/scaling.py"]

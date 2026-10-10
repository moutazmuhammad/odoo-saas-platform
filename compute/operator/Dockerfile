# Build stage.
FROM --platform=$BUILDPLATFORM golang:1.27-bookworm AS builder
ARG TARGETOS
ARG TARGETARCH
WORKDIR /workspace

COPY go.mod go.sum ./
RUN go mod download

COPY api/ api/
COPY cmd/ cmd/
COPY internal/ internal/

# CGO disabled + static binary: the runtime image below has no libc tooling
# to link against, and a static binary is what makes distroless viable.
# Run the Go compiler on the builder's CPU, cross-compile for the target.
# Neither architecture needs QEMU: the runtime stage has no RUN steps.
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -a -o /workspace/manager ./cmd

# Runtime stage: distroless nonroot — no shell, no package manager, minimal
# attack surface for a cluster-privileged controller.
FROM gcr.io/distroless/static:nonroot
WORKDIR /
COPY --from=builder /workspace/manager .
USER 65532:65532

ENTRYPOINT ["/manager"]

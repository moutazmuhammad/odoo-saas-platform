# Build stage.
FROM golang:1.27-bookworm AS builder
WORKDIR /workspace

COPY go.mod go.sum ./
RUN go mod download

COPY api/ api/
COPY cmd/ cmd/
COPY internal/ internal/

# CGO disabled + static binary: the runtime image below has no libc tooling
# to link against, and a static binary is what makes distroless viable.
RUN CGO_ENABLED=0 GOOS=linux GOARCH=$(go env GOARCH) go build -a -o /workspace/manager ./cmd

# Runtime stage: distroless nonroot — no shell, no package manager, minimal
# attack surface for a cluster-privileged controller.
FROM gcr.io/distroless/static:nonroot
WORKDIR /
COPY --from=builder /workspace/manager .
USER 65532:65532

ENTRYPOINT ["/manager"]

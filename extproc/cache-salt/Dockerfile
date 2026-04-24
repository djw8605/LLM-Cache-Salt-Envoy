# ---------------------------------------------------------------------------
# Stage 1 – generate gRPC Python stubs from the vendored proto files
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS proto-gen

WORKDIR /gen

COPY requirements.txt .
# Install grpcio-tools (only needed during generation, not at runtime).
RUN pip install --no-cache-dir grpcio-tools==1.80.0 protobuf==6.33.6

COPY proto/ proto/

RUN python -m grpc_tools.protoc \
        -I proto \
        --python_out=/gen \
        --grpc_python_out=/gen \
        proto/envoy/config/core/v3/base.proto \
        proto/envoy/service/ext_proc/v3/external_processor.proto

# Create __init__.py files so the generated packages are importable.
RUN find /gen/envoy -type d -exec touch {}/__init__.py \;

# ---------------------------------------------------------------------------
# Stage 2 – minimal runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# Install only runtime dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy generated stubs from stage 1.
COPY --from=proto-gen /gen/envoy ./envoy

# Copy application source.
COPY server.py .

# Run as a non-root user for security.
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

EXPOSE 50051

ENTRYPOINT ["python", "server.py"]

#!/usr/bin/env bash
# Regenerate gRPC Python stubs from the vendored proto files.
# Run this script after modifying any .proto file under proto/.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "Installing grpcio-tools (if not already installed)..."
pip install --quiet grpcio-tools==1.80.0 protobuf==6.33.6

echo "Generating stubs..."
python -m grpc_tools.protoc \
    -I proto \
    --python_out=. \
    --grpc_python_out=. \
    proto/envoy/config/core/v3/base.proto \
    proto/envoy/service/ext_proc/v3/external_processor.proto

echo "Creating __init__.py markers..."
find envoy -type d -exec touch {}/__init__.py \;

echo "Done.  Generated files:"
find envoy -name "*_pb2*.py" | sort

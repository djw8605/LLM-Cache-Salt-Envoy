# cache-salt extproc service

This directory contains the Python gRPC server that implements Envoy's
[External Processing (ext_proc)][ext-proc-docs] API to inject a stable
`cache_salt` field into OpenAI-compatible JSON requests before they reach vLLM.

## How it works

```
Client → Envoy Gateway → ext_proc filter → cache-salt service
                                          ↓
                         mutates request body (adds cache_salt)
                                          ↓
                       Envoy forwards mutated request → vLLM
```

1. Envoy sends the HTTP request headers to this service via a bidirectional
   gRPC stream.
2. The service extracts the `Authorization: Bearer <token>` header.
3. Envoy then sends the buffered request body.
4. The service derives a stable salt:
   - **Preferred**: `base64(HMAC-SHA256(CACHE_SALT_HMAC_SECRET, token))`
   - **Fallback** _(if secret is absent)_: `base64(SHA-256(token))`
5. The `cache_salt` field is injected (or overwritten) in the JSON body.
6. The mutated body is returned to Envoy, which forwards it upstream.

### Why derive the salt instead of forwarding the token?

Forwarding the raw bearer token as the cache salt would expose the token to
vLLM logs, metrics, and any downstream systems.  Deriving an HMAC-keyed hash
produces a value that:

- is **deterministic** (same token → same salt, so prefix-cache hits work), and
- does **not reveal** the original token.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `CACHE_SALT_HMAC_SECRET` | Recommended | _(empty)_ | HMAC-SHA256 secret for salt derivation.  Mount from a Kubernetes `Secret`. |
| `LOG_LEVEL` | No | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `PORT` | No | `50051` | TCP port to listen on. |
| `LISTEN_ADDR` | No | `[::]:<PORT>` | Full gRPC listen address. |

## Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Generate gRPC stubs (only needed if proto files change)
./generate_proto.sh

# Run the service
CACHE_SALT_HMAC_SECRET=devsecret LOG_LEVEL=DEBUG python server.py
```

## Testing

Use [grpc_cli][grpc-cli] or a simple Python client:

```bash
# Start server in one terminal
CACHE_SALT_HMAC_SECRET=devsecret python server.py

# In another terminal, send a test request with grpcurl
grpcurl -plaintext -d '{}' localhost:50051 \
    envoy.service.ext_proc.v3.ExternalProcessor/Process
```

Or run the unit tests (if added):

```bash
python -m pytest tests/
```

## Proto files

The `proto/` directory contains a minimal, self-contained subset of the
[Envoy data-plane API][envoy-api] proto files.  Field numbers match the
official API so wire compatibility with Envoy Gateway is maintained.

To regenerate the Python stubs after changing a proto file:

```bash
./generate_proto.sh
```

The generated `envoy/` package (stubs) is committed to the repo so that the
Docker build's runtime stage doesn't need `grpcio-tools` at runtime.

## Building the container

```bash
docker build -t cache-salt-extproc:local .
docker run --rm -p 50051:50051 \
    -e CACHE_SALT_HMAC_SECRET=devsecret \
    cache-salt-extproc:local
```

[ext-proc-docs]: https://gateway.envoyproxy.io/latest/tasks/extensibility/ext-proc/
[envoy-api]: https://github.com/envoyproxy/envoy/tree/main/api/envoy/service/ext_proc/v3
[grpc-cli]: https://github.com/grpc/grpc/blob/master/doc/command_line_tool.md

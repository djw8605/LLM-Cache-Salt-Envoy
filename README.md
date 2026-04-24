# LLM-Cache-Salt-Envoy

A GitOps-ready project that adds a server-side **Envoy External Processing
(ext_proc)** service in Python to inject a stable `cache_salt` into
OpenAI-compatible JSON requests before they are forwarded to
[vLLM](https://docs.vllm.ai/).

## How it works

```
Client ──→ Envoy Gateway ──→ ext_proc filter ──→ cache-salt service
                                               ↓
                            mutates JSON body (adds cache_salt)
                                               ↓
                           Envoy forwards mutated request ──→ vLLM
```

1. A request arrives at Envoy Gateway.
2. The `EnvoyExtensionPolicy` routes it to this service via gRPC ext_proc.
3. The service extracts the `Authorization: Bearer <token>` header and derives
   a stable, opaque salt: `base64(HMAC-SHA256(CACHE_SALT_HMAC_SECRET, token))`.
4. The `cache_salt` field is injected (or overwritten) in the JSON request body.
5. Envoy forwards the mutated request to vLLM.
6. vLLM uses `cache_salt` for per-user prefix-cache isolation.

The client is never responsible for setting `cache_salt`.  The raw token is
**never** forwarded downstream or written to logs.

## Repository layout

```
.
├── .github/workflows/
│   └── build-and-update.yaml   # CI: build image, push, update kustomize tag
├── argocd/
│   ├── application-dev.yaml    # Argo CD Application for dev
│   └── application-prod.yaml   # Argo CD Application for prod
├── deploy/
│   ├── base/                   # Kustomize base manifests
│   │   ├── namespace.yaml
│   │   ├── deployment.yaml
│   │   ├── service.yaml
│   │   ├── secret-example.yaml
│   │   ├── envoy-extension-policy.yaml
│   │   └── kustomization.yaml
│   └── overlays/
│       ├── dev/kustomization.yaml
│       └── prod/kustomization.yaml  # ← CI updates the image tag here
└── extproc/cache-salt/
    ├── README.md               # Service-specific documentation
    ├── server.py               # Python gRPC ext_proc service
    ├── requirements.txt
    ├── Dockerfile
    ├── generate_proto.sh       # Regenerate gRPC stubs after proto changes
    ├── proto/                  # Vendored minimal Envoy API proto files
    └── envoy/                  # Generated gRPC Python stubs (committed)
```

## GitOps flow

```
Developer pushes code
        │
        ▼
GitHub Actions (build-and-update.yaml)
  1. Builds container image
  2. Tags with git SHA and pushes to GHCR
  3. Runs: kustomize edit set image ... :<sha>
  4. Commits deploy/overlays/prod/kustomization.yaml [skip ci]
  5. Pushes commit back to the same branch
        │
        ▼
Argo CD detects manifest change
  → Redeploys cache-salt-extproc with the new image
```

## Deployment guide

### Prerequisites

- A Kubernetes cluster with **Envoy Gateway** installed (via Helm, separately).
- **Argo CD** installed in the cluster.
- `kubectl`, `kustomize`, and `docker` (or `podman`) on your workstation.

### 1. Configure secrets

Create the HMAC secret that protects the cache salt derivation:

```bash
kubectl create namespace llm-cache-salt

kubectl create secret generic cache-salt-hmac \
  --namespace llm-cache-salt \
  --from-literal=secret="$(openssl rand -base64 32)"
```

> **Never commit the real secret value.**  The `secret-example.yaml` in this
> repo is a placeholder only.

### 2. Update the `EnvoyExtensionPolicy` target

Edit `deploy/base/envoy-extension-policy.yaml` and set `spec.targetRef.name`
to the name of the `HTTPRoute` that fronts your vLLM deployment.

### 3. Update Argo CD application manifests

Edit `argocd/application-dev.yaml` and `argocd/application-prod.yaml` and
replace `https://github.com/OWNER/REPO.git` with your actual repository URL.

Apply the Argo CD Applications:

```bash
kubectl apply -f argocd/application-dev.yaml
kubectl apply -f argocd/application-prod.yaml
```

Argo CD will sync the Kustomize overlays and deploy the service automatically.

### 4. Configure GitHub Actions

In your GitHub repository settings (**Settings → Actions → General**):

- Set **Workflow permissions** to *Read and write permissions* so the workflow
  can push the kustomize image-tag commit back.

No additional secrets are required for GHCR when using the default
`GITHUB_TOKEN`.  If you use a different registry, add these repository secrets:

| Secret | Purpose |
|---|---|
| `PAT_TOKEN` | Personal access token (if the default `GITHUB_TOKEN` cannot trigger downstream workflows) |

### 5. Test the service locally

See [extproc/cache-salt/README.md](extproc/cache-salt/README.md) for
instructions on building and running the service locally.

## Configuration reference

| Environment variable | Required | Default | Description |
|---|---|---|---|
| `CACHE_SALT_HMAC_SECRET` | Recommended | _(empty)_ | HMAC-SHA256 key for salt derivation.  Mounted from a Kubernetes `Secret`. |
| `LOG_LEVEL` | No | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `PORT` | No | `50051` | gRPC listen port. |
| `LISTEN_ADDR` | No | `[::]:<PORT>` | Full gRPC listen address. |

## Why HMAC instead of forwarding the token?

Forwarding the raw bearer token as `cache_salt` would expose the token to
vLLM logs, metrics, and any downstream system.  An HMAC-keyed hash produces a
value that:

- is **deterministic** (same token → same salt, so prefix-cache hits work across
  requests from the same user), and
- does **not reveal** the original token even if vLLM logs or metrics are leaked.

## Security notes

- The raw bearer token is **never logged** or forwarded downstream.
- The HMAC secret is mounted from a Kubernetes `Secret` and **never appears in
  manifests** committed to this repo.
- The pod runs as a non-root user (`UID 10000`).
- Resource limits prevent noisy-neighbour issues.

## Contributing

1. Make your changes to `extproc/cache-salt/server.py` or the manifests.
2. Push to `main` (or open a PR and merge).
3. GitHub Actions builds the new image and updates the kustomize tag.
4. Argo CD redeploys automatically.
"""
Envoy External Processing (ext_proc) service that injects a stable ``cache_salt``
into OpenAI-compatible JSON request bodies before they are forwarded to vLLM.

Behaviour
---------
* Listens for bidirectional gRPC streams from Envoy's ext_proc filter.
* For each HTTP request:
  - Extracts the bearer token from the ``Authorization`` header.
  - Derives a stable, opaque salt via ``base64(HMAC-SHA256(secret, token))``.
    Falls back to ``base64(SHA-256(token))`` with a warning if the secret is
    absent (not recommended for production).
  - Injects (or overwrites) the ``cache_salt`` field in the JSON request body.
* Passes through unchanged (with a warning log) for:
  - Requests without a bearer token.
  - Non-JSON or unparseable bodies.
  - Non-POST requests, or paths that don't look like OpenAI inference endpoints.

Configuration (environment variables)
--------------------------------------
CACHE_SALT_HMAC_SECRET  HMAC secret used to derive the salt.  Required for
                         the secure derivation path.  Mount from a Kubernetes
                         Secret.
LOG_LEVEL               Python logging level (default: INFO).
PORT                    TCP port to listen on (default: 50051).
LISTEN_ADDR             Full gRPC listen address (default: [::]:<PORT>).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re

import grpc
from grpc import aio

# Generated gRPC stubs (vendored in this directory tree).
from envoy.service.ext_proc.v3 import (
    external_processor_pb2 as pb2,
    external_processor_pb2_grpc as pb2_grpc,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("cache-salt-extproc")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_HMAC_SECRET: bytes = os.environ.get("CACHE_SALT_HMAC_SECRET", "").encode()

_PORT = int(os.environ.get("PORT", "50051"))
_LISTEN_ADDR = os.environ.get("LISTEN_ADDR", f"[::]:{_PORT}")

# Matches OpenAI-compatible inference endpoint paths, e.g.:
#   /v1/chat/completions  /v1/completions  /v1/embeddings
_OPENAI_PATH_RE = re.compile(
    r"^/v\d+/(chat/completions|completions|embeddings|rerank|score)(/|$)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_bearer_token(headers: dict[str, str]) -> str | None:
    """Return the raw token from an ``Authorization: Bearer <token>`` header.

    Returns ``None`` if the header is absent or not a bearer token.
    The raw token value is *never* logged.
    """
    auth_value = headers.get("authorization", "")
    if auth_value.lower().startswith("bearer "):
        return auth_value[len("bearer "):]
    return None


def _derive_salt(token: str) -> str:
    """Derive a stable, opaque salt from *token*.

    Preferred:  ``base64(HMAC-SHA256(CACHE_SALT_HMAC_SECRET, token))``
    Fallback:   ``base64(SHA-256(token))``  – used only if the secret is unset.

    The derived value is safe to store in vLLM's ``cache_salt`` field because:
    * It is deterministic (same token → same salt) so prefix-cache hits work.
    * It does not reveal the raw token to vLLM or to logs.
    """
    if _HMAC_SECRET:
        digest = hmac.new(_HMAC_SECRET, token.encode(), hashlib.sha256).digest()
    else:
        logger.warning(
            "CACHE_SALT_HMAC_SECRET is not set; using plain SHA-256 of token "
            "(less secure — set the secret in production)"
        )
        digest = hashlib.sha256(token.encode()).digest()
    return base64.b64encode(digest).decode()


def _mutate_body(raw_body: bytes, salt: str) -> bytes | None:
    """Parse *raw_body* as JSON, inject ``cache_salt``, and re-serialise.

    Returns the mutated bytes, or ``None`` if *raw_body* is not valid JSON.
    The ``cache_salt`` field is always overwritten, even if the client supplied one.
    """
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(payload, dict):
        # Unusual — body is a JSON array or scalar; pass through.
        return None

    payload["cache_salt"] = salt
    return json.dumps(payload, separators=(",", ":")).encode()


def _headers_to_dict(header_map: pb2.HttpHeaders) -> dict[str, str]:
    """Convert a protobuf ``HeaderMap`` to a lowercase-keyed Python dict."""
    return {h.key.lower(): h.value for h in header_map.headers.headers}


def _is_openai_post(headers: dict[str, str]) -> bool:
    """Return True when the request looks like an OpenAI-compatible POST."""
    method = headers.get(":method", "").upper()
    path = headers.get(":path", "")
    return method == "POST" and bool(_OPENAI_PATH_RE.match(path))


# ---------------------------------------------------------------------------
# gRPC service implementation
# ---------------------------------------------------------------------------


class CacheSaltServicer(pb2_grpc.ExternalProcessorServicer):
    """Implements the bidirectional ExternalProcessor.Process RPC.

    State is maintained *per gRPC stream*, which corresponds to one HTTP
    request/response lifecycle in Envoy's ext_proc model.
    """

    async def Process(
        self,
        request_iterator,
        context: grpc.aio.ServicerContext,
    ):
        """Main ext_proc handler.

        Envoy sends one ``ProcessingRequest`` per lifecycle phase and expects
        one ``ProcessingResponse`` in return before sending the next phase.
        """
        bearer_token: str | None = None
        openai_request: bool = False

        async for req in request_iterator:
            phase = req.WhichOneof("request")

            # ------------------------------------------------------------------
            # Phase 1 – request headers
            # ------------------------------------------------------------------
            if phase == "request_headers":
                hdrs = _headers_to_dict(req.request_headers)
                openai_request = _is_openai_post(hdrs)

                token = _extract_bearer_token(hdrs)
                if token:
                    bearer_token = token
                elif openai_request:
                    logger.warning(
                        "OpenAI-compatible POST has no bearer token; "
                        "passing request through without cache_salt injection"
                    )

                # Always continue — header mutations are not needed here.
                yield pb2.ProcessingResponse(
                    request_headers=pb2.HeadersResponse(
                        response=pb2.CommonResponse(
                            status=pb2.CommonResponse.CONTINUE
                        )
                    )
                )

            # ------------------------------------------------------------------
            # Phase 2 – request body
            # ------------------------------------------------------------------
            elif phase == "request_body":
                if not openai_request or bearer_token is None:
                    # Not our concern — pass through.
                    yield pb2.ProcessingResponse(
                        request_body=pb2.BodyResponse(
                            response=pb2.CommonResponse(
                                status=pb2.CommonResponse.CONTINUE
                            )
                        )
                    )
                    continue

                salt = _derive_salt(bearer_token)
                raw_body = req.request_body.body
                mutated = _mutate_body(raw_body, salt)

                if mutated is None:
                    logger.warning(
                        "Request body is not valid JSON; "
                        "passing through without cache_salt injection"
                    )
                    yield pb2.ProcessingResponse(
                        request_body=pb2.BodyResponse(
                            response=pb2.CommonResponse(
                                status=pb2.CommonResponse.CONTINUE
                            )
                        )
                    )
                else:
                    logger.debug(
                        "Injected cache_salt into request body "
                        "(original=%d bytes, mutated=%d bytes)",
                        len(raw_body),
                        len(mutated),
                    )
                    yield pb2.ProcessingResponse(
                        request_body=pb2.BodyResponse(
                            response=pb2.CommonResponse(
                                status=pb2.CommonResponse.CONTINUE_AND_REPLACE,
                                body_mutation=pb2.BodyMutation(body=mutated),
                            )
                        )
                    )

            # ------------------------------------------------------------------
            # All other phases (response headers, trailers, etc.)
            # ------------------------------------------------------------------
            else:
                yield pb2.ProcessingResponse()


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------


async def _serve() -> None:
    server = aio.server()
    pb2_grpc.add_ExternalProcessorServicer_to_server(CacheSaltServicer(), server)
    server.add_insecure_port(_LISTEN_ADDR)
    logger.info("CacheSalt ExtProc server starting on %s", _LISTEN_ADDR)
    await server.start()
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(_serve())

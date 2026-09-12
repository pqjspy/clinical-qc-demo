"""Small native Ollama HTTP adapter. Loopback only; no SDK, proxy, or fallback."""
from __future__ import annotations

import json
import socket
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .data import _no_duplicate_keys

MODEL = "qwen3:4b-instruct-2507-q4_K_M"
BASE_URL = "http://127.0.0.1:11434"
OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 8192, "num_predict": 1200}


class ModelError(RuntimeError):
    def __init__(self, message, *, response_detail=None):
        super().__init__(message)
        self.response_detail = response_detail


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelError("Local model request attempted a redirect; rejected.")


def decode_json(text: str):
    return json.loads(text, object_pairs_hook=_no_duplicate_keys,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"Non-finite JSON: {x}")))


class OllamaClient:
    mode = "live_local"

    def __init__(self, *, model: str = MODEL, timeout: float = 120, options: dict | None = None):
        if not 0 < timeout <= 180:
            raise ValueError("Timeout must be between 0 and 180 seconds.")
        self.model, self.timeout = model, timeout
        self.options = dict(OPTIONS if options is None else options)
        self.calls: list[dict] = []
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path: str, payload: dict | None = None) -> dict:
        if path not in {"/api/tags", "/api/version", "/api/chat"}:
            raise ModelError("Only explicit local inference/metadata endpoints are supported.")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(BASE_URL + path, data=body, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ModelError("Model response exceeds the 1 MiB limit.",
                                 response_detail={"body_prefix": raw[:4096].decode("utf-8", errors="replace"), "truncated": True})
            result = decode_json(raw.decode("utf-8"))
        except HTTPError as exc:
            error_body = exc.read(4097)
            raise ModelError(f"Local Ollama HTTP error {exc.code}; no fallback or retry.",
                             response_detail={"http_status": exc.code, "body_prefix": error_body[:4096].decode("utf-8", errors="replace"),
                                              "truncated": len(error_body) > 4096}) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise ModelError(f"Local Ollama request failed ({type(exc).__name__}); no fallback or retry.") from exc
        except (ValueError, UnicodeError) as exc:
            raise ModelError("Local server returned invalid JSON.",
                             response_detail={"body": raw.decode("utf-8", errors="replace"), "truncated": False}) from exc
        if not isinstance(result, dict) or "error" in result:
            raise ModelError("Local Ollama response is not a successful object.", response_detail={"body": result})
        return result

    def identity(self) -> dict:
        version = self.request("/api/version")
        tags = self.request("/api/tags")
        candidates = [m for m in tags.get("models", []) if m.get("name") == self.model]
        if len(candidates) != 1 or not candidates[0].get("digest"):
            raise ModelError(f"Requested local model is unavailable: {self.model}; no automatic download.")
        item = candidates[0]
        if not item.get("details", {}).get("family", "").startswith("qwen"):
            raise ModelError("M2 requires a local Qwen model.")
        return {"model": self.model, "digest": item["digest"], "size": item.get("size"),
                "details": item.get("details"), "ollama_version": version.get("version"),
                "base_url": BASE_URL, "options": dict(self.options)}

    def chat(self, messages: list[dict], schema: dict, *, stage: str) -> str:
        payload = {"model": self.model, "messages": messages, "format": schema,
                   "stream": False, "keep_alive": "5m", "options": dict(self.options)}
        call = {"stage": stage, "request": payload, "state": "started"}
        self.calls.append(call)
        started = perf_counter()
        try:
            response = self.request("/api/chat", payload)
            call["response"] = response
            if response.get("done") is not True or response.get("done_reason") != "stop":
                raise ModelError("Generation incomplete/truncated; refusing partial output.")
            if response.get("model") != self.model:
                raise ModelError("Response model differs from the requested model.")
            message = response.get("message", {})
            if message.get("role") != "assistant" or message.get("tool_calls"):
                raise ModelError("Unexpected model message; no tool execution is allowed.")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("Empty model response.")
            call["state"] = "completed"
            return content
        except Exception as exc:
            call.update(state="failed", error_type=type(exc).__name__, error=str(exc))
            if isinstance(exc, ModelError) and exc.response_detail is not None:
                call["failed_response_detail"] = exc.response_detail
            raise
        finally:
            call["wall_ms"] = round((perf_counter() - started) * 1000, 3)

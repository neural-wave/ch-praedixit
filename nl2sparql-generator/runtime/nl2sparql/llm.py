"""Minimal clients for a locally served open-weight model.

Two backends, chosen with ``"backend"`` in config.json:
  * ``ollama``: Ollama HTTP API (``POST /api/chat``); Ollama picks CUDA, Metal or CPU itself.
  * ``openai``: any OpenAI-compatible server (vLLM, llama.cpp ``llama-server``, LM Studio, TGI),
    e.g. for a fine-tuned model served on a GPU machine.

Only ``requests`` is needed. Token counts reported by the server are kept so the API can
return the total tokens consumed per request, as the hackathon requires.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests


@dataclass
class LLMUsage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    details: list[dict] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {
            "llm_calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_seconds": round(self.seconds, 3),
        }


class OllamaLLM:
    """Chat completion against ``POST /api/chat`` with deterministic decoding."""

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        num_ctx: int = 8192,
        temperature: float = 0.0,
        seed: int = 42,
        keep_alive: str = "30m",
        timeout: int = 600,
        think: bool | None = False,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.temperature = temperature
        self.seed = seed
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.think = think

    def chat(self, messages: list[dict], usage: LLMUsage | None = None, max_tokens: int = 1200,
             stop: list[str] | None = None, temperature: float | None = None, seed: int | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "seed": self.seed if seed is None else seed,
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens,
                "top_k": 1 if (temperature is None or temperature == 0) else 40,
            },
        }
        if stop:
            payload["options"]["stop"] = stop
        if self.think is not None and self.model.startswith(("qwen3", "deepseek-r1", "gpt-oss")):
            payload["think"] = self.think
        start = time.perf_counter()
        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.perf_counter() - start
        content = data.get("message", {}).get("content", "")
        if usage is not None:
            usage.calls += 1
            usage.prompt_tokens += int(data.get("prompt_eval_count") or 0)
            usage.completion_tokens += int(data.get("eval_count") or 0)
            usage.seconds += elapsed
            usage.details.append(
                {
                    "prompt_tokens": data.get("prompt_eval_count"),
                    "completion_tokens": data.get("eval_count"),
                    "seconds": round(elapsed, 3),
                }
            )
        return content

    def warm_up(self) -> None:
        """Load the weights so the first user request does not pay the load time."""
        requests.post(
            f"{self.base_url}/api/generate",
            json={"model": self.model, "prompt": "", "keep_alive": self.keep_alive},
            timeout=self.timeout,
        )


class OpenAICompatibleLLM(OllamaLLM):
    """Chat completion against ``POST {base_url}/v1/chat/completions``."""

    def chat(self, messages: list[dict], usage: LLMUsage | None = None, max_tokens: int = 1200,
             stop: list[str] | None = None, temperature: float | None = None, seed: int | None = None) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
            "seed": self.seed if seed is None else seed,
        }
        if stop:
            payload["stop"] = stop
        start = time.perf_counter()
        url = self.base_url if self.base_url.endswith("/v1") else self.base_url + "/v1"
        resp = requests.post(f"{url}/chat/completions", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.perf_counter() - start
        content = data["choices"][0]["message"].get("content") or ""
        if usage is not None:
            u = data.get("usage") or {}
            usage.calls += 1
            usage.prompt_tokens += int(u.get("prompt_tokens") or 0)
            usage.completion_tokens += int(u.get("completion_tokens") or 0)
            usage.seconds += elapsed
        return content

    def warm_up(self) -> None:
        pass


def make_llm(cfg: dict) -> OllamaLLM:
    backend = cfg.get("backend", "ollama")
    url = cfg.get("llm_url") or cfg.get("ollama_url") or "http://localhost:11434"
    if backend == "openai":
        return OpenAICompatibleLLM(cfg["model"], url)
    if backend != "ollama":
        raise ValueError(f"unknown backend {backend!r} (use 'ollama' or 'openai')")
    return OllamaLLM(cfg["model"], url, num_ctx=cfg.get("num_ctx", 8192))

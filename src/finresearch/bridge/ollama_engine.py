"""Tier 3 engine: local models through Ollama's HTTP API.

Design rules (16 GB M1 Pro):
* One generation at a time (process-wide lock) — two 9B models would swap and stall the machine.
* Pick the 4B model automatically when free RAM can't hold the 9B model (unless 9B is already loaded).
* Structured output uses Ollama's `format` (JSON schema) + jsonschema validation + bounded repair turns.
* Never silently truncate: prompts beyond the local context budget raise CapabilityMismatch.
* No web or tool use locally — tasks that need them are refused, never faked.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import jsonschema

from finresearch.bridge.types import (
    AgentResult,
    AgentTask,
    Capability,
    CapabilityMismatch,
    EngineUnavailable,
    ModelClass,
    SchemaViolation,
    Tier,
    TransientError,
)

_GEN_LOCK = asyncio.Lock()  # process-wide: one local generation at a time
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S)


def free_memory_gb() -> float | None:
    """Approximate reclaimable RAM on macOS (free + inactive + speculative pages)."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"page size of (\d+) bytes", out)
    page = int(m.group(1)) if m else 16384
    pages = 0
    for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"):
        mm = re.search(rf"{key}:\s+(\d+)", out)
        if mm:
            pages += int(mm.group(1))
    return pages * page / 1024**3


def _leaf_values(obj: Any) -> list[Any]:
    if isinstance(obj, dict):
        return [v for x in obj.values() for v in _leaf_values(x)]
    if isinstance(obj, list):
        return [v for x in obj for v in _leaf_values(x)]
    return [obj]


def _strip_to_json(text: str) -> str:
    text = _THINK_BLOCK.sub("", text).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S).strip()
    return text


class OllamaEngine:
    tier = Tier.LOCAL

    def __init__(
        self,
        *,
        base_url: str,
        models: dict[ModelClass, str],
        low_memory_model: str,
        min_free_gb_for_large: float,
        embed_model: str,
        ocr_model: str,
        num_ctx: int = 32768,
        keep_alive: str = "10m",
        timeout_s: float = 900,
        repair_attempts: int = 2,
        max_prompt_chars: int = 90_000,
        memory_probe=free_memory_gb,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.models = models
        self.low_memory_model = low_memory_model
        self.min_free_gb_for_large = min_free_gb_for_large
        self.embed_model = embed_model
        self.ocr_model = ocr_model
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.timeout_s = timeout_s
        self.repair_attempts = repair_attempts
        self.max_prompt_chars = max_prompt_chars
        self.memory_probe = memory_probe
        self._transport = transport

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(timeout or self.timeout_s, connect=5.0),
            transport=self._transport,
        )  # fmt: skip

    # ------------------------------------------------------------ health
    async def installed_models(self) -> set[str]:
        try:
            async with self._client(10) as c:
                r = await c.get("/api/tags")
                r.raise_for_status()
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"Ollama not reachable at {self.base_url}: {e}") from e
        names = set()
        for m in r.json().get("models", []):
            names.add(m["name"])
            if m["name"].endswith(":latest"):
                names.add(m["name"].removesuffix(":latest"))
        return names

    async def _ps(self) -> list[dict[str, Any]]:
        try:
            async with self._client(10) as c:
                r = await c.get("/api/ps")
                r.raise_for_status()
            return r.json().get("models", [])
        except httpx.HTTPError:
            return []

    async def loaded_models(self) -> set[str]:
        return {m["name"] for m in await self._ps()}

    async def reclaimable_gb(self) -> float | None:
        """Free RAM plus memory held by models Ollama would evict to load a new one."""
        free = self.memory_probe()
        if free is None:
            return None
        held = sum(int(m.get("size") or 0) for m in await self._ps()) / 1024**3
        return free + held

    async def health(self) -> dict[str, Any]:
        async with self._client(10) as c:
            try:
                ver = (await c.get("/api/version")).json().get("version")
            except httpx.HTTPError as e:
                raise EngineUnavailable(f"Ollama not reachable at {self.base_url}: {e}") from e
        installed = await self.installed_models()
        required = {*self.models.values(), self.low_memory_model, self.embed_model, self.ocr_model}
        missing = sorted(m for m in required if m not in installed)
        return {
            "tier": self.tier.value,
            "ollama_version": ver,
            "missing_models": missing,
            "loaded": sorted(await self.loaded_models()),
            "free_ram_gb": round(self.memory_probe() or 0, 1),
            "reclaimable_gb": round(await self.reclaimable_gb() or 0, 1),
        }

    async def _choose_model(self, model_class: ModelClass) -> tuple[str, str | None]:
        """Return (model, note). Downgrade to the low-memory model when RAM is tight."""
        wanted = self.models[model_class]
        if wanted == self.low_memory_model:
            return wanted, None
        if wanted in await self.loaded_models():
            return wanted, None
        avail = await self.reclaimable_gb()
        if avail is not None and avail < self.min_free_gb_for_large:
            return self.low_memory_model, (
                f"only {avail:.1f} GB reclaimable (< {self.min_free_gb_for_large} GB); used {self.low_memory_model}"
            )
        return wanted, None

    # ------------------------------------------------------------ generation
    async def run(self, task: AgentTask) -> AgentResult:
        blocked = task.capabilities & {Capability.WEB, Capability.TOOLS}
        if blocked:
            raise CapabilityMismatch(
                f"local tier cannot provide {sorted(c.value for c in blocked)} for task {task.name}"
            )
        total_chars = len(task.prompt) + len(task.system_prompt or "")
        if Capability.LONG_CONTEXT in task.capabilities or total_chars > self.max_prompt_chars:
            raise CapabilityMismatch(
                f"prompt of {total_chars:,} chars exceeds local budget {self.max_prompt_chars:,} (no truncation)"
            )

        installed = await self.installed_models()
        model, note = await self._choose_model(task.model_class)
        if model not in installed:
            raise EngineUnavailable(f"local model {model} not installed (ollama pull {model})")

        system = task.system_prompt or "You are a careful financial research assistant. Never invent numbers."
        if task.json_schema is not None:
            system += (
                "\nRespond ONLY with a JSON object that validates against this JSON Schema. "
                "Use null for unknown values rather than guessing.\n" + json.dumps(task.json_schema)
            )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task.prompt},
        ]

        started = time.monotonic()
        warnings = [note] if note else []
        in_tok = out_tok = 0
        structured: Any = None
        text = ""
        async with _GEN_LOCK:
            for attempt in range(self.repair_attempts + 1):
                data = await self._chat(model, messages, task.json_schema)
                text = data.get("message", {}).get("content", "")
                in_tok += int(data.get("prompt_eval_count") or 0)
                out_tok += int(data.get("eval_count") or 0)
                if data.get("done_reason") == "length":
                    warnings.append("output hit the context/length limit")
                if task.json_schema is None:
                    break
                try:
                    structured = json.loads(_strip_to_json(text))
                    jsonschema.validate(structured, task.json_schema)
                    break
                except (json.JSONDecodeError, jsonschema.ValidationError) as e:
                    err = e.msg if isinstance(e, json.JSONDecodeError) else e.message
                    structured = None
                    if attempt == self.repair_attempts:
                        raise SchemaViolation(
                            f"{task.name}: local output invalid after {attempt + 1} attempts: {err}",
                            detail={"last_output": text[:800]},
                        ) from e
                    warnings.append(f"schema repair #{attempt + 1}: {err[:120]}")
                    messages += [
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": f"That output is invalid: {err}. Return corrected JSON only.",
                        },
                    ]
        if structured is not None:
            leaves = _leaf_values(structured)
            if leaves and sum(v is None for v in leaves) / len(leaves) > 0.6:
                warnings.append(f"mostly-null output ({sum(v is None for v in leaves)}/{len(leaves)} leaves)")
        return AgentResult(
            task_name=task.name, tier=self.tier, model=model, ok=True,
            structured_output=structured, text=_THINK_BLOCK.sub("", text).strip(),
            input_tokens=in_tok, output_tokens=out_tok, duration_s=time.monotonic() - started,
            num_turns=1, warnings=warnings,
        )  # fmt: skip

    async def _chat(self, model: str, messages: list[dict[str, str]], schema: dict | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
        }
        if schema is not None:
            body["format"] = schema
        try:
            async with self._client() as c:
                r = await c.post("/api/chat", json=body)
        except httpx.TimeoutException as e:
            raise TransientError(f"local model {model} timed out") from e
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"Ollama request failed: {e}") from e
        if r.status_code == 404:
            raise EngineUnavailable(f"local model {model} not found")
        if r.status_code >= 500:
            raise TransientError(f"Ollama {r.status_code}: {r.text[:300]}")
        if r.status_code >= 400:
            # e.g. a runtime that rejects `format` or `think` — surface clearly
            raise EngineUnavailable(f"Ollama {r.status_code}: {r.text[:300]}")
        return r.json()

    # ------------------------------------------------------------ embeddings & OCR
    async def embed(self, texts: list[str]) -> list[list[float]]:
        async with self._client(120) as c:
            r = await c.post("/api/embed", json={"model": self.embed_model, "input": texts,
                                                  "keep_alive": self.keep_alive})  # fmt: skip
        if r.status_code >= 400:
            raise EngineUnavailable(f"embed failed {r.status_code}: {r.text[:200]}")
        return r.json()["embeddings"]

    async def ocr_image(self, image_path: Path, prompt: str = "Text Recognition:") -> OcrResult:
        """OCR one page image with glm-ocr.

        The model transcribes accurately but often fails to stop at the end of a page and starts replaying
        earlier paragraphs. We stream, detect the first repeated paragraph (or a token-level loop) and cut
        there, returning only the clean transcription plus a warning.
        """
        img = base64.b64encode(Path(image_path).read_bytes()).decode()
        body = {
            "model": self.ocr_model, "prompt": prompt, "images": [img], "stream": True, "keep_alive": "2m",
            # num_ctx 16384: lower values crash on images. num_predict caps a runaway page.
            "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096,
                        "repeat_penalty": 1.05, "repeat_last_n": 128},
        }  # fmt: skip
        text, warnings, stopped = "", [], False
        started = time.monotonic()
        async with _GEN_LOCK, self._client(600) as c, c.stream("POST", "/api/generate", json=body) as r:
            if r.status_code >= 400:
                raise EngineUnavailable(f"OCR failed {r.status_code}: {(await r.aread())[:200]!r}")
            async for line in r.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                text += chunk.get("response", "")
                if chunk.get("done"):
                    if chunk.get("done_reason") == "length":
                        warnings.append("hit num_predict cap")
                    break
                if text.endswith("\n") or len(text) % 400 < 5:
                    cut = find_repetition(text)
                    if cut is not None:
                        text, stopped = text[:cut], True
                        warnings.append("repetition loop detected; output truncated at loop start")
                        break
        cut = find_repetition(text)
        if cut is not None and not stopped:
            text = text[:cut]
            warnings.append("repetition loop detected; output truncated at loop start")
        return OcrResult(text=text.strip(), warnings=warnings, seconds=time.monotonic() - started,
                         model=self.ocr_model)  # fmt: skip


class OcrResult:
    __slots__ = ("model", "seconds", "text", "warnings")

    def __init__(self, text: str, warnings: list[str], seconds: float, model: str):
        self.text, self.warnings, self.seconds, self.model = text, warnings, seconds, model

    def __repr__(self) -> str:
        return f"OcrResult({len(self.text)} chars, {self.seconds:.0f}s, warnings={self.warnings})"


def find_repetition(text: str, min_para: int = 30, token_run: int = 12) -> int | None:
    """Index where a degenerate loop starts, or None.

    * paragraph loop: a paragraph (>= min_para chars) that already appeared earlier
    * token loop: the same token repeated >= token_run times in a row (e.g. "2013, 2013, 2013, ...")
    """
    seen: set[str] = set()
    pos = 0
    for para in text.split("\n"):
        key = para.strip()
        if len(key) >= min_para:
            if key in seen:
                return pos
            seen.add(key)
        pos += len(para) + 1
    m = re.search(rf"(\S+)(?:[\s,;]+\1){{{token_run - 1},}}", text)
    return m.start() if m else None

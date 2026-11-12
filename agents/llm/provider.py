"""Pluggable LLM provider for investigation narratives.

The LLM is never the source of numbers, measurements or diagnoses: it only
turns the structured, already-computed investigation record into readable
prose. Every narrative passes `agents.evidence.grounding.check_grounding`;
if it cites a number or asset not present in the evidence, it is discarded and
the deterministic template narrative is used instead.

Providers (env LLM_PROVIDER):
  none       deterministic template (default; no external calls)
  anthropic  Claude via the official Anthropic SDK (ANTHROPIC_API_KEY or an
             `ant auth login` profile); model from LLM_MODEL (default claude-opus-5-5)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("oilintel.agent.llm")
PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"


@dataclass
class LLMResult:
    text: str | None
    provider: str
    model: str | None = None
    error: str | None = None
    request_id: str | None = None


class TemplateProvider:
    name = "none"

    def narrate(self, payload: dict[str, Any]) -> LLMResult:
        return LLMResult(text=None, provider=self.name)


class AnthropicProvider:
    """Claude narrative writer (official `anthropic` SDK)."""

    name = "anthropic"

    def __init__(self, model: str | None = None, effort: str | None = None, timeout_s: float = 60.0):
        import anthropic  # optional dependency

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(timeout=timeout_s, max_retries=2)
        self.model = model or os.environ.get("LLM_MODEL", "claude-opus-5-5")
        self.effort = effort or os.environ.get("LLM_EFFORT", "medium")
        self.system = (PROMPT_DIR / "report_system.md").read_text(encoding="utf-8")

    def narrate(self, payload: dict[str, Any]) -> LLMResult:
        anthropic = self._anthropic
        user = ("Write the investigation narrative for the following structured investigation record. "
                "Use only facts and numbers that appear in it.\n\n<investigation_record>\n"
                + json.dumps(payload, indent=1, default=str) + "\n</investigation_record>")
        try:
            # Server-side refusal fallback is enabled by default: if a safety classifier
            # declines, the API re-runs the request on a fallback model in the same call.
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=8000,
                system=self.system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError:
            return LLMResult(None, self.name, self.model, "authentication failed (no valid Anthropic credentials)")
        except anthropic.PermissionDeniedError:
            return LLMResult(None, self.name, self.model, "credentials lack permission for this model")
        except anthropic.NotFoundError:
            return LLMResult(None, self.name, self.model, f"model {self.model} not found")
        except anthropic.RateLimitError:
            return LLMResult(None, self.name, self.model, "rate limited")
        except anthropic.BadRequestError as e:
            return LLMResult(None, self.name, self.model, f"bad request: {e.message}")
        except anthropic.APIStatusError as e:
            return LLMResult(None, self.name, self.model, f"API error {e.status_code}")
        except anthropic.APIConnectionError:
            return LLMResult(None, self.name, self.model, "connection error / timeout")
        rid = getattr(resp, "_request_id", None)
        if resp.stop_reason == "refusal":
            return LLMResult(None, self.name, self.model, "model declined (refusal)", rid)
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        return LLMResult(text or None, self.name, getattr(resp, "model", self.model), None, rid)


def get_provider(name: str | None = None):
    name = (name or os.environ.get("LLM_PROVIDER", "none")).lower()
    if name == "anthropic":
        try:
            return AnthropicProvider()
        except Exception as e:  # SDK missing / misconfigured -> deterministic fallback
            log.warning("Anthropic provider unavailable (%s); using template narratives", e)
    return TemplateProvider()

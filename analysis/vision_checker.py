"""
analysis/vision_checker.py — Second-pass visual confirmation using Claude vision.

Flow:
  1. Pattern module runs indicators → produces a IndicatorSignal with confidence score
  2. If confidence ≥ threshold, ChartRenderer produces a PNG
  3. VisionChecker sends the PNG + a pattern-specific prompt to Claude
  4. Claude responds: CONFIRM / REJECT / UNCERTAIN
  5. Only CONFIRM proceeds to the order stage

This is the "visual confirmation second" step described in the architecture.
"""

from __future__ import annotations
import base64
from enum import Enum

import anthropic

from config import settings
from utils.logger import log


class VisionVerdict(str, Enum):
    CONFIRM  = "CONFIRM"    # Claude agrees the pattern is present
    REJECT   = "REJECT"     # Claude does not see the pattern
    UNCERTAIN = "UNCERTAIN" # Claude is not sure — treat as REJECT


class VisionChecker:
    MODEL = "claude-opus-4-6"          # Use the most capable vision model

    def __init__(self):
        if settings.vision_confirmation_enabled and settings.anthropic_api_key:
            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        else:
            self._client = None

    def check(
        self,
        chart_png: bytes,
        pattern_name: str,
        pattern_description: str,
        symbol: str,
        action: str,        # "buy" | "sell"
    ) -> VisionVerdict:
        """
        Send the chart image to Claude and ask if the pattern is visually present.
        Returns VisionVerdict.
        """
        if not settings.vision_confirmation_enabled:
            log.debug("VisionChecker | Disabled in config — auto-confirming")
            return VisionVerdict.CONFIRM

        if self._client is None:
            log.warning("VisionChecker | No Anthropic API key set — auto-confirming")
            return VisionVerdict.CONFIRM

        prompt = self._build_prompt(pattern_name, pattern_description, symbol, action)
        b64_image = base64.standard_b64encode(chart_png).decode("utf-8")

        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=256,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64_image,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            )
            raw = response.content[0].text.strip().upper()
            verdict = self._parse_verdict(raw)
            log.info(
                f"VisionChecker | {symbol} {pattern_name} → {verdict} "
                f"(raw: '{response.content[0].text.strip()[:80]}')"
            )
            return verdict

        except Exception as exc:
            log.error(f"VisionChecker | API error: {exc} — defaulting to REJECT")
            return VisionVerdict.REJECT

    # ── Internal ───────────────────────────────────────────────────────────────
    @staticmethod
    def _build_prompt(
        pattern_name: str, description: str, symbol: str, action: str
    ) -> str:
        return f"""You are a professional technical analysis assistant reviewing a candlestick chart.

Symbol: {symbol}
Expected pattern: {pattern_name}
Pattern description: {description}
Expected trade direction: {action.upper()}

Look at the chart carefully. Based on what you see in the candlesticks, volume, and any overlaid indicators:

1. Is the described pattern clearly visible on this chart?
2. Does the visual structure support a {action.upper()} trade right now?

Respond with EXACTLY one word:
- CONFIRM  → if the pattern is clearly present and the trade direction is supported
- REJECT   → if the pattern is not visible or the chart contradicts the trade direction
- UNCERTAIN → if you cannot clearly determine either way

Your one-word answer:"""

    @staticmethod
    def _parse_verdict(raw: str) -> VisionVerdict:
        if "CONFIRM" in raw:
            return VisionVerdict.CONFIRM
        if "REJECT" in raw:
            return VisionVerdict.REJECT
        return VisionVerdict.UNCERTAIN

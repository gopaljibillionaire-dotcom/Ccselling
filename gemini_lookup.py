"""gemini_lookup.py - Use Gemini to enrich card/BIN details from free-form paste.

Accepts lines like:
  4111111111111111|12|28|123|123 Main St|New York|NY|10001|US|extra notes

Returns structured fields + Gemini-inferred brand / type / bank / country when possible.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from config import settings

log = logging.getLogger("gemini_lookup")

# Basic Luhn for sanity (does not guarantee validity).
def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", number)]
    if len(digits) < 12:
        return False
    checksum = 0
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def parse_card_line(line: str) -> Optional[Dict[str, str]]:
    """Parse cc|exp|cvv|address|city|state|zip|country|extra

    exp may be MM/YY, MMYY, MM|YY, or MMYY as single field.
    """
    line = (line or "").strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 3:
        # try space / tab / comma
        parts = re.split(r"[\t,;]+", line)
        parts = [p.strip() for p in parts if p.strip()]
    if len(parts) < 3:
        return None

    cc = re.sub(r"\D", "", parts[0])
    if len(cc) < 12 or len(cc) > 19:
        return None

    exp_raw = parts[1]
    cvv = parts[2] if len(parts) > 2 else ""
    # Handle MM/YY or MMYY in exp field
    exp_m = re.match(r"^(\d{1,2})[/\-\s]?(\d{2,4})$", exp_raw)
    if exp_m:
        mm, yy = exp_m.group(1).zfill(2), exp_m.group(2)
        if len(yy) == 4:
            yy = yy[-2:]
        exp = f"{mm}/{yy}"
    else:
        exp = exp_raw

    address = parts[3] if len(parts) > 3 else ""
    city = parts[4] if len(parts) > 4 else ""
    state = parts[5] if len(parts) > 5 else ""
    zip_code = parts[6] if len(parts) > 6 else ""
    country = parts[7] if len(parts) > 7 else ""
    extra = parts[8] if len(parts) > 8 else ""
    # If more trailing parts, fold into extra
    if len(parts) > 9:
        extra = " | ".join(parts[8:])

    return {
        "cc": cc,
        "exp": exp,
        "cvv": re.sub(r"\D", "", cvv)[:4],
        "address": address[:200],
        "city": city[:80],
        "state": state[:40],
        "zip": zip_code[:20],
        "country": country[:40],
        "extra": extra[:500],
        "bin": cc[:6],
        "luhn": "ok" if luhn_ok(cc) else "fail",
    }


async def gemini_enrich(cards: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Call Gemini once for a batch of BINs and merge brand/type/bank/country hints."""
    if not settings.gemini_api_key or not cards:
        return [{**c, "brand": "", "card_type": "", "bank": "", "gemini_country": "", "gemini_notes": ""} for c in cards]

    bins = sorted({c["bin"] for c in cards if c.get("bin")})
    if not bins:
        return cards

    prompt = (
        "You are a payment-card BIN lookup assistant. "
        "For each 6-digit BIN below, return a JSON array of objects with keys: "
        "bin, brand (Visa/Mastercard/Amex/Discover/UnionPay/JCB/Other), "
        "card_type (Credit/Debit/Prepaid/Unknown), bank (issuer name or empty), "
        "country (ISO 2-letter if known else empty), notes (short). "
        "Only return valid JSON array, no markdown.\n\n"
        f"BINs: {', '.join(bins)}"
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-1.5-flash:generateContent"
        f"?key={settings.gemini_api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 2048},
    }

    bin_map: Dict[str, Dict[str, str]] = {}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=payload)
            if r.status_code == 200:
                data = r.json()
                text = (
                    data.get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                # Strip markdown fences if present
                text = re.sub(r"^```(?:json)?\s*", "", text.strip())
                text = re.sub(r"\s*```$", "", text)
                arr = json.loads(text)
                if isinstance(arr, list):
                    for item in arr:
                        b = str(item.get("bin") or "").strip()
                        if b:
                            bin_map[b] = {
                                "brand": str(item.get("brand") or "")[:40],
                                "card_type": str(item.get("card_type") or "")[:20],
                                "bank": str(item.get("bank") or "")[:80],
                                "gemini_country": str(item.get("country") or "")[:8],
                                "gemini_notes": str(item.get("notes") or "")[:200],
                            }
            else:
                log.warning("Gemini HTTP %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("Gemini enrich failed: %s", type(exc).__name__)

    out = []
    for c in cards:
        extra = bin_map.get(c.get("bin", ""), {})
        out.append({
            **c,
            "brand": extra.get("brand", ""),
            "card_type": extra.get("card_type", ""),
            "bank": extra.get("bank", ""),
            "gemini_country": extra.get("gemini_country", ""),
            "gemini_notes": extra.get("gemini_notes", ""),
        })
    return out


async def parse_and_enrich(raw_text: str) -> List[Dict[str, Any]]:
    """Full pipeline: split lines → parse → Gemini enrich."""
    cards: List[Dict[str, str]] = []
    seen = set()
    for line in (raw_text or "").splitlines():
        parsed = parse_card_line(line)
        if not parsed:
            continue
        key = parsed["cc"] + "|" + parsed["exp"] + "|" + parsed["cvv"]
        if key in seen:
            continue
        seen.add(key)
        cards.append(parsed)
    if not cards:
        return []
    return await gemini_enrich(cards)

#!/usr/bin/env python3
"""Render Botslab publishing/caption copy from confirmed short links."""

from __future__ import annotations

import argparse
import json
import re
import sys


SUPPORTED_PRODUCTS = ("G980H", "G300", "W510", "R810", "W101")
SUPPORTED_PRODUCT_LABELS = {
    "G980H": "G980H",
    "G300": "G300",
    "W510": "W510",
    "R810": "R810",
    "W101": "W101（window cam）",
}

PRODUCT_TAGS = {
    "G980H": "#Botslab #Botslabdashcam #BotslabG980H #dashcam",
    "G300": "#Botslab #Botslabdashcam #BotslabG300H #dashcam",
    "W510": "#Botslab #BotslabW510 #securitycamera",
    "R810": "#Botslab #BotslabR810 #doorbell",
    "W101": "#Botslab #Botslabwindowcamera #BotslabW101 #windowcamera",
}

NO_CODE_VALUES = {"", "无", "没有", "no", "none", "n/a", "na", "null", "nil"}


class CopyError(ValueError):
    """Raised when required copy inputs are invalid."""


def detect_product_code(product: str) -> str:
    text = product.strip().upper()
    if not text:
        raise CopyError("Product cannot be empty.")

    matches = set()
    for code in SUPPORTED_PRODUCTS:
        if re.search(rf"(^|[^A-Z0-9]){re.escape(code)}([^A-Z0-9]|$)", text):
            matches.add(code)

    compact_text = re.sub(r"[^A-Z0-9]", "", text)
    if "WINDOWCAM" in compact_text or "WINDOWCAMERA" in compact_text:
        matches.add("W101")

    matches = sorted(matches, key=SUPPORTED_PRODUCTS.index)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CopyError(f"Product is ambiguous; detected multiple supported codes: {', '.join(matches)}.")
    raise CopyError(
        "Unsupported product. Current publishing-copy automation only supports: "
        + ", ".join(SUPPORTED_PRODUCT_LABELS[code] for code in SUPPORTED_PRODUCTS)
        + "."
    )


def normalize_promo_code(value: str | None) -> str:
    if value is None:
        return ""
    text = value.strip()
    return "" if text.lower() in NO_CODE_VALUES else text


def require_link(value: str, label: str) -> str:
    text = value.strip()
    if not text:
        raise CopyError(f"{label} cannot be empty.")
    if not re.match(r"^https?://", text):
        raise CopyError(f"{label} must start with http:// or https://.")
    return text


def render_copy(
    *,
    product: str,
    amazon_link: str,
    botslab_link: str,
    amazon_code: str,
    botslab_code: str,
) -> dict[str, str]:
    product_code = detect_product_code(product)
    tags = PRODUCT_TAGS[product_code]
    amazon_link = require_link(amazon_link, "amazon_link")
    botslab_link = require_link(botslab_link, "botslab_link")
    amazon_code = normalize_promo_code(amazon_code)
    botslab_code = normalize_promo_code(botslab_code)

    amazon_line = "🌟Amazon:"
    if amazon_code:
        amazon_line += f" at least 20% off after code: {amazon_code}"

    botslab_line = "🌟Official store:"
    if botslab_code:
        botslab_line += f" at least 15% off after code: {botslab_code}"

    copy = f"""Hi, thanks again for all your hard work on this project!
The video looks great, and you’re all set to go. Feel free to pick your preferred time to post. To make things easier, here are the final publishing details for your description/caption:

1. Add product link in the comment and pin it：
{amazon_line}
🔥Link: {amazon_link}
{botslab_line}
🔥Link: {botslab_link}
2. Add hashtags: {tags}
3. Tag:
    IG & facebook: @botslabofficial
    youtube: @Botslab750 (http://www.youtube.com/@botslab750)
    tiktok: @botslab_shop
4. Invite collaborator on IG: botslabofficial"""

    return {
        "product_code": product_code,
        "tags": tags,
        "copy": copy,
        "message": f"根据相关信息，生成回复话术如下：\n{copy}",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate publishing/caption copy for supported Botslab products.")
    parser.add_argument("--product", required=True)
    parser.add_argument("--amazon-link", required=True)
    parser.add_argument("--botslab-link", required=True)
    parser.add_argument("--amazon-code", default="")
    parser.add_argument("--botslab-code", default="")
    parser.add_argument("--json", action="store_true", help="Print structured JSON instead of the final message.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        rendered = render_copy(
            product=args.product,
            amazon_link=args.amazon_link,
            botslab_link=args.botslab_link,
            amazon_code=args.amazon_code,
            botslab_code=args.botslab_code,
        )
        if args.json:
            print(json.dumps(rendered, ensure_ascii=False, indent=2))
        else:
            print(rendered["message"])
        return 0
    except CopyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

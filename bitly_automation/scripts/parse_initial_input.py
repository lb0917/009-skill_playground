#!/usr/bin/env python3
"""Parse the user's initial Bitly automation message."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from typing import Any


URL_RE = re.compile(r"https?://[^\s，,；;。)）]+", re.IGNORECASE)

AMAZON_DOMAINS = {
    "amazon.com",
    "amazon.ca",
    "amazon.co.uk",
    "amazon.de",
    "amazon.fr",
    "amazon.es",
    "amazon.it",
    "amazon.co.jp",
    "amazon.com.au",
    "amazon.com.br",
    "amazon.com.mx",
    "amazon.nl",
    "amazon.pl",
    "amazon.se",
    "amazon.sg",
    "amazon.ae",
    "amazon.sa",
}
AMAZON_SHORT_DOMAINS = {"amzn.to"}
BOTSLAB_DOMAINS = {"botslabcam.com", "botslab.com"}

KOL_LABELS = ("kol名称", "kol", "达人名称", "达人", "creator", "influencer")
AMAZON_URL_LABELS = ("amazon链接", "amazon link", "亚马逊链接", "amazon平台的原始长链接")
BOTSLAB_URL_LABELS = ("botslab链接", "botslab link", "botslab独立站链接", "botslab独立站点上的原始长链接")
AMAZON_CODE_LABELS = ("amazon平台的促销代码", "amazon促销代码", "amazon code", "亚马逊促销代码")
BOTSLAB_CODE_LABELS = ("botslab独立站的促销代码", "botslab促销代码", "botslab code", "独立站促销代码")


def normalize_url(url: str) -> str:
    return url.strip().rstrip(".,，。;；")


def hostname_for(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return (parsed.hostname or "").lower().rstrip(".")


def host_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def classify_url(url: str) -> str:
    hostname = hostname_for(url)
    if any(host_matches(hostname, domain) for domain in BOTSLAB_DOMAINS):
        return "botslab"
    if any(host_matches(hostname, domain) for domain in AMAZON_DOMAINS):
        return "amazon"
    if any(host_matches(hostname, domain) for domain in AMAZON_SHORT_DOMAINS):
        return "amazon_short"
    return "unknown"


def normalize_label(label: str) -> str:
    label = re.sub(r"[（(].*?[）)]", "", label)
    return re.sub(r"\s+", "", label.strip().lower())


def parse_labeled_lines(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    label_groups = {
        "kol_name": KOL_LABELS,
        "amazon_url": AMAZON_URL_LABELS,
        "botslab_url": BOTSLAB_URL_LABELS,
        "amazon_promo_code": AMAZON_CODE_LABELS,
        "botslab_promo_code": BOTSLAB_CODE_LABELS,
    }

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or ("：" not in line and ":" not in line):
            continue
        if "：" in line:
            raw_label, raw_value = line.split("：", 1)
        else:
            raw_label, raw_value = line.split(":", 1)
        normalized_label = normalize_label(raw_label)
        for key, labels in label_groups.items():
            if any(normalized_label == normalize_label(label) for label in labels):
                result[key] = raw_value.strip()
                break
    return result


def infer_kol_from_unlabeled_text(text: str, urls: list[str]) -> str:
    cleaned = text
    for url in urls:
        cleaned = cleaned.replace(url, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" \t\r\n,，;；")
    if not cleaned:
        return ""

    first_line = cleaned.splitlines()[0].strip()
    if ":" in first_line or "：" in first_line:
        return ""

    # In simple free-form input, the KOL is usually the text before the first URL.
    first_url = URL_RE.search(text)
    if first_url:
        prefix = text[: first_url.start()].strip(" \t\r\n,，;；")
        if prefix and ":" not in prefix and "：" not in prefix:
            return prefix.split()[0] if len(prefix.split()) > 1 else prefix
    return first_line.split()[0]


def parse_initial_input(text: str) -> dict[str, Any]:
    labeled = parse_labeled_lines(text)
    urls = [normalize_url(match.group(0)) for match in URL_RE.finditer(text)]

    amazon_urls = [url for url in urls if classify_url(url) == "amazon"]
    amazon_short_urls = [url for url in urls if classify_url(url) == "amazon_short"]
    botslab_urls = [url for url in urls if classify_url(url) == "botslab"]
    unknown_urls = [url for url in urls if classify_url(url) == "unknown"]

    kol_name = labeled.get("kol_name") or infer_kol_from_unlabeled_text(text, urls)
    amazon_url = labeled.get("amazon_url") or (amazon_urls[0] if amazon_urls else "")
    botslab_url = labeled.get("botslab_url") or (botslab_urls[0] if botslab_urls else "")

    missing_required = []
    if not kol_name:
        missing_required.append("KOL名称")
    if not amazon_url:
        missing_required.append("Amazon链接")
    if not botslab_url:
        missing_required.append("Botslab链接")

    notes = []
    if amazon_short_urls and not amazon_url:
        notes.append("检测到 amzn.to 短链；生成新 Bitly 短链时应让用户提供 Amazon 原始长链接。")
    if len(amazon_urls) > 1:
        notes.append("检测到多个 Amazon 原始链接，请向用户确认使用哪一个。")
    if len(botslab_urls) > 1:
        notes.append("检测到多个 Botslab 链接，请向用户确认使用哪一个。")

    return {
        "ok": not missing_required,
        "kol_name": kol_name,
        "amazon_url": amazon_url,
        "botslab_url": botslab_url,
        "amazon_promo_code": labeled.get("amazon_promo_code", ""),
        "botslab_promo_code": labeled.get("botslab_promo_code", ""),
        "missing_required": missing_required,
        "detected_urls": [
            {"url": url, "kind": classify_url(url), "hostname": hostname_for(url)}
            for url in urls
        ],
        "unknown_urls": unknown_urls,
        "notes": notes,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse initial KOL/link/promo-code input.")
    parser.add_argument("--text", help="Raw user message. If omitted, stdin is used.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    text = args.text if args.text is not None else sys.stdin.read()
    result = parse_initial_input(text)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

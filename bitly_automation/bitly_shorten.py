#!/usr/bin/env python3
"""Create and verify Bitly short links for Botslab and Amazon URLs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


BITLY_API_URL = "https://api-ssl.bitly.com/v4/bitlinks"
DEFAULT_GROUP_GUID = "Bq5q3cm6vS4"
DEFAULT_BOTSLAB_DOMAIN = "botslabcam.com"
BOTSLAB_SOURCE_DOMAINS = {"botslabcam.com", "botslab.com"}
AMAZON_SHORT_DOMAIN = "amzn.to"


class ValidationError(ValueError):
    pass


def clean_field(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"{label} cannot be empty.")
    if "\n" in value or "\r" in value:
        raise ValidationError(f"{label} cannot contain line breaks.")
    return value


def build_title(creator_id: str, product_name: str, country_code: str) -> str:
    creator_id = clean_field(creator_id, "creator_id")
    product_name = clean_field(product_name, "product_name")
    country_code = clean_field(country_code, "country_code").upper()

    if not re.fullmatch(r"[A-Z]{2}", country_code):
        raise ValidationError("country_code must be exactly two English letters, for example US.")

    return f"{creator_id}-{product_name}-{country_code}"


def parse_hostname(long_url: str) -> str:
    parsed = urllib.parse.urlparse(long_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("long_url must start with http:// or https://.")
    if not parsed.hostname:
        raise ValidationError("long_url must include a valid hostname.")
    return parsed.hostname.lower().rstrip(".")


def classify_url(long_url: str, botslab_domain: str) -> tuple[str, str]:
    hostname = parse_hostname(long_url)
    botslab_domain = botslab_domain.lower().rstrip(".")

    botslab_source_domains = BOTSLAB_SOURCE_DOMAINS | {botslab_domain}
    if any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in botslab_source_domains
    ):
        return "botslab", botslab_domain

    amazon_domains = {
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
    if any(hostname == domain or hostname.endswith(f".{domain}") for domain in amazon_domains):
        return "amazon", AMAZON_SHORT_DOMAIN

    raise ValidationError(
        "Only botslabcam.com and amazon.com URLs are supported by this automation."
    )


def bitly_request(token: str, payload: dict[str, object]) -> dict[str, object]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BITLY_API_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
            detail = json.dumps(parsed, ensure_ascii=False)
        except json.JSONDecodeError:
            detail = body
        raise RuntimeError(f"Bitly API error HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Bitly API network error: {exc}") from exc


def domain_from_link(link: str) -> str:
    hostname = urllib.parse.urlparse(link).hostname
    if not hostname:
        raise ValidationError(f"Bitly response link has no hostname: {link}")
    return hostname.lower().rstrip(".")


def verify_response(
    response: dict[str, object],
    *,
    expected_domain: str,
    expected_title: str,
    expected_long_url: str,
) -> None:
    link = response.get("link")
    if not isinstance(link, str) or not link:
        raise ValidationError("Bitly response does not include a valid link.")

    actual_domain = domain_from_link(link)
    if actual_domain != expected_domain:
        raise ValidationError(
            f"Created link domain mismatch: expected {expected_domain}, got {actual_domain}."
        )

    actual_title = response.get("title")
    if actual_title != expected_title:
        raise ValidationError(
            f"Created link title mismatch: expected {expected_title!r}, got {actual_title!r}."
        )

    actual_long_url = response.get("long_url")
    if actual_long_url != expected_long_url:
        raise ValidationError(
            f"Created link long_url mismatch: expected {expected_long_url!r}, got {actual_long_url!r}."
        )


def make_payload(
    *,
    long_url: str,
    title: str,
    url_kind: str,
    group_guid: str,
    botslab_domain: str,
    force_new_link: bool,
) -> dict[str, object]:
    domain = botslab_domain if url_kind == "botslab" else "bit.ly"
    return {
        "long_url": long_url,
        "domain": domain,
        "group_guid": group_guid,
        "title": title,
        "force_new_link": force_new_link,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a Bitly short link and verify domain, title, and long URL."
    )
    parser.add_argument("long_url")
    parser.add_argument("--creator-id", required=True, help="达人ID")
    parser.add_argument("--product-name", required=True, help="产品名称")
    parser.add_argument("--country-code", required=True, help="Two-letter country code, e.g. US")
    parser.add_argument("--group-guid", default=DEFAULT_GROUP_GUID)
    parser.add_argument("--botslab-domain", default=DEFAULT_BOTSLAB_DOMAIN)
    parser.add_argument(
        "--token-env",
        default="BITLY_TOKEN",
        help="Environment variable that contains the Bitly access token.",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Allow Bitly to reuse an existing equivalent Bitlink. Default creates a new link.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate input and print the planned Bitly request without calling the API.",
    )
    args = parser.parse_args()

    try:
        long_url = clean_field(args.long_url, "long_url")
        title = build_title(args.creator_id, args.product_name, args.country_code)
        url_kind, expected_domain = classify_url(long_url, args.botslab_domain)
        payload = make_payload(
            long_url=long_url,
            title=title,
            url_kind=url_kind,
            group_guid=args.group_guid,
            botslab_domain=args.botslab_domain,
            force_new_link=not args.reuse_existing,
        )

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "mode": "dry_run",
                        "url_kind": url_kind,
                        "expected_short_domain": expected_domain,
                        "request_url": BITLY_API_URL,
                        "payload": payload,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        token = os.environ.get(args.token_env, "").strip()
        if not token:
            raise ValidationError(f"Set the Bitly token in environment variable {args.token_env}.")

        response = bitly_request(token, payload)
        verify_response(
            response,
            expected_domain=expected_domain,
            expected_title=title,
            expected_long_url=long_url,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "url_kind": url_kind,
                    "short_link": response["link"],
                    "bitlink_id": response.get("id"),
                    "title": response.get("title"),
                    "long_url": response.get("long_url"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (RuntimeError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

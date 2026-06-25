#!/usr/bin/env python3
"""Look up Botslab KOL records from a Feishu Bitable."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


DEFAULT_BASE_URL = "https://open.feishu.cn/open-apis"
DEFAULT_APP_TOKEN = "LV8CbCGYOaHDH2sAoybc16fjnVc"
DEFAULT_TABLE_ID = "tblqZSoSg3KJUMJU"
DEFAULT_VIEW_ID = "vewv77qXy0"

KOL_FIELD = "KOL名称"
PRODUCT_FIELD = "测评产品"
COUNTRY_FIELD = "国家"
FIELD_NAMES = [KOL_FIELD, PRODUCT_FIELD, COUNTRY_FIELD]

COUNTRY_CODE_ALIASES = {
    "us": "US",
    "usa": "US",
    "u.s.": "US",
    "u.s.a.": "US",
    "united states": "US",
    "united states of america": "US",
    "美国": "US",
    "ca": "CA",
    "canada": "CA",
    "加拿大": "CA",
    "gb": "GB",
    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "britain": "GB",
    "great britain": "GB",
    "英国": "GB",
    "de": "DE",
    "germany": "DE",
    "deutschland": "DE",
    "德国": "DE",
    "fr": "FR",
    "france": "FR",
    "法国": "FR",
    "es": "ES",
    "spain": "ES",
    "西班牙": "ES",
    "it": "IT",
    "italy": "IT",
    "意大利": "IT",
    "jp": "JP",
    "japan": "JP",
    "日本": "JP",
    "au": "AU",
    "australia": "AU",
    "澳大利亚": "AU",
    "br": "BR",
    "brazil": "BR",
    "巴西": "BR",
    "mx": "MX",
    "mexico": "MX",
    "墨西哥": "MX",
    "nl": "NL",
    "netherlands": "NL",
    "荷兰": "NL",
    "pl": "PL",
    "poland": "PL",
    "波兰": "PL",
    "se": "SE",
    "sweden": "SE",
    "瑞典": "SE",
    "sg": "SG",
    "singapore": "SG",
    "新加坡": "SG",
    "ae": "AE",
    "uae": "AE",
    "united arab emirates": "AE",
    "阿联酋": "AE",
    "sa": "SA",
    "saudi arabia": "SA",
    "沙特": "SA",
    "沙特阿拉伯": "SA",
}


class FeishuError(RuntimeError):
    """Raised when the Feishu API returns an error."""


def normalize_kol_name(value: str) -> str:
    value = stringify_cell(value).strip().lower()
    if value.startswith("@"):
        value = value[1:]
    return " ".join(value.split())


def stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value).strip()
    if isinstance(value, list):
        parts = [stringify_cell(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "full_name", "en_name", "value", "link", "url"):
            if key in value:
                text = stringify_cell(value[key])
                if text:
                    return text
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def request_json(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise FeishuError(f"HTTP {exc.code} from Feishu: {body}") from exc
    except urllib.error.URLError as exc:
        raise FeishuError(f"Feishu network error: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FeishuError(f"Feishu returned non-JSON response from {url}") from exc


def get_tenant_access_token(args: argparse.Namespace) -> str:
    existing = os.environ.get(args.tenant_token_env, "").strip()
    if existing:
        return existing

    app_id = os.environ.get(args.app_id_env, "").strip()
    app_secret = os.environ.get(args.app_secret_env, "").strip()
    if not app_id or not app_secret:
        raise FeishuError(
            f"Set {args.app_id_env} and {args.app_secret_env}, or provide a token in "
            f"{args.tenant_token_env}."
        )

    url = f"{args.base_url}/auth/v3/tenant_access_token/internal"
    response = request_json(
        url,
        method="POST",
        payload={"app_id": app_id, "app_secret": app_secret},
    )
    if response.get("code") != 0:
        raise FeishuError(f"Failed to get tenant_access_token: {json.dumps(response, ensure_ascii=False)}")
    token = response.get("tenant_access_token")
    if not isinstance(token, str) or not token:
        raise FeishuError("Feishu token response did not include tenant_access_token.")
    return token


def load_records_from_file(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise FeishuError("--offline-json must contain a JSON list or an object with an items list.")
    return data


def fetch_records(args: argparse.Namespace, token: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    page_token = ""
    page_size = max(1, min(args.page_size, 500))

    while True:
        query = {"page_size": str(page_size)}
        if page_token:
            query["page_token"] = page_token
        url = (
            f"{args.base_url}/bitable/v1/apps/{args.app_token}/tables/{args.table_id}"
            f"/records/search?{urllib.parse.urlencode(query)}"
        )
        payload: dict[str, Any] = {"field_names": FIELD_NAMES}
        if args.view_id:
            payload["view_id"] = args.view_id

        response = request_json(url, method="POST", token=token, payload=payload)
        if response.get("code") != 0:
            raise FeishuError(f"Feishu record search failed: {json.dumps(response, ensure_ascii=False)}")
        data = response.get("data") or {}
        items = data.get("items") or []
        if not isinstance(items, list):
            raise FeishuError("Feishu record search response data.items is not a list.")
        records.extend(items)

        has_more = bool(data.get("has_more"))
        page_token = str(data.get("page_token") or "")
        if not has_more or not page_token:
            break
        if args.limit and len(records) >= args.limit:
            records = records[: args.limit]
            break

    return records


def simplify_record(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    if not isinstance(fields, dict):
        fields = {}
    country = stringify_cell(fields.get(COUNTRY_FIELD))
    return {
        "record_id": record.get("record_id") or record.get("id") or "",
        "kol_name": stringify_cell(fields.get(KOL_FIELD)),
        "product_name": stringify_cell(fields.get(PRODUCT_FIELD)),
        "country": country,
        "country_code_guess": guess_country_code(country),
        "raw_fields": fields,
    }


def dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = value.strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def guess_country_code(country: str) -> str:
    text = stringify_cell(country).strip()
    if not text:
        return ""
    normalized = text.lower().replace("（", "(").replace("）", ")")
    normalized = " ".join(normalized.split())
    if normalized in COUNTRY_CODE_ALIASES:
        return COUNTRY_CODE_ALIASES[normalized]
    if len(text) == 2 and text.isalpha():
        return text.upper()
    return ""


def match_records(kol_name: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    simplified = [simplify_record(record) for record in records]
    target = normalize_kol_name(kol_name)

    exact = [record for record in simplified if normalize_kol_name(record["kol_name"]) == target]
    if exact:
        matches = exact
        match_mode = "exact"
        requires_user_confirmation = False
    else:
        fuzzy = []
        for record in simplified:
            candidate = normalize_kol_name(record["kol_name"])
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(a=target, b=candidate).ratio()
            if target in candidate or candidate in target or ratio >= 0.72:
                enriched = dict(record)
                enriched["similarity"] = round(ratio, 4)
                fuzzy.append(enriched)
        fuzzy.sort(key=lambda item: item.get("similarity", 0), reverse=True)
        matches = fuzzy
        match_mode = "fuzzy" if fuzzy else "none"
        requires_user_confirmation = bool(fuzzy)

    return {
        "ok": True,
        "query": kol_name,
        "normalized_query": target,
        "match_mode": match_mode,
        "requires_user_confirmation": requires_user_confirmation,
        "record_count": len(matches),
        "records": matches,
        "unique_kol_names": dedupe_nonempty([record["kol_name"] for record in matches]),
        "unique_products": dedupe_nonempty([record["product_name"] for record in matches]),
        "unique_countries": dedupe_nonempty([record["country"] for record in matches]),
        "unique_country_code_guesses": dedupe_nonempty(
            [record.get("country_code_guess", "") for record in matches]
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search configured Feishu Bitable records by KOL name.")
    parser.add_argument("kol_name", help="KOL name or handle. A leading @ is ignored for matching.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--app-token", default=DEFAULT_APP_TOKEN)
    parser.add_argument("--table-id", default=DEFAULT_TABLE_ID)
    parser.add_argument("--view-id", default=DEFAULT_VIEW_ID)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum record count to fetch.")
    parser.add_argument("--app-id-env", default="FEISHU_APP_ID")
    parser.add_argument("--app-secret-env", default="FEISHU_APP_SECRET")
    parser.add_argument("--tenant-token-env", default="FEISHU_TENANT_ACCESS_TOKEN")
    parser.add_argument(
        "--offline-json",
        help="Read Feishu-like records from a local JSON file instead of calling Feishu. Useful for tests.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.offline_json:
            records = load_records_from_file(args.offline_json)
        else:
            token = get_tenant_access_token(args)
            records = fetch_records(args, token)
        print(json.dumps(match_records(args.kol_name, records), ensure_ascii=False, indent=2))
        return 0
    except FeishuError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

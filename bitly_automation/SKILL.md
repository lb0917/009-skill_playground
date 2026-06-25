---
name: bitly-automation
description: Automate Botslab KOL Bitly short-link generation. Use when an agent sees requests about Bitly, bitly links, short links, 短链, 短链接, 链接缩短, 短链制作, 生成短链, Amazon/Botslab tracking links, or Botslab KOL link automation; the workflow collects KOL name, Amazon and Botslab long URLs, optional promo codes, looks up product/country data from Feishu Bitable, confirms details, calls bitly_shorten.py twice, validates supported products, and can generate platform-neutral publishing/caption copy.
---

# Bitly Automation

## Purpose

Use this skill to run the Botslab KOL Bitly short-link workflow end to end. Trigger it for Bitly, short-link, 短链, 短链接, 链接缩短, or short-link production requests. Keep the interaction stateful: do not skip required user confirmations, and do not generate final publishing/caption copy until the user has confirmed the KOL/product/country/URL/promo-code summary and the product has been confirmed as one of the five supported products.

## Runtime Requirements

Set credentials in environment variables. Never hard-code secrets in prompts, files, logs, or generated output.

- `FEISHU_APP_ID`: Feishu app ID.
- `FEISHU_APP_SECRET`: Feishu app secret.
- `BITLY_TOKEN`: Bitly API token used by `bitly_shorten.py`.

Default Feishu Bitable configuration:

- Base app token: `LV8CbCGYOaHDH2sAoybc16fjnVc`
- Table ID: `tblqZSoSg3KJUMJU`
- View ID: `vewv77qXy0`
- KOL field: `KOL名称`
- Product field: `测评产品`
- Country field: `国家`

Use `scripts/feishu_kol_lookup.py` for KOL lookup and `scripts/generate_publishing_copy.py` for final copy rendering. Use the sibling `bitly_shorten.py` script for Bitly generation.

## Workflow

### 1. Start And Collect Inputs

Start by sending this exact introduction in Chinese:

```text
hello我是你的bitly自动化小帮手，可以帮你自动制作短链。首先请你填写以下信息：
KOL名称：
Amazon链接：
Botslab链接：
amazon平台的促销代码（可选，留空视为没有）：
botslab独立站的促销代码（可选，留空视为没有）：
```

Wait until the user has provided all three required values:

- KOL name
- Amazon platform original long URL
- Botslab independent-site original long URL

The two promo-code values are optional. If the user leaves them blank, omits them, or writes `无`, treat them as no promo code. If any required item is missing, ask only for the missing required item(s). Do not proceed to Feishu lookup until all three required values are present.

Users do not have to follow the form exactly. If they send free-form text such as `alicehome https://www.amazon.com/dp/B0TEST123 https://botslabcam.com/products/w101-window-camera`, parse it automatically. Prefer this helper:

```bash
python3 scripts/parse_initial_input.py --text "<raw user message>"
```

The parser extracts the KOL name, classifies Amazon URLs by Amazon domains, and classifies Botslab URLs by `botslabcam.com` or `botslab.com`. If it can confidently identify one Amazon original URL and one Botslab URL, use them without asking the user to reformat. If a required field is missing, multiple same-platform links are found, or a URL cannot be classified, ask only for the specific missing/unclear item. If the parser detects an `amzn.to` short link instead of an Amazon original long URL, ask the user for the original Amazon URL before calling `bitly_shorten.py`.

### 2. Look Up The KOL In Feishu

Normalize the user-provided KOL name by trimming spaces and ignoring a leading `@`. Search Feishu with:

```bash
python3 scripts/feishu_kol_lookup.py "<KOL name>"
```

The script performs exact normalized matching first. If no exact match exists, it falls back to fuzzy matching. When fuzzy matching returns records, confirm the matched KOL identity with the user before using those records.

Interpret lookup results:

- Zero records: ask the user to check the KOL name or provide product/country manually.
- One record: use that record's `测评产品` and `国家`.
- Multiple records: collect all unique `测评产品` values and ask: `请选择你与该达人合作的产品的名称`. Use the product the user selects.
- If multiple records have inconsistent countries, ask the user to choose or confirm the correct country.
- If the chosen/only record lacks product or country, ask the user for the missing information and remind them to补全飞书多维表格中的缺失信息. Continue after the user supplies the missing value.

Country values passed to `bitly_shorten.py` must be two-letter English country codes such as `US`. Prefer the lookup script's `country_code_guess` or `unique_country_code_guesses` when present. If Feishu stores a country name such as `United States` or `美国`, convert it when obvious. If not obvious, ask the user for the two-letter country code.

### 3. Confirm All Resolved Information

After resolving KOL, product, country, both long URLs, and promo codes, send a confirmation summary:

```text
根据您提供的信息，我搜索飞书多维表格后得到的所有相关信息汇总如下：
KOL名称：xxx
合作产品：xxx
KOL国家：xxx
amazon长链接：xxx
botslab长链接：xxx
amazon促销代码：xxx
botslab促销代码：xxx
请您确认，如无误请回答确认，如有问题请指出哪一条有问题并给出正确的信息。
```

Use a generous confirmation rule. If the user replies with an affirmative phrase such as `确认`, `确认无误`, `OK`, `ok`, `好的`, `好`, `行`, `可以`, `没问题`, `正确`, `对`, `yes`, `y`, or similar, treat it as confirmation and proceed. If the user corrects any value, update the stored information and repeat this confirmation step. Do not proceed until the user confirms.

### 4. Generate Two Bitly Links

Call `bitly_shorten.py` twice from this skill directory, once for Amazon and once for Botslab:

```bash
python3 bitly_shorten.py "<amazon_long_url>" --creator-id "<KOL name>" --product-name "<product>" --country-code "<country_code>"
python3 bitly_shorten.py "<botslab_long_url>" --creator-id "<KOL name>" --product-name "<product>" --country-code "<country_code>"
```

Parse the JSON output and capture `short_link`. After both succeed, tell the user:

```text
已经根据您提供的相关信息生成amazon短链接：xxx，生成botslab独立站短链接：xxx
```

If a Bitly call fails, show the relevant error and stop for correction. Do not continue to publishing-copy generation with missing short links.

### 5. Confirm Supported Product And Tags

The final publishing/caption copy can only be generated for one of:

- `G980H`
- `G300`
- `W510`
- `R810`
- `W101（window cam）`

Infer the product code from the confirmed product name. For `W101（window cam）`, matching either `W101` or `window cam` is enough, and matching is case-insensitive. If one of the five supported products is detected, ask:

```text
我判断您与达人合作的选品是xxx，这个信息正确吗？如正确请回复”正确“，如不正确请告诉我正确的选品是G980H，G300,W510,R810,W101（window cam）中的哪一个？
```

Apply the same generous confirmation rule here: `OK`, `好的`, `行`, `可以`, `没问题`, `正确`, and similar replies all mean the user has confirmed. If the user corrects it to one of the five supported products, use the corrected product. If the user insists on any other product, stop the publishing-copy generation flow and explain that the current automation only supports those five products.

Use these exact tag mappings, rendered as plain text:

- `G980H`: `#Botslab #Botslabdashcam #BotslabG980H #dashcam`
- `G300`: `#Botslab #Botslabdashcam #BotslabG300H #dashcam`
- `W510`: `#Botslab #BotslabW510 #securitycamera`
- `R810`: `#Botslab #BotslabR810 #doorbell`
- `W101（window cam）`: `#Botslab #Botslabwindowcamera #BotslabW101 #windowcamera`

### 6. Generate Publishing Copy

Use the helper script:

```bash
python3 scripts/generate_publishing_copy.py \
  --product "<supported_product>" \
  --amazon-link "<amazon_short_link>" \
  --botslab-link "<botslab_short_link>" \
  --amazon-code "<amazon_promo_or_无>" \
  --botslab-code "<botslab_promo_or_无>"
```

Return the rendered copy to the user with the prefix:

```text
根据相关信息，生成回复话术如下：
```

When a promo code is absent, remove the corresponding `at least ... off after code:` phrase entirely. Always include both short links, the product tags, and the platform tag/account instructions.

## Guardrails

- Keep all user-facing workflow prompts in Chinese until the final English publishing template text.
- Never expose Feishu or Bitly credentials.
- Do not skip the Feishu fuzzy-match confirmation when exact matching fails.
- Treat confirmations generously: `OK`, `好的`, `行`, `可以`, `没问题`, `正确`, and similar affirmative replies should all count.
- Do not skip the full information confirmation before creating Bitly links.
- Do not generate final publishing/caption copy for unsupported products.
- Preserve `G300` tag as `#BotslabG300H`.
- Render `W101（window cam）` tags as plain hashtags, not Markdown links.

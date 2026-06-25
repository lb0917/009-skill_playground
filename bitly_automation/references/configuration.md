# Configuration Reference

Use this only when configuring the automation runtime or debugging credentials.

## Environment Variables

- `FEISHU_APP_ID`: Feishu custom app ID.
- `FEISHU_APP_SECRET`: Feishu custom app secret.
- `FEISHU_TENANT_ACCESS_TOKEN`: Optional pre-issued tenant access token. If set, `scripts/feishu_kol_lookup.py` uses it instead of exchanging app credentials.
- `BITLY_TOKEN`: Bitly access token used by `bitly_shorten.py`.

## Feishu Defaults

- API base URL: `https://open.feishu.cn/open-apis`
- Bitable app token: `LV8CbCGYOaHDH2sAoybc16fjnVc`
- Table ID: `tblqZSoSg3KJUMJU`
- View ID: `vewv77qXy0`
- KOL field: `KOL名称`
- Product field: `测评产品`
- Country field: `国家`

Override defaults with command-line flags on `scripts/feishu_kol_lookup.py` if the Bitable location changes.

## Bitly Script

`bitly_shorten.py` must be run from this directory or addressed by absolute path. It expects:

```bash
python3 bitly_shorten.py "<long_url>" \
  --creator-id "<KOL name>" \
  --product-name "<product>" \
  --country-code "<two-letter country code>"
```

It reads the token from `BITLY_TOKEN` by default and prints JSON containing `short_link` on success.

# 009 Skill Playground

This repository is a playground for small reusable automation skills.

## Layout

Each top-level folder is one standalone skill package:

```text
009-skill_playground/
├── bitly_automation/
│   ├── SKILL.md
│   ├── bitly_shorten.py
│   ├── scripts/
│   ├── references/
│   └── agents/
├── another_skill/
│   └── ...
└── README.md
```

Keep each skill self-contained so it can be copied, installed, or connected to an agent independently.

## Current Skills

- `bitly_automation`: Generates Bitly short links for Botslab KOL workflows and renders WhatsApp publishing copy.

## Secret Handling

Do not commit API keys or app secrets. Skills should read credentials from environment variables such as `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, and `BITLY_TOKEN`.

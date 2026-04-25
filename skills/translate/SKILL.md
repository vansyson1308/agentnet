---
name: translate
version: 1.0
description: Dịch văn bản giữa các ngôn ngữ
price: 3
---

# Translation Skill

Dịch văn bản giữa các ngôn ngữ.

## Input Schema

```json
{
  "type": "object",
  "properties": {
    "text": {"type": "string"},
    "source_lang": {"type": "string", "default": "auto"},
    "target_lang": {"type": "string", "default": "en"}
  },
  "required": ["text"]
}
```

## Output Schema

```json
{
  "type": "object",
  "properties": {
    "translated_text": {"type": "string"},
    "source_lang": {"type": "string"},
    "target_lang": {"type": "string"}
  }
}
```

## Code

```python
import httpx

async def handle(input_data: dict) -> dict:
    text = input_data.get("text", "")
    source = input_data.get("source_lang", "auto")
    target = input_data.get("target_lang", "en")

    # Sử dụng LibreTranslate public instance
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://libretranslate.de/translate",
            json={
                "q": text,
                "source": source,
                "target": target,
                "format": "text",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    return {
        "translated_text": data.get("translatedText", ""),
        "source_lang": data.get("detectedLanguage", {}).get("language", source),
        "target_lang": target,
    }
```

---
name: image_gen
version: 1.0
description: Tạo ảnh từ text prompt sử dụng Pollinations.AI API (free, không cần key)
price: 5
---

# Image Generation Skill

Tạo ảnh từ text prompt sử dụng Pollinations.AI — free, không cần API key.

## Input Schema

```json
{
  "type": "object",
  "properties": {
    "prompt": {"type": "string", "description": "Mô tả ảnh cần tạo"},
    "width": {"type": "integer", "default": 1024},
    "height": {"type": "integer", "default": 1024}
  },
  "required": ["prompt"]
}
```

## Output Schema

```json
{
  "type": "object",
  "properties": {
    "image_url": {"type": "string"},
    "prompt": {"type": "string"}
  }
}
```

## Usage

```bash
# Gọi API
curl -X POST https://agentnet.io.vn/v1/tasks/ \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"agent_id":"TARGET_AGENT","capability":"image_gen","input":{"prompt":"a cat wearing a hat"}}'
```

## Code

```python
import httpx

async def handle(input_data: dict) -> dict:
    prompt = input_data.get("prompt", "default prompt")
    width = input_data.get("width", 1024)
    height = input_data.get("height", 1024)

    url = f"https://image.pollinations.ai/prompt/{prompt.replace(' ', '%20')}?width={width}&height={height}"

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()

    return {
        "image_url": str(resp.url),
        "prompt": prompt,
    }
```

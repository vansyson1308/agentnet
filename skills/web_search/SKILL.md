---
name: web_search
version: 1.0
description: Tìm kiếm thông tin trên web sử dụng Tavily API
price: 2
---

# Web Search Skill

Tìm kiếm web và trả về kết quả. Cần Tavily API key.

## Input Schema

```json
{
  "type": "object",
  "properties": {
    "query": {"type": "string", "description": "Từ khóa tìm kiếm"},
    "limit": {"type": "integer", "default": 5}
  },
  "required": ["query"]
}
```

## Output Schema

```json
{
  "type": "object",
  "properties": {
    "results": {"type": "array"},
    "query": {"type": "string"}
  }
}
```

## Configuration

```bash
export TAVILY_API_KEY="tvly-..."
```

## Code

```python
import os
import httpx

async def handle(input_data: dict) -> dict:
    query = input_data.get("query", "")
    limit = input_data.get("limit", 5)
    api_key = os.environ.get("TAVILY_API_KEY", "")

    if not api_key:
        return {"error": "TAVILY_API_KEY not set", "query": query, "results": []}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": limit},
        )
        resp.raise_for_status()
        data = resp.json()

    return {
        "results": data.get("results", []),
        "query": query,
    }
```

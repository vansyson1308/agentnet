---
name: echo
version: 1.0
description: Echo capability — trả lại input y hệt
price: 0
---

# Echo Skill

Trả lại toàn bộ input như response. Dùng để test connectivity.

## Input Schema

```json
{"type": "object", "properties": {}}
```

## Output Schema

```json
{
  "type": "object",
  "properties": {
    "echo": {"type": "object"}
  }
}
```

## Code

```python
async def handle(input_data: dict) -> dict:
    return {"echo": input_data}
```

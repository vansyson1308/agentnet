# AgentNet Skills Marketplace

Skills là code templates cho AI agents, mỗi capability đi kèm instructions + code mẫu.
Lấy ý tưởng từ Coinbase Agentic Wallet Skills — `npx skills add coinbase/agentic-wallet-skills`.

## Danh sách skills có sẵn

| Skill | Capability | Mô tả |
|-------|-----------|-------|
| `echo` | echo | Trả lại input — test connectivity |
| `reverse` | reverse | Đảo ngược chuỗi |
| `count_words` | count_words | Đếm số từ |
| `uppercase` | uppercase | Viết hoa toàn bộ text |
| `image_gen` | image_gen | Generate ảnh từ prompt (via Pollinations.AI) |
| `web_search` | web_search | Tìm kiếm web (via Tavily) |
| `translate` | translate | Dịch văn bản |

## Cấu trúc một skill

Mỗi skill là 1 file markdown trong thư mục này.

Ví dụ:

### `echo/SKILL.md`

```markdown
---
name: echo
version: 1.0
description: Echo capability — trả lại input y hệt
price: 0
---

# Echo Skill

Trả lại toàn bộ input như response.

## Input Schema
```json
{"type": "object", "properties": {}}
```

## Output Schema
```json
{"type": "object", "properties": {}}
```

## Python Handler
```python
async def handle(input_data: dict) -> dict:
    return {"echo": input_data}
```
```

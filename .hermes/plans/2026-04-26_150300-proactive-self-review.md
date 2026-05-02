# Proactive Self-Review — Agent tự review performance metrics

## Goal
AgentNet agents tự động review performance metrics của chính mình định kỳ (không chỉ khi task fail) và sinh improvement proposals proactive.

## Current context
- Worker đã chạy reflection loop tạo proposals từ **task failure** ✅
- Có sẵn `/v1/agents/{id}/reputation` endpoint trả về metrics (success_rate, avg_response_time, verify_score, reputation_tier)
- Có sẵn `/v1/improvements/` endpoint để create proposals
- `ProposalSource` enum có sẵn `SELF_REFLECTION = "self_reflection"`
- AgentNet có ~4 agents active (openclaw-workhorse, hermes-brain, các agent simulation)

## Approach
Thêm một **scheduled job** trong worker chạy mỗi `REFLECTION_LOOP_INTERVAL_SEC` (hiện 300s = 5 phút) nhưng chức năng khác:
- Không chỉ xử lý task fail
- Query agents có metrics degradation (success_rate giảm, avg_response_time tăng, hoặc reputation_tier thấp)
- Gọi LLM (DeepSeek) prompt: "Based on this agent's metrics, what should it improve?"
- Tạo ImprovementProposal với source=SELF_REFLECTION

## Step-by-step

1. **Đọc code hiện tại**: `services/worker/app/reflection_loop.py` — `run_reflection_loop()` hiện chỉ xử lý task failed/timeout
2. **Thêm function mới**: `run_self_review()` trong cùng file, query agents metrics từ DB
3. **Metrics threshold**: success_rate < 0.8, avg_response_time > 5000ms, reputation_tier == "unranked" (và đã có task), verify_score < 50
4. **LLM prompt**: "Agent {name} has success_rate={x}, avg_response_time={y}ms, reputation_tier={z}. Propose one concrete improvement."
5. **INSERT improvement_proposals** với source=self_reflection
6. **Gọi từ worker main loop**: sau reflection loop, gọi `run_self_review(db)`
7. **Build + restart worker**
8. **Verify**: `curl /v1/improvements/?source=self_reflection` có kết quả sau 1 cycle

## Files to change
- `services/worker/app/reflection_loop.py` — thêm `run_self_review()` + cập nhật `run_reflection_loop()` gọi nó
- Không cần migration (DB table đã có)
- Không cần .env mới (dùng REFLECTION_LOOP_INTERVAL_SEC sẵn)

## Tests / validation
```bash
# Verify proposal created with self_reflection source
curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:8000/v1/improvements/?source=self_reflection&limit=5"
```
Expect: 1+ proposals với source=self_reflection

## Risks
- LLM call cost: mỗi cycle gọi DeepSeek cho mỗi agent cần review → có thể 4-5 calls/5phút = ~$0.02/ngày
- False positives: agent mới chưa có task history (`total_tasks_completed=0`) không nên review

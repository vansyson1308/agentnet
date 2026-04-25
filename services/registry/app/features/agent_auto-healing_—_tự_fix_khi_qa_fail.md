# Agent Auto-Healing — tự fix khi QA fail

Hiện tại Builder gửi code → QAAgent test → nếu fail thì chỉ báo cáo.

**Đề xuất:**
1. Khi QA report FAILED, Builder tự động phân tích lỗi
2. Fix code → gửi lại review_request
3. Loop tối đa 3 lần, nếu vẫn fail thì escalate lên human

**Priority:** HIGH — cốt lõi của autonomous agents
**Effort:** ~1h

---
Implemented by Hermes_Builder at Sat Apr 25 04:32:53 2026
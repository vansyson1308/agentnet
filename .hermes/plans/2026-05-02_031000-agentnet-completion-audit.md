# AgentNet Full-Stack Agent Economy — Completion Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Đưa AgentNet từ trạng thái "hệ thống hoạt động về mặt kỹ thuật nhưng còn bug + gap" thành **full-stack agent economy hoàn chỉnh, sẵn sàng cho production use bởi người dùng thực**.

**Architecture:** Đây là bản audit cuối cùng để xác định chính xác những gì còn thiếu giữa hiện trạng và "full-stack agent economy". Sau đó fix từng gap một, verify end-to-end, commit push.

**Hiện trạng (đã xác minh qua DB + API audit):**
- 31 agents đăng ký, 137 task sessions, 6 offers, 62 wallets, 5 transactions completed
- Escrow flow hoạt động (lock → start → confirm → release)
- Reputation real-time cập nhật sau confirm
- Multi-agent collaboration (offer → counter → accept → task) có code nhưng **bị 2 bug blocking**
- Pipeline (Planner → Builder → QA) đang tắt (Planner inactive)
- 10 agents đang `unverified` (không block task creation — chấp nhận unverified)
- Catalog 12 services
- Payment service hoạt động (wallets, transactions, approval requests)
- Dashboard + Metaverse render OK
- Staging environment đầy đủ (registry + payment + dashboard + worker)
- Security headers + rate limit OK
- **2 critical bugs vừa phát hiện trong E2E demo** (xem bên dưới)

---

## GAP ANALYSIS: Còn thiếu gì để là "full-stack agent economy"?

### Gap 1 — Bug: Accept offer bị chặn sai
**File:** `services/registry/app/api/routes/offers.py:273`
**Bug:** `accept_offer` check `current_agent.id != offer.to_agent_id` nhưng `_resolve_offer_agent` nhận `caller_agent_id` (là agent đang gọi API), trong khi flow đúng là **callee** (người nhận offer) mới là người accept.  

Khi user dùng `caller_agent_id=EID` (escrow-callee-test), `_resolve_offer_agent` trả về đúng callee agent. Nhưng check dòng 273 lại bảo "Only the recipient can accept" — thực tế callee agent **là** recipient.  

**Root cause:** `_resolve_offer_agent` được gọi với `caller_agent_id=agent_id` — tham số này đại diện cho "agent đang thực hiện hành động", không phải "người gửi offer". Logic check đúng nhưng tên biến gây nhầm lẫn. Cần debug xem dòng 273 thực sự trả về gì.

### Gap 2 — Bug: TaskCreate schema yêu cầu `capability` phải có trong callee's capabilities
**File:** `services/registry/app/api/routes/tasks.py:183-194`
**Bug:** Task creation check `callee_agent.capabilities` — nhưng escrow-callee-test không có capability "test" trong capabilities list.  

**Impact:** Không thể tạo task nếu callee chưa advertise capability đó. Đây là design choice hợp lý (chỉ giao task cho agent có capability), nhưng cần **document rõ** + đảm bảo agent registration flow cho phép add capabilities dễ dàng.

### Gap 3 — Pipeline chết (Planner inactive)
**File:** systemd `hermes-planner.service`
**Bug:** Planner không chạy → backlog không được xử lý → không có task mới được dispatch → không có hoạt động tự động.  
Có 4 backlog items đã done (QA-TEST-001 + 3 APP items), nhưng 0 items open. Backlog cạn. Đây có thể là lý do Planner tắt (không có gì để làm).

### Gap 4 — User thật (sonnv.hd34@gmail.com) không sở hữu agent nào
**DB state:** sonnv.hd34@gmail.com có 0 agents. Tất cả agents thuộc về hermes-bot@duybui.dev, annhien.dev@gmail.com, hoặc demo users.  

**Impact:** Andrew không thể đăng nhập và test hệ thống như một user thực. Cần tạo ít nhất 1 agent cho sonnv user.

### Gap 5 — Payment service chưa có escrow tracking endpoint
**File:** payment service (`/v1/transactions/`, `/v1/wallets/`)
**Gap:** Không có endpoint để query "escrow status của 1 task" từ payment service. Tất cả escrow tracking phải qua registry. Payment service nên expose escrow status per task.

### Gap 6 — Không có on-chain settlement (crypto)
**Gap:** Toàn bộ economy là internal credits. Chưa có cầu nối ra crypto (USDC on Base/Arbitrum). Đây là "full-stack" thực sự — internal marketplace + on-chain settlement.

### Gap 7 — Không có public signup flow end-to-end
**Gap:** User registration có (`/v1/auth/user/register`), nhưng chưa có flow hoàn chỉnh: register → verify email → create agent → fund wallet → browse marketplace → create task. Tất cả đang phải làm thủ công qua API.

### Gap 8 — Staging chưa được test độc lập
**Gap:** Staging environment (ports 8100/8101/8180) chạy nhưng chưa được verify độc lập với production. Cần ít nhất 1 E2E test trên staging.

### Gap 9 — OpenClaw Workhorse + Hermes Brain chưa được tích hợp thực sự
**Gap:** 2 agent flagship (hermes-brain + openclaw-workhorse) không thuộc về user nào có thể test được. Chúng là agent "mồ côi" — tồn tại trong DB nhưng không ai sở hữu để ra task.

---

## PLAN: 4 Phases

### Phase A: Bug fixes (critical blocking)
**Mục tiêu:** Sửa 2 bug blocking E2E flow để demo được toàn bộ flow: offer → accept → task → escrow → confirm → reputation.

| Task | File | Bug |
|------|------|-----|
| A1 | `offers.py:221-290` | Accept offer check — debug + fix để callee accept được original offer |
| A2 | `tasks.py:183-194` | Thêm capability "test" cho escrow-callee-test HOẶC nới lỏng check capability khi có offer_id |
| A3 | DB | Gán ít nhất 1 agent cho sonnv.hd34@gmail.com để Andrew test được |
| A4 | DB | Active tất cả agents đang unverified (10 agents) |

### Phase B: E2E verification toàn bộ flow
**Mục tiêu:** Chạy full flow từ registration → marketplace → task → escrow → reputation, verify từng bước.

| Task | Mô tả |
|------|-------|
| B1 | Register user mới → verify email → login |
| B2 | Tạo agent với capabilities → fund wallet (1000 credits) |
| B3 | Browse marketplace → discover agent khác |
| B4 | Offer → counter → accept → create task (escrow) → start → confirm |
| B5 | Verify escrow release (caller -10, callee +10, platform fee) |
| B6 | Verify reputation cập nhật real-time |
| B7 | Verify task hiển thị trên dashboard |
| B8 | Repeat trên staging environment (port 8100) |

### Phase C: Pipeline revival
**Mục tiêu:** Khởi động lại autonomous pipeline để backlog được xử lý tự động.

| Task | Mô tả |
|------|-------|
| C1 | Kiểm tra systemd hermes-planner service |
| C2 | Thêm 1 backlog item test để verify pipeline hoạt động |
| C3 | Verify Planner → Builder → QA → commit → push tự động |

### Phase D: Final hardening
**Mục tiêu:** Đảm bảo không còn edge case + security gap.

| Task | Mô tả |
|------|-------|
| D1 | Audit lại toàn bộ endpoint auth (đảm bảo không public leak) |
| D2 | Test edge cases: double-spend, overspend, refund, timeout, concurrent confirm |
| D3 | Verify security headers + rate limit trên tất cả endpoint |
| D4 | Lightpanda verify dashboard + metaverse render đúng |
| D5 | Commit + push tất cả fixes |
| D6 | Cập nhật skill `agentnet-dashboard-status` với trạng thái cuối cùng |

---

## Success Criteria (cái gì xong thì mới được confirm)

- [ ] Andrew (sonnv.hd34@gmail.com) login được và thấy agent của mình
- [ ] Full flow: register → create agent → fund → browse → offer → accept → task → escrow → confirm → reputation — **tất cả PASS tự động**
- [ ] Staging environment pass cùng flow
- [ ] Pipeline chạy tự động (Planner active, xử lý backlog)
- [ ] 0 bug blocking, 0 security leak
- [ ] Dashboard render đúng (lightpanda verified)
- [ ] Commit push sạch

---

## Files likely to change
- `services/registry/app/api/routes/offers.py` — accept bug fix
- `services/registry/app/api/routes/tasks.py` — capability check relaxation
- Database — agent assignment + activation
- `services/payment/app/` — nếu cần thêm escrow tracking endpoint
- `/opt/agentnet/AGENT_BACKLOG.md` — thêm test item
- Systemd services — hermes-planner restart
- `~/.hermes/skills/devops/agentnet-dashboard-status/SKILL.md` — update

# Plan: World-Class QA Audit + Pipeline Fix + End-to-End User Flow

**Date:** 2026-05-01
**Author:** Hermes — World-Class Tester Mode
**Goal:** Audit 3 trụ cột AgentNet, fix bugs, chứng minh full end-to-end flow hoạt động

---

## Tổng quan: 3 phần audit

### Part A: Pipeline Automation Audit (closed-loop)
- Planner v5 → Builder v6 → QA v7
- Verify pipeline thực sự hoạt động hay fake
- Fix bugs nếu có

### Part B: APP Protocol Audit (AB-415→418)
- Catalog, ScopedToken, Projects, Orchestrator
- Route bugs, edge cases, integration test

### Part C: End-to-End User Flow (POV người dùng thật)
- Register → Login → Create Agent → Fund Wallet → Post Task → Escrow → Verify → Release
- Demo script để chứng minh

---

## Phát hiện ban đầu (pre-audit)

### Bug #1: Duplicate Planner + Builder processes
- 2 Planner instances: PID 2992957 (systemd) + PID 3033881 (orphan từ Apr28)
- 2 Builder instances: PID 2984914 + PID 2992956
- **Impact**: Double-dispatch tasks, race condition

### Bug #2: Planner stuck loop AB-411 (7 lần dispatch, 0 lần done)
- AB-411 dispatched 7 lần từ 00:56 đến 01:26, không completion
- QA không confirm PASS/FAIL

### Bug #3: Catalog route bug
- `/v1/catalog/services` không tồn tại thành route riêng
- `/v1/catalog/{service_id}` bắt "services" làm UUID → 422 error
- `/v1/catalog` chỉ trả flat array, không có provider grouping

### Bug #4: Catalog seed data only 1 provider + 1 service
- Lẽ ra phải 5 providers (Cloudflare, Vultr, GitHub, HuggingFace, AgentNet) + 12 services
- Có thể seed script chạy mỗi lần container restart, hoặc data bị mất

### Bug #5: SHIP_LOG không được update tự động
- Chỉ có dispatch log, không có build done / QA pass log
- Storyteller v4 đọc SHIP_LOG — sẽ chỉ thấy dispatch, không thấy kết quả

---

## Part A: Pipeline Automation Audit

### A1. Kiểm tra từng thành phần

| Component | Check | Expected | Current |
|-----------|-------|----------|---------|
| Planner v5 | Đọc backlog mỗi 30s | Poll AGENT_BACKLOG.md | ✅ Running but 2 instances |
| Planner v5 | Enrich task (DeepSeek + OpenClaw) | Thêm spec, files_to_modify, acceptance | ❓ Cần verify |
| Planner v5 | Dispatch to Builder qua AgentNet Chat | POST /v1/chat | ❓ Cần verify |
| Builder v6 | Nhận spec từ AgentNet Chat | GET /v1/chat/threads | ❓ Cần verify |
| Builder v6 | Gọi DeepSeek codegen | Tạo file + commit | ❓ Cần verify |
| Builder v6 | Git commit + push | Commit SHA trong log | ❓ Partial — commits là manual |
| QA v7 | Chạy acceptance bash one-liner | PASS/FAIL output | ❓ Cần verify |
| QA v7 | PATCH Paperclip status | Cập nhật issue status | ❓ Cần verify |

### A2. Fix plan

1. **Kill duplicate processes** — chỉ giữ 1 Planner systemd + 1 Builder systemd
2. **Verify Planner thực sự đọc backlog + enrich** — tail log
3. **Verify Builder thực sự nhận task + codegen** — check thread messages
4. **Verify QA thực sự chạy test + báo cáo** — check Paperclip status
5. **Fix nếu bất kỳ step nào hỏng**
6. **Test pipeline với 1 task nhỏ thực tế**

---

## Part B: APP Protocol Audit (AB-415→418)

### B1. Kiểm tra routes

| Route | Method | Expected | Check |
|-------|--------|----------|-------|
| /v1/catalog | GET | Providers grouped with services | ❌ Flat array, only 1 provider |
| /v1/catalog/{service_id} | GET | Service detail by UUID | ❌ Conflict with /v1/catalog/services |
| /v1/tokens | POST | Create scoped token | ❓ Test |
| /v1/projects | POST/GET | CRUD projects | ❓ Test |
| /v1/orchestrator/oauth/authorize | POST | OAuth authorize | ❓ Test |
| /v1/orchestrator/oauth/token | POST | Token exchange | ❓ Test |
| /v1/orchestrator/provision | POST | Direct provision | ❓ Test |

### B2. Fix plan

1. **Fix `/v1/catalog/services` route** — thêm route riêng hoặc fix conflict với `{service_id}`
2. **Re-seed catalog data** — 5 providers + 12 services
3. **Integration test APP flow**: tạo token → tạo project → provision resource
4. **Edge case test**: token expired, invalid spending cap, duplicate project

---

## Part C: End-to-End User Flow (POV người dùng thật)

### User Story
"Một developer mới tên 'Alex' muốn dùng AgentNet để thuê agent code hộ 1 tính năng"

### Flow từng bước (phải verify từng bước qua curl/Lightpanda)

```
1. Alex truy cập https://agentnet.io.vn/landing
   → Thấy J.A.R.V.I.S. hero, "Explore Marketplace" CTA

2. Alex click "Initialize" → /register
   → Form glass card, điền email + password

3. Sau register → redirect /login → đăng nhập
   → Vào dashboard index, thấy wallet balance

4. Fund wallet → +1000 dev credits
   → Balance update real-time

5. Tạo agent → /agent/register
   → Điền name, capabilities, endpoint

6. Vào Marketplace → thấy agent của mình trong danh sách

7. Post task: "Add endpoint /api/hello"
   → Task created, status: pending

8. Escrow lock: credits bị giữ

9. Builder picks task → codegen → commit

10. QA runs test → PASS

11. Escrow release → credits chuyển cho agent

12. Verify: check git log có commit mới, check endpoint hoạt động
```

### Demo script
Viết 1 bash script chạy toàn bộ flow trên, mỗi step có assertion

---

## Files Likely Changed

| File | Reason |
|------|--------|
| `/opt/agentnet/services/registry/app/routes/catalog.py` | Fix route conflict |
| `/opt/agentnet/services/registry/app/seed_catalog.py` | Re-seed data |
| `/opt/agentnet/hermes_planner_v5.py` | Fix duplicate process |
| `/opt/agentnet/hermes_builder_v6.py` | Fix duplicate process |
| `/opt/agentnet/hermes_qaagent_v7.py` | Verify/fix |
| `/opt/agentnet/AGENT_BACKLOG.md` | Clean up stuck items |
| `.hermes/plans/` | Plan this |

---

## Verification Checklist

- [ ] Chỉ 1 Planner process chạy
- [ ] Chỉ 1 Builder process chạy
- [ ] Planner đọc backlog + enrich thành công
- [ ] Builder nhận spec + codegen + commit thành công
- [ ] QA chạy test + báo cáo PASS/FAIL thành công
- [ ] /v1/catalog/services hoạt động
- [ ] Catalog seed 5 providers + 12 services
- [ ] APP flow: token → project → provision hoạt động
- [ ] End-to-end user flow: register → agent → task → escrow → release
- [ ] Lightpanda verify tất cả page public

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| Duplicate Planner/Builder gây race condition | High | Kill orphan, verify systemd unit chỉ start 1 |
| Catalog seed bị mất sau restart | Medium | Check seed logic, add idempotency |
| QA không thực sự test endpoint | High | Verify QA log output, test manually |
| User flow bị gián đoạn bởi auth | Medium | Lấy token từ login flow, dùng trong các step tiếp |

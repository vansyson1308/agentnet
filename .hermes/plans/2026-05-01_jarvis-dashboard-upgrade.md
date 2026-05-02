# Plan: J.A.R.V.I.S. Dashboard Upgrade — Flask Control Panel

**Date:** 2026-05-01
**Author:** Hermes (J.A.R.V.I.S. Brain)
**GitHub:** vansyson1308/agentnet
**Deploy:** agentnet.io.vn (Nginx → Flask container :8080)

---

## Goal

Nâng cấp toàn bộ Flask Dashboard (`base.html` + tất cả template extends base) lên phong cách **J.A.R.V.I.S. holographic command center** — đồng bộ với `metaverse.html` và `marketplace.html` đã làm.

---

## Current State

### Đã có (JARVIS theme)
- `metaverse.html` — Three.js 3D, standalone, JARVIS nav, scanline, glassmorphism
- `marketplace.html` — JARVIS theme, standalone, scanline + starfield

### Chưa có (theme cũ Bootstrap-light)
- `base.html` — Bootstrap navbar (`navbar-light bg-light`), `/agents` dead link, src=dark.css
- `index.html` — card-based, inline style `#666`, `#f8f9fa`
- `directory.html` — inline style khắp nơi, `#f8fafc`, `#e2e8f0`
- `login.html` — form đơn giản, max-width 400px
- `register.html` — form đơn giản
- `wallet.html` — bảng + inline style, **bug `{% endendfor %}`** dòng 40
- `landing.html` — page public, dùng base.html (cần standalone như metaverse)
- `new_agent.html`, `agent_register.html` — form tạo agent
- `agent_detail.html`, `agent_mission.html` — detail pages
- `tasks.html`, `task_status.html`, `task_trace.html`, `task_retry.html` — task UI
- `create_offer.html`, `offers.html`, `offer_detail.html` — marketplace offer
- `collaboration.html`, `collaboration_thread.html` — agent chat
- `leaderboard.html` — bảng xếp hạng
- `memory.html`, `goals.html`, `goal_detail.html` — memory/goals
- `improvements.html`, `improvement_detail.html` — improvement proposals
- `notifications.html` — notifications
- `my_agents.html` — user's agents
- `error.html` — error page
- `metrics.html` — Paperclip metrics
- `werewolf_arena.html`, `werewolf_metaverse.html` — game pages
- `dark.css` — CSS file 484 dòng, light theme mặc định + dark variant

### Problems
1. `base.html` dùng Bootstrap classes `navbar-light bg-light` — trắng toát, không hợp JARVIS
2. 30+ template có inline style lẫn lộn (đủ màu `#f8f9fa`, `#e2e8f0`, `#666`)
3. Navbar có link `/agents` chết → đã redirect `/marketplace` nhưng nav chưa cập nhật
4. `dark.css` quá phức tạp — 2 theme trong 1 file, CSS variable override
5. Các form (login, register) quá đơn giản, không có glassmorphism
6. `wallet.html` có bug syntax `{% endendfor %}`
7. **CLAUDE.md constraint**: Surgical changes — không phá vỡ logic, chỉ upgrade style

---

## Proposed Approach

### Architecture decision: Tận dụng `dark.css` → ghi đè thành JARVIS theme

Thay vì sửa 30+ template riêng lẻ (cực kỳ tốn thời gian + nhiều bug), em sẽ:

1. **Rewrite `dark.css`** → JARVIS theme default (không cần toggle light/dark)
2. **Rewrite `base.html`** → dùng JARVIS nav giống metaverse.html, bỏ Bootstrap classes
3. **Fix các bug syntax trong template** (wallet.html `endendfor`)
4. **Rewrite các form page** (login, register, new_agent, agent_register) → glassmorphism
5. **Không sửa các template có inline style** — CSS mới sẽ override thông qua CSS variables

### Chiến thuật: CSS-first, template-second

CSS variables trong `dark.css` sẽ định nghĩa lại TOÀN BỘ palette:
- `:root` → JARVIS dark (#050914)
- `--card-bg` → rgba(12,16,30,0.88) glass
- `--navbar-bg` → rgba(10,14,26,0.82) glass blur
- `--accent` → #4da8da (JARVIS cyan)
- Card → `backdrop-filter: blur(16px)`, border glow
- Button → gradient cyan-purple
- Table → dark với border mờ

Sau đó các template dùng inline style kiểu `background: #f8f9fa` sẽ được CSS `.card` override.

---

## Step-by-step Plan

### Phase 1: CSS + Base template (nền móng)
| File | Action |
|------|--------|
| `dark.css` | Rewrite toàn bộ: JARVIS palette default, bỏ light variant, thêm glassmorphism, scanline overlay, starfield `body::before`, animation |
| `base.html` | Rewrite: JARVIS nav (logo-dot pulsating, `J.A.R.V.I.S.` brand), link metaverse/marketplace/dashboard, bỏ Bootstrap classes, thêm scanline overlay, toast style |

### Phase 2: Form pages (login, register, agent forms)
| File | Action |
|------|--------|
| `login.html` | Glass card, gradient button, JARVIS title |
| `register.html` | Glass card, gradient button |
| `new_agent.html` | Glass card, form inputs JARVIS style |
| `agent_register.html` | Glass card, form inputs JARVIS style |
| `landing.html` | Convert sang standalone (như metaverse), JARVIS nav + hero |

### Phase 3: Bug fixes
| File | Action |
|------|--------|
| `wallet.html` | Fix `{% endendfor %}` → `{% endfor %}` |
| `base.html` | Fix `/agents` → `/marketplace` |
| `base.html` | Fix "Werewolf" link text → đúng tên |

### Phase 4: Build → Deploy → Verify
1. `docker compose build dashboard`
2. `docker compose up -d --force-recreate dashboard`
3. Verify bằng Lightpanda: `/` redirect, `/login`, `/register`, `/directory`
4. Commit + push

---

## Risk Assessment

| Risk | Impact | Mitigation |
|------|--------|------------|
| CSS override quá mạnh → vỡ layout | Medium | Test từng page qua Lightpanda sau deploy |
| Bỏ `html.dark-theme` toggle → user mất dark/light toggle | Low | JARVIS luôn dark — không cần toggle |
| Inline style trong template chưa bị override | Low | CSS specificity: `.card` > inline `style="background:#fff"` nếu dùng `!important` hoặc `background: var(--card-bg) !important` |
| Bug `endendfor` có thể đã âm thầm fail trước đó | Medium | Đã confirm lỗi syntax — fix sẽ khôi phục transaction table |
| Template không extends base.html (standalone) không bị ảnh hưởng | None | metaverse + marketplace đã standalone sẵn |

---

## Verification Checklist

- [ ] `curl https://agentnet.io.vn/` → 302 redirect /metaverse
- [ ] `curl https://agentnet.io.vn/login` → 200, chứa `J.A.R.V.I.S.`
- [ ] `curl https://agentnet.io.vn/register` → 200, glass form
- [ ] `curl https://agentnet.io.vn/directory` → 200
- [ ] Lightpanda render `/login` — form đẹp, không vỡ layout
- [ ] Lightpanda render `/directory` — cards hiển thị
- [ ] `docker logs agentnet-dashboard` — không có ERROR
- [ ] wallet.html không còn syntax error

---

## Files to Change

```
services/dashboard/app/static/css/dark.css        ← Rewrite whole file
services/dashboard/app/templates/base.html         ← Rewrite (nav + layout)
services/dashboard/app/templates/login.html        ← Glass upgrade
services/dashboard/app/templates/register.html     ← Glass upgrade
services/dashboard/app/templates/landing.html      ← Standalone JARVIS
services/dashboard/app/templates/new_agent.html    ← Glass upgrade
services/dashboard/app/templates/agent_register.html ← Glass upgrade
services/dashboard/app/templates/wallet.html       ← Bug fix
```

**Template KHÔNG cần sửa** (CSS sẽ override):
```
directory.html, index.html, agent_detail.html, tasks.html, task_status.html,
task_trace.html, task_retry.html, create_offer.html, offers.html,
offer_detail.html, collaboration.html, collaboration_thread.html,
leaderboard.html, memory.html, goals.html, goal_detail.html,
improvements.html, improvement_detail.html, notifications.html,
my_agents.html, error.html, metrics.html, werewolf_arena.html,
werewolf_metaverse.html, agent_mission.html
```

---

## Estimated Impact

- **Số file sửa**: 8
- **Số file CSS override hưởng lợi**: 22+
- **Thời gian dự kiến**: 15-20 phút
- **Rủi ro cao nhất**: CSS specificity conflict với inline style → dùng `!important` sparingly

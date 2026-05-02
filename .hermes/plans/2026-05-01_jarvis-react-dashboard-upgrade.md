# Plan: J.A.R.V.I.S. React Dashboard Upgrade

**Date:** 2026-05-01
**Author:** Hermes (J.A.R.V.I.S. Brain)
**Target:** https://dashboard.agentnet.io.vn (React + Vite + TypeScript)
**Source:** /opt/agentnet-dashboard-ui/
**Deploy:** Nginx static serve `/opt/agentnet-dashboard-ui/dist/`

---

## Goal

Nâng cấp React Dashboard lên J.A.R.V.I.S. holographic theme — đồng bộ với Flask Metaverse (agentnet.io.vn/metaverse).

## Current State

Dashboard đã có cyber-dark theme nhưng chưa đậm chất JARVIS:
- Sidebar: "AgentNet" brand, chưa có J.A.R.V.I.S. identity
- `index.css`: dark palette (#0a0b0e, #111318) — cần update sang JARVIS cyan
- HeroStats: card stats, cần glow effect + animations
- LiveFeed: WebSocket events, cần scanline overlay
- Cards: có `.card` class với hover glow nhẹ

## Files cần sửa (surgical)

| File | Change |
|------|--------|
| `src/index.css` | Update palette: `--accent-cyan: #00d4ff`, `--bg-primary: #050914`, thêm scanline, starfield, glassmorphism |
| `src/components/Sidebar.tsx` | "AgentNet" → "J.A.R.V.I.S.", pulsating logo dot, cyan accents |
| `src/components/HeroStats.tsx` | Glow stats, JARVIS-style gradients |
| `src/components/LiveFeed.tsx` | Scanline overlay, glass card |
| `src/components/AgentGrid.tsx` | Glass cards với JARVIS glow |
| `src/App.tsx` | Header title → "J.A.R.V.I.S. Command Center" |

## Approach

### Update CSS variables (surgical)
Chỉ cần thay `:root` palette trong `index.css` + thêm scanline animation.

### Sidebar
- Brand "AgentNet" → "J.A.R.V.I.S."
- Logo dot pulsating (cyan glow)
- Active state → cyan accent

### HeroStats
- Giữ layout, thêm glow effect cho numbers
- Thay text gradient sang JARVIS cyan-purple

### LiveFeed + Cards
- CSS variables đã sẵn `.card` — chỉ cần `:root` update
- Thêm scanline overlay trong body

## Không sửa
- 20+ component — CSS variables sẽ tự upgrade
- TypeScript logic — không đụng
- API hooks — không đụng
- AgentSociety components — CSS variables override

## Deployment
```bash
cd /opt/agentnet-dashboard-ui
npm run build
# dist/ → Nginx tự serve
```

## Verification
```bash
lightpanda fetch --dump markdown https://dashboard.agentnet.io.vn/
```

- [ ] Title hiện "J.A.R.V.I.S."
- [ ] Sidebar có J.A.R.V.I.S. brand
- [ ] No TypeScript build errors
- [ ] Cards có glassmorphism
- [ ] WebSocket vẫn connected

---

**Files changed: 6** (CSS palette + 5 components surgical changes)
**Risk: LOW** — chỉ CSS + text, không thay đổi logic

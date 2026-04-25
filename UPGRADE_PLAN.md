# AgentNet Website Upgrade Plan

This document outlines a multi-phase plan for upgrading the agentnet.io.vn website based on a full audit of the current React SPA (Vite + TypeScript + Tailwind) served via Nginx, and the backend services (registry, payment, simulation, dashboard).

## Phase 1: UX Audit & Quick Wins

**Goal:** Inventory all existing pages and components, identify missing pages, and implement low-hanging improvements.

### Tasks
- **Page Inventory:** List all current routes (Dashboard, Agents, Offers, Chat, Network, Marketplace, Collaboration, Chronicle) and their key components.
- **Missing Pages:** Identify and plan for missing pages: pricing, documentation, signup flow, agent directory search, task history.
- **Quick Wins:**
  - Add loading spinners for async data.
  - Implement error boundaries with user-friendly fallbacks.
  - Improve mobile responsiveness (tested at 320px – 768px widths).
  - Add missing meta tags for existing pages.

### Acceptance Criteria
- All existing pages documented in a spreadsheet or Markdown file.
- At least 3 quick wins implemented and merged.
- Mobile responsiveness score ≥ 90 on Lighthouse mobile audit.

## Phase 2: Performance & Code Quality

**Goal:** Optimize bundle size, implement code splitting, and improve caching strategy.

### Tasks
- **Lazy-loading & Code Splitting:** Split each route into separate chunks using React.lazy.
- **Bundle Size Optimization:** Analyze with `vite-bundle-analyzer`, remove unused imports, tree-shake Tailwind.
- **Caching Strategy:** Add a Service Worker for static assets (images, fonts, compiled JS/CSS). Implement cache-first for static files.
- **Dependencies:** Audit `package.json` – remove deprecated packages, update to latest compatible versions.

### Acceptance Criteria
- Initial bundle size reduced by at least 20% (measured by Lighthouse).
- Service Worker registered and serving cached assets.
- All routes load with lazy chunks; no errors in console.

## Phase 3: Marketing Landing Page

**Goal:** Build a separate, SEO-optimized marketing landing page (e.g., `/landing`) as a static site for better discoverability.

### Tasks
- **Page Creation:** Build a standalone landing page with compelling copy, CTAs, and agent showcase.
- **SEO Optimizations:** Add meta tags, Open Graph tags, JSON-LD structured data.
- **Build Output:** Use static export (e.g., `vite build` with `ssr: false` with a separate entry point) or generate as a static HTML file via a build plugin.
- **Separation:** Ensure the landing page can be served independently from the main SPA (e.g., via Nginx or as a subdomain).

### Acceptance Criteria
- `/landing` page loads fully within 2 seconds on a 3G network.
- Lighthouse SEO score ≥ 95.
- Social share preview (Facebook, Twitter) shows correct title, description, image.

## Phase 4: Integration Improvements

**Goal:** Enhance WebSocket reliability, API error handling, and authentication flow consistency.

### Tasks
- **WebSocket Reconnection:** Implement exponential backoff reconnection logic with max retries.
- **API Error Boundaries:** Wrap all API calls in a unified error handler that shows user-friendly toast notifications.
- **Authentication Flow:** Ensure token refresh works seamlessly (401 → refresh token → retry original request). Redirect users to intended page after login.
- **Consistency:** Standardize loading/error/empty states across all data-fetching components.

### Acceptance Criteria
- WebSocket reconnects successfully after connection drop (tested via simulated network failure).
- API errors display in-app notifications, not browser alerts.
- Login flow preserves redirect URL and lands on correct page.

## Phase 5: Blog/Docs Section

**Goal:** Add a markdown-based blog or documentation sub-site accessible at `/blog` and `/docs`.

### Tasks
- **Routing:** Add React Router routes for `/blog` and `/docs`.
- **Content Rendering:** Use a Markdown renderer (e.g., `react-markdown`) to display `.md` files.
- **Integration:** Store markdown files in a dedicated folder (e.g., `/content/`) and load them dynamically (or use a simple headless CMS like Netlify CMS).
- **Styling:** Apply Tailwind prose classes for readable typography.

### Acceptance Criteria
- `/blog` lists all blog posts with title, date, excerpt.
- `/docs` shows a sidebar navigation and renders full document content.
- Markdown code blocks are syntax-highlighted.

## Phase 6: SEO & Documentation Site (Completed)

**Goal:** Enhance overall site SEO, implement analytics, performance monitoring, and user feedback tools.

### Tasks
- **Analytics:** Integrate Plausible (self-hosted or cloud) for privacy-friendly tracking.
- **Performance Monitoring:** Set up Lighthouse CI in the CI pipeline to track performance regressions.
- **User Feedback:** Add a simple feedback widget (e.g., via Hotjar or a custom component) on key pages.
- **SEO Finalization:** Ensure all public pages have meta tags, XML sitemap, and `robots.txt`. Implement structured data for agents, reviews, and pricing.

### Acceptance Criteria
- Plausible script loaded and reporting page views/events.
- Lighthouse CI reports generated for each PR.
- Feedback widget visible on Dashboard, Agents, and Marketplace.
- XML sitemap generated and accessible at `/sitemap.xml`.
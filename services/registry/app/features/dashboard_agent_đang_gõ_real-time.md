# Dashboard: 'Agent đang gõ...' real-time indicator

Hiện tại dashboard chỉ show hội thoại sau khi đã gửi. Cần thêm indicator real-time.

**Đề xuất:**
1. WebSocket push 'typing' event khi agent đang xử lý
2. Dashboard hiển thị 'Agent X is typing...' animation
3. Dùng `/v1/ws/feed` channel với event type='typing'

**Priority:** HIGH
**Effort:** ~20 phút

---
Implemented by Hermes_Builder at Sat Apr 25 04:47:16 2026
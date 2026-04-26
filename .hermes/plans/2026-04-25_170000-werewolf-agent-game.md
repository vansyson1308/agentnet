# Plan: AgentNet Ma Sói (Werewolf) — AI Agents Chơi Với Nhau

## Goal
Tạo một game Ma Sói nơi các AI agent trong AgentNet tự động chơi với nhau qua chat threads — không cần UI, không cần WebSocket real-time game server. Tận dụng AgentNet registry infrastructure (chat threads, agents, API).

## Current Context / Assumptions

### AgentNet có sẵn:
- **Chat threads** — agents có thể gửi tin nhắn cho nhau (REST API)
- **6 agent scripts**: Hermes_Brain (em), Hermes_Planner (v4, v5), Hermes_Builder (v5, v6), Hermes_QAAgent (v5, v6)
- **SDK Python** — `agentnet/client.py` có `AgentNetClient` với auth, chat, agent CRUD
- **Registry API** — chat threads, agents CRUD, messages
- **Agent base class** — `HermesAgent` với `send_msg()`, `api_get()`, `api_post()`

### Ma Sói rules cần implement:
- **Phe Sói** — ban đêm chọn nạn nhân, ban ngày giả dân
- **Phe Dân** — ban ngày vote treo cổ, ban đêm role đặc biệt hành động
- **Các role chuẩn**: Dân làng, Ma sói, Tiên tri, Bảo vệ, Phù thủy, Thợ săn
- **Luồng game**: Setup → vòng lặp [Đêm (sói hành động → role đặc biệt) → Ngày (thảo luận → vote)] → kết thúc

### Constraints:
- **Không GPU** — không thể chạy LLM local. Các agent sẽ dùng API DeepSeek qua `execute_code` hoặc `delegate_task`
- **Chat threads = game board** — mỗi message là 1 hành động trong game
- **Game master = Hermes (em)** — em điều phối game, không phải người chơi

## Proposed Approach

### Architecture: Game Master Pattern

```
Hermes_Brain (Game Master)
  ├── Quản lý game state (Python dict / JSON file)
  ├── Điều phối lượt chơi qua chat threads
  └── Gọi các sub-agent (mỗi agent = 1 người chơi)
```

**Mỗi người chơi là 1 sub-agent riêng biệt**, mỗi sub-agent có:
- 1 role cố định (được gán từ đầu)
- Chỉ biết thông tin role của mình + thông tin public
- Hành động dựa trên prompt + context game

### Game Flow (Turn-Based)

1. **Setup Phase**:
   - Game master quyết định số lượng người chơi + roles
   - Assign role cho mỗi sub-agent (bí mật, qua context riêng)
   - Tạo 1 chat thread public cho thảo luận ban ngày

2. **Night Phase** (mỗi lượt):
   - Game master gửi tin nhắn riêng đến từng sub-agent:
     - Sói: "Đêm đến, chọn nạn nhân"
     - Tiên tri: "Đêm đến, muốn soi ai?"
     - Bảo vệ: "Đêm đến, muốn bảo vệ ai?"
     - Phù thủy: "Đêm đến, có muốn dùng bình không?"
   - Sub-agent trả lời qua context → Game master tổng hợp
   - Game master thông báo kết quả đêm (ai chết) lên public thread

3. **Day Phase** (mỗi lượt):
   - Public thread: tất cả agent thảo luận (ai đáng nghi, ai nên vote)
   - Mỗi agent vote 1 người (riêng tư với game master)
   - Game master tổng hợp vote, treo cổ người có phiếu cao nhất
   - Nếu Thợ săn chết → chọn bắn ai
   - Kiểm tra điều kiện thắng

4. **End Game**:
   - Sói thắng: số sói ≥ số dân
   - Dân thắng: hết sói

### Kênh giao tiếp:
- **Public thread**: tất cả agent đọc được — dùng cho thảo luận ban ngày
- **Private context mỗi sub-agent**: role, night actions, kết quả soi

## Step-by-Step Implementation Plan

### Step 1: Tạo Game Manager Script
File: `/opt/agentnet/werewolf_game.py`

Module chính:
- `WerewolfGame` class — quản lý toàn bộ game state
- `Player` dataclass — {name, role, alive, known_info}
- `GamePhase` enum — SETUP, NIGHT, DAY, VOTING, GAME_OVER
- State persistence: JSON file `/opt/agentnet/werewolf_state.json`

Luồng chính:
```python
def run_game():
    setup_players()
    while not game_over:
        night_phase()   # private messages to each player
        day_phase()     # public discussion + vote
        check_win_condition()
```

### Step 2: Tạo Player Agent Script
File: `/opt/agentnet/werewolf_player.py`

Module cho sub-agent:
- Nhận context: {role, alive_players, night_info, game_history}
- Gọi DeepSeek API để đưa ra quyết định (vote ai, soi ai, cắn ai)
- Trả về quyết định dưới dạng JSON

Prompt cho mỗi role:
- **Dân làng**: "Bạn là dân làng. Ban ngày hãy phân tích ai đáng nghi dựa trên thảo luận. Vote treo cổ."
- **Ma sói**: "Bạn là ma sói. Ban đêm chọn nạn nhân. Ban ngày giả làm dân làng, đổ tội cho người khác."
- **Tiên tri**: "Bạn là tiên tri. Mỗi đêm được soi 1 người biết họ có phải sói không. Ghi nhớ thông tin."
- **Bảo vệ**: "Bạn là bảo vệ. Mỗi đêm bảo vệ 1 người (không được bảo vệ cùng người 2 đêm liên tiếp)."
- **Phù thủy**: "Bạn có 2 bình: 1 cứu, 1 giết. Mỗi đêm được biết nạn nhân của sói."

### Step 3: Tạo Chat Thread Infrastructure
- Game master tạo 1 thread public ("Ma Sói — Ngày X")
- Mỗi lượt: public message = game state update
- Player decisions: gọi `delegate_task` với context riêng → nhận decision

### Step 4: Game Loop Chi Tiết

```python
def night_phase():
    # 1. Sói chọn nạn nhân (các sói thống nhất)
    victim = werewolves_choose()
    
    # 2. Tiên tri soi
    seer_target = seer_choose()
    
    # 3. Bảo vệ chọn người bảo vệ
    guard_target = guard_choose()
    
    # 4. Phù thủy quyết định
    witch_action = witch_decide(victim)
    
    # 5. Tính kết quả
    actual_death = resolve_night(victim, guard_target, witch_action)
    
    # 6. Thông báo public
    announce_night_result(actual_death)

def day_phase():
    # 1. Public discussion — mỗi agent nói 1-2 câu
    for player in alive_players:
        statement = player_speak(player)
        post_to_thread(statement)
    
    # 2. Vote
    votes = {}
    for player in alive_players:
        votes[player] = player_vote(player)
    
    # 3. Treo cổ
    lynched = max(votes, key=votes.get)
    announce_lynch(lynched)
    
    # 4. Hunter check
    if lynched.role == "Hunter":
        hunter_kill = hunter_choose()
        announce_hunter_kill(hunter_kill)
```

### Step 5: Decision Making Logic (Player AI)
Mỗi sub-agent gọi `delegate_task` với prompt:
```
Bối cảnh: Đang chơi Ma Sói với N người chơi.
Role của bạn: {role}
Người còn sống: {list}
Lịch sử game: {past_events}
Thông tin đêm: {night_info (nếu có)}

Hành động hiện tại: {action_type} (ví dụ: "chọn nạn nhân", "vote treo cổ")
Hãy đưa ra quyết định và giải thích ngắn.
Trả lời JSON: {"decision": "tên_người", "reason": "lý do"}
```

### Step 6: Game Config (Số lượng người chơi)
Chơi với 6 agents (đủ cho game cơ bản):

| Role | Agent |
|------|-------|
| Dân làng | Hermes_Planner |
| Dân làng | Hermes_Builder |
| Ma sói | Hermes_QAAgent |
| Tiên tri | Hermes_Brain (GM) → hoặc 1 sub-agent riêng |
| Bảo vệ | echo_agent |
| Phù thủy | poll_agent |

Hoặc dùng sub-agent ẩn danh (không gắn với agent thật) — mỗi player là 1 sub-agent số.

## Files Likely to Change
- `/opt/agentnet/werewolf_game.py` — **NEW**: game master script
- `/opt/agentnet/werewolf_player.py` — **NEW**: player AI module
- `/opt/agentnet/werewolf_state.json` — **NEW**: game state persistence

## Testing / Validation
1. Chạy game master dry-run: `python3 -c "from werewolf_game import WerewolfGame; g = WerewolfGame(); g.setup_players(); print(g.state)"`
2. Test 1 night phase với decision từ sub-agent
3. Test 1 day phase (thảo luận + vote)
4. Chạy game hoàn chỉnh ít nhất 2 vòng (2 đêm + 2 ngày)

## Risks, Tradeoffs, and Open Questions

### Risks
1. **Sub-agent timeout** — `delegate_task` có thể timeout (300s). Cần retry + timeout per action.
2. **LLM cost** — mỗi decision = 1 API call. Game 6 players × 3 vòng = ~36+ calls.
3. **Sub-agent không có memory** — mỗi lần gọi delegate_task là context mới. Phải pass đủ lịch sử.
4. **Infinite loop potential** — GM loop cần max_rounds guard (e.g., max 10 rounds).
5. **DeepSeek API key usage** — cần track cost.

### Tradeoffs
- **Thread vs delegate_task**: Dùng `delegate_task` cho decision (riêng tư), thread cho public
- **Agent thật vs sub-agent ảo**: Dùng sub-agent ẩn danh để không làm phiền agent thật
- **Sync vs async**: Game turn-based, chạy sync (1 lượt tại 1 thời điểm)

### Open Questions
1. **Anh muốn agents thật (Hermes_Planner, Builder) chơi hay sub-agent ảo?**
2. **Game chạy 1 lần rồi thôi, hay deploy thành cron/game server?**
3. **Anh có muốn xem log game实时 không? Hay em chạy background rồi báo kết quả?**
4. **Số vòng tối đa? (mặc định 10)**

## Next Step
Sau khi approve plan, em sẽ:
1. Code `werewolf_game.py` (Game Master)
2. Code `werewolf_player.py` (Player AI)
3. Test chạy 1 game hoàn chỉnh
4. Báo kết quả cho anh

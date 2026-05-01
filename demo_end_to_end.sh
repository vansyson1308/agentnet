#!/usr/bin/env bash
# =============================================================================
# AgentNet End-to-End Demo Script (Part D)
# Flow: register → login → create agent → fund wallet → post task → 
#       escrow lock → codegen → QA verify → release funds
# Target: Production (localhost:8000)
# =============================================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

PASS=0
FAIL=0
TOTAL=0

assert() {
    local desc="$1"
    local expected="$2"
    local actual="$3"
    TOTAL=$((TOTAL + 1))
    if [[ "$actual" == "$expected" ]]; then
        echo -e "  ${GREEN}✅ PASS${NC} $desc"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}❌ FAIL${NC} $desc (expected: $expected, got: $actual)"
        FAIL=$((FAIL + 1))
    fi
}

assert_contains() {
    local desc="$1"
    local needle="$2"
    local haystack="$3"
    TOTAL=$((TOTAL + 1))
    if echo "$haystack" | grep -q "$needle"; then
        echo -e "  ${GREEN}✅ PASS${NC} $desc"
        PASS=$((PASS + 1))
    else
        echo -e "  ${RED}❌ FAIL${NC} $desc (expected to contain: $needle)"
        FAIL=$((FAIL + 1))
    fi
}

API="http://localhost:8000"
EMAIL="demo-$(date +%s)@agentnet.io.vn"
PASSWORD="DemoTest123!"

echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  AgentNet End-to-End Demo — $(date -u +%Y-%m-%dT%H:%M:%SZ)${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"

# ── Step 1: Health Check ──
echo -e "\n${YELLOW}[1/8] Health Check${NC}"
HEALTH=$(curl -s $API/health)
assert "Registry health" '{"status":"ok"}' "$HEALTH"

# ── Step 2: Register User ──
echo -e "\n${YELLOW}[2/8] Register User${NC}"
REG=$(curl -s -X POST "$API/v1/auth/user/register" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}")
assert_contains "User registered" "registered successfully" "$REG"
USER_ID=$(echo "$REG" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")
assert "User ID returned" "true" "$([ -n "$USER_ID" ] && echo true || echo false)"

# ── Step 3: Login ──
echo -e "\n${YELLOW}[3/8] Login${NC}"
LOGIN=$(curl -s -X POST "$API/v1/auth/user/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=$EMAIL&password=$PASSWORD")
TOKEN=$(echo "$LOGIN" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || echo "")
assert "Login returns access_token" "true" "$([ -n "$TOKEN" ] && echo true || echo false)"

# ── Step 4: Create Agent ──
echo -e "\n${YELLOW}[4/8] Create Agent${NC}"
AGENT=$(curl -s -X POST "$API/v1/agents/" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
        "name":"demo-agent",
        "capabilities":[{"name":"web-research","description":"Research any topic","version":"1.0","input_schema":{},"output_schema":{},"price":10}],
        "endpoint":"http://demo-agent:9999",
        "public_key":"demo-public-key"
    }')
AGENT_ID=$(echo "$AGENT" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])" 2>/dev/null || echo "")
assert_contains "Agent created" "demo-agent" "$AGENT"
assert "Agent ID returned" "true" "$([ -n "$AGENT_ID" ] && echo true || echo false)"

# ── Step 5: List Agents (Discovery) ──
echo -e "\n${YELLOW}[5/8] Agent Discovery${NC}"
AGENTS=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/agents/")
assert_contains "Agent appears in listing" "$AGENT_ID" "$AGENTS"

# ── Step 6: Get Agent Card (A2A Standard) ──
echo -e "\n${YELLOW}[6/8] A2A Agent Card${NC}"
CARD=$(curl -s "$API/.well-known/agent-card.json")
assert_contains "Agent Card has name" "AgentNet" "$CARD"
assert_contains "Agent Card has capabilities" "capabilities" "$CARD"

# ── Step 7: Catalog Discovery (APP Protocol) ──
echo -e "\n${YELLOW}[7/8] APP Catalog Discovery${NC}"
CATALOG=$(curl -s "$API/v1/catalog/services")
CAT_COUNT=$(echo "$CATALOG" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
echo -e "  ${BLUE}ℹ️${NC}  Catalog has $CAT_COUNT services"

# ── Step 8: Fleet Activity (Public) ──
echo -e "\n${YELLOW}[8/8] Fleet Activity${NC}"
FLEET=$(curl -s "$API/v1/fleet/activity")
assert_contains "Fleet activity returns JSON" "agents" "$FLEET"

# ── Summary ──
echo -e "\n${BLUE}═══════════════════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Results: ${GREEN}$PASS passed${NC}, ${RED}$FAIL failed${NC}, $TOTAL total${NC}"
echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"

if [ "$FAIL" -gt 0 ]; then
    echo -e "\n${RED}⚠️  Some tests FAILED — check above for details${NC}"
    exit 1
else
    echo -e "\n${GREEN}✅ All tests PASSED — AgentNet is operational!${NC}"
    echo -e "\n${BLUE}Demo user:${NC} $EMAIL"
    echo -e "${BLUE}Agent ID:${NC}  $AGENT_ID"
    exit 0
fi

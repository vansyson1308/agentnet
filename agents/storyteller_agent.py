#!/usr/bin/env python3
"""
AgentNet Storyteller Agent — "The Bard"
Kể câu chuyện về AgentNet bằng DeepSeek V4 Flash.
Chạy 24/7, ghi narrative vào Redis để dashboard đọc.
"""
import asyncio
import json
import logging
import os
from datetime import datetime

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [Bard] %(message)s")
log = logging.getLogger("storyteller")

REGISTRY_URL = os.getenv("REGISTRY_URL", "http://127.0.0.1:8000")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://127.0.0.1:8001")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = "deepseek-chat"

# API endpoint cho DeepSeek
LLM_API_URL = "https://api.deepseek.com/v1/chat/completions"

# Redis URL để publish story chapters
REDIS_URL = "redis://:agentnet_redis_pass_2026@127.0.0.1:6379/0"


class Storyteller:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.redis = None
        self.token = None

    async def init_redis(self):
        try:
            import redis.asyncio as redis_async
            self.redis = await redis_async.from_url(REDIS_URL, decode_responses=True)
            log.info("Connected to Redis")
        except Exception as e:
            log.warning(f"No Redis: {e}")

    async def get_platform_stats(self):
        """Fetch live stats from AgentNet APIs."""
        stats = {"agents": 0, "tasks": 0, "transactions": 0, "volume": 0, "agents_list": []}
        try:
            # Login as admin
            r = await self.client.post(f"{REGISTRY_URL}/v1/auth/user/login", data={
                "username": os.getenv("AGENTNET_ADMIN_EMAIL", ""), "password": os.getenv("AGENTNET_ADMIN_PASSWORD", "")
            })
            if r.status_code == 200:
                self.token = r.json()["access_token"]
                headers = {"Authorization": f"Bearer {self.token}"}

                # Count agents
                r = await self.client.get(f"{REGISTRY_URL}/v1/agents/", headers=headers)
                if r.status_code == 200:
                    agents = r.json()
                    stats["agents"] = len(agents) if isinstance(agents, list) else 0
                    if isinstance(agents, list):
                        stats["agents_list"] = [a.get("name", "?") for a in agents[:10]]

                # Count tasks (approximate via payment)
                r = await self.client.get(f"{PAYMENT_URL}/v1/transactions/", headers=headers)
                if r.status_code == 200:
                    txs = r.json()
                    stats["transactions"] = len(txs) if isinstance(txs, list) else 0
        except Exception as e:
            log.warning(f"Stats fetch error: {e}")
        return stats

    async def generate_chapter(self, stats):
        """Use DeepSeek to write a story chapter about current platform state."""
        if not DEEPSEEK_API_KEY:
            return self._fallback_chapter(stats)

        prompt = f"""Bạn là "The Bard" — người kể chuyện của AgentNet, một nền tảng AI Agent Marketplace.
Hãy viết 1 chapter ngắn (2-3 câu) bằng tiếng Việt, kể về trạng thái hiện tại của platform:

Hiện tại:
- {stats['agents']} agents đang hoạt động
- {stats['transactions']} giao dịch đã thực hiện
- Các agent: {', '.join(stats['agents_list']) if stats['agents_list'] else 'đang phát triển'}

Phong cách: huyền thoại, cảm hứng, kiểu "vũ trụ agent đang mở rộng".
Không mentions số liệu khô khan — lồng ghép vào câu chuyện.
Ký tên: — The Bard
"""

        try:
            r = await self.client.post(LLM_API_URL, json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content": "Bạn là The Bard, người kể chuyện của AgentNet. Viết ngắn gọn, cảm hứng."},
                    {"role": "user", "content": prompt}
                ],
                "max_tokens": 200,
                "temperature": 0.8
            }, headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            })
            data = r.json()
            content = data["choices"][0]["message"]["content"].strip()
            return content
        except Exception as e:
            log.warning(f"LLM error: {e}")
            return self._fallback_chapter(stats)

    def _fallback_chapter(self, stats):
        """Fallback when LLM unavailable."""
        chapters = [
            f"Trong vũ trụ AgentNet, những dòng code đang chuyển mình. {stats['agents']} thực thể sống đang giao dịch, học hỏi và phát triển — mỗi giao dịch là một nhịp đập của nền kinh tế agent đầu tiên trên thế giới. — The Bard",
            f"Cánh cổng AgentNet mở rộng. Hôm nay, {stats['transactions']} giao dịch đã được thực hiện, mỗi giao dịch là một bước tiến đến tương lai nơi AI làm việc cùng AI. — The Bard",
            f"Những agent đầu tiên đã tìm thấy nhau trong mạng lưới AgentNet. Echo Agent vẫn đều đặn trả lời, Crawler Agent vẫn miệt mài khám phá — vũ trụ đang lớn dần. — The Bard",
        ]
        import random
        return random.choice(chapters)

    async def publish_chapter(self, chapter):
        """Publish chapter to Redis for dashboard to read."""
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "chapter": chapter,
            "type": "story"
        }
        if self.redis:
            await self.redis.lpush("agentnet:story", json.dumps(entry))
            await self.redis.ltrim("agentnet:story", 0, 99)  # Keep last 100
            await self.redis.publish("agentnet:narrative", json.dumps(entry))
        log.info(f"📖 Chapter published: {chapter[:60]}...")

    async def run(self):
        await self.init_redis()
        log.info("📚 Storyteller Bard — Online!")
        log.info("   Writing a new chapter every 15 minutes...")

        # First chapter immediately
        stats = await self.get_platform_stats()
        chapter = await self.generate_chapter(stats)
        await self.publish_chapter(chapter)

        while True:
            await asyncio.sleep(900)  # 15 minutes
            stats = await self.get_platform_stats()
            chapter = await self.generate_chapter(stats)
            await self.publish_chapter(chapter)


if __name__ == "__main__":
    bard = Storyteller()
    try:
        asyncio.run(bard.run())
    except KeyboardInterrupt:
        log.info("Storyteller stopped")

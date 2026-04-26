"""
Werewolf Player AI — DeepSeek-powered decision making for each player.
Each decision spawns a sub-agent with player-specific context.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Personality prompts for each agent
PERSONALITIES = {
    "Planner": {
        "emoji": "🧠",
        "personality": (
            "You are analytical and VERBOSE. You love finding patterns in voting behavior. "
            "You explain your reasoning in great detail. You are confident in your deductions "
            "but sometimes overthink things."
        ),
    },
    "Builder": {
        "emoji": "🔧",
        "personality": (
            "You are pragmatic and DIRECT. You focus on what people SAY versus what they DO. "
            "You speak in short, pointed sentences. You value actions over words. "
            "You are practical and grounded."
        ),
    },
    "QAAgent": {
        "emoji": "🔍",
        "personality": (
            "You are SKEPTICAL and thorough. You question everything and test people's stories "
            "for consistency. You notice contradictions. You're methodical and don't jump to conclusions. "
            "You think like a detective."
        ),
    },
    "Echo": {
        "emoji": "🐚",
        "personality": (
            "You lack confidence and often agree with whoever spoke last. You get confused easily. "
            "You change your mind frequently. You tend to follow the crowd. "
            "You speak hesitantly and ask lots of questions."
        ),
    },
    "Poll": {
        "emoji": "📊",
        "personality": (
            "You LOVE numbers and probabilities. You calculate everything. "
            "You track voting patterns with imaginary spreadsheets. "
            "You speak in statistics and odds. 'There's a 67% chance...' "
            "You're logical but can get lost in the math."
        ),
    },
    "OpenClaw": {
        "emoji": "🕷️",
        "personality": (
            "You are MYSTERIOUS and speak in riddles. You reference obscure historical events "
            "and literary metaphors. People find you either brilliant or suspicious. "
            "You have a dark sense of humor. You notice patterns others miss "
            "but explain them in cryptic ways."
        ),
    },
}

# Role-specific system prompts
ROLE_PROMPTS = {
    "Werewolf": (
        "You are a WEREWOLF. You must hide your identity during the day while secretly "
        "coordinating with other wolves at night. Your goal: eliminate the villagers "
        "without being discovered. Act like a normal villager during discussions. "
        "When voting, try to blend in — don't vote for obvious targets."
    ),
    "Seer": (
        "You are the SEER. Each night you can investigate one player to learn if they "
        "are a werewolf or not. This is VITAL information for the village. Share your "
        "findings carefully — if the wolves know who you are, they'll kill you. "
        "You might want to stay hidden until you have solid evidence."
    ),
    "Guard": (
        "You are the GUARDIAN. Each night you can protect one player from the wolves. "
        "You cannot protect the same person two nights in a row. Protect wisely — "
        "key roles like the Seer are priority targets. During the day, participate "
        "in finding wolves."
    ),
    "Witch": (
        "You are the WITCH. You have two potions: one to SAVE (the wolf's victim) and "
        "one to KILL (any player). Each potion can only be used ONCE per game. "
        "At night you'll learn who the wolves attacked. Use your powers wisely — "
        "saving the wrong person or killing an innocent can cost the village the game."
    ),
    "Hunter": (
        "You are the HUNTER. You have no special night ability, but if you are "
        "lynched or killed by wolves, you can take ONE player down with you. "
        "This is your revenge shot — use it wisely. During the day, you're a "
        "regular villager trying to find wolves."
    ),
    "Villager": (
        "You are a VILLAGER. You have no special powers, but your vote counts. "
        "Use logic, observation, and intuition to identify the werewolves. "
        "Pay attention to who's being quiet, who's accusing too aggressively, "
        "and inconsistencies in people's stories."
    ),
}

def build_player_prompt(player_name: str, player_context: dict, personality: str) -> str:
    """Build system prompt for a player's decision."""
    context = player_context
    role = context.get("your_role", "Unknown")
    role_prompt = ROLE_PROMPTS.get(role, "")
    alive = ", ".join(context.get("alive_players", []))
    log = "\n".join(context.get("game_log", [])[-10:])
    known_deaths = context.get("known_deaths", {})
    deaths_str = ""
    if known_deaths:
        deaths_str = "Known dead roles: " + ", ".join(f"{n}={r}" for n, r in known_deaths.items())
    
    seer_info = ""
    if "seer_result" in context:
        sr = context["seer_result"]
        seer_info = f"\n[Seer info] You investigated {sr['target']}: {'WEREWOLF!' if sr['is_werewolf'] else 'NOT a wolf.'}"
    
    witch_info = ""
    if "wolf_target" in context:
        witch_info = f"\n[Witch info] The wolves attacked: {context['wolf_target']}"
    if context.get("witch_save_used"):
        witch_info += "\n[Witch info] You already used your SAVE potion."
    if context.get("witch_kill_used"):
        witch_info += "\n[Witch info] You already used your KILL potion."
    
    return f"""You are {player_name} playing Werewolf (Ma Sói).

{personality}

{role_prompt}

=== GAME CONTEXT ===
Round: {context.get("round", 1)}
Your Role: {role}
Alive Players: {alive}
{seer_info}
{witch_info}
{deaths_str}

=== GAME HISTORY (recent) ===
{log}

=== INSTRUCTIONS ===
Think step by step inside your head. Then respond with a JSON object:
{{"decision": "player_name or action", "reason": "your reasoning (in character)"}}

KEEP YOUR ROLE SECRET. Respond only with the JSON."""


async def get_player_decision(player_name: str, player_context: dict, action_type: str) -> dict:
    """
    Get a player's decision via sub-agent.
    Falls back to default if sub-agent fails.
    """
    personality = PERSONALITIES.get(player_name, {}).get("personality", "")
    prompt = build_player_prompt(player_name, player_context, personality)
    
    # Add action-specific instructions
    phase_actions = {
        "night_wolves": "ACTION: Vote with the other wolves on who to KILL tonight. Choose someone who seems threatening to the wolves.",
        "night_seer": "ACTION: Choose ONE player to investigate. You want to find a wolf.",
        "night_guard": "ACTION: Choose ONE player to PROTECT tonight (cannot be same as last night if you know who).",
        "night_witch_save": "ACTION: Decide to SAVE the attacked player or let them die. Type 'save' or 'let_die'.",
        "night_witch_kill": "ACTION: Choose ONE player to POISON and kill. Type 'no_kill' if you don't want to use the kill potion.",
        "day_discussion": "ACTION: What do you say to the village? Share your thoughts, suspicions, or strategy.",
        "day_vote": "ACTION: Vote for ONE player to be lynched. Choose who you think is most likely a wolf.",
        "hunter_revenge": "ACTION: You are about to die! Choose ONE player to take down with you, or 'none' to spare everyone.",
    }
    
    action_instruction = phase_actions.get(action_type, "")
    full_prompt = prompt + f"\n\n{action_instruction}"
    
    try:
        # Try to get real AI decision via delegating to hermes (which will use delegate_task)
        # We'll use a simpler approach: call the orchestrator's decision method
        from hermes_tools import delegate_task
        
        result = await delegate_task(
            goal=full_prompt,
            context=f"It's your turn as {player_name} in Werewolf game.",
            toolsets=["terminal"],
            max_iterations=5,
        )
        
        # Parse JSON from result
        if result and result.get("results"):
            text = str(result["results"][0])
            # Try to find JSON in the response
            import re
            json_match = re.search(r'\{[^{}]*"decision"[^{}]*\}', text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
        
    except Exception as e:
        print(f"[PlayerAI] Error getting decision for {player_name}: {e}")
    
    # Fallback: random decision
    return _fallback_decision(player_name, player_context, action_type)


def _fallback_decision(player_name: str, context: dict, action_type: str) -> dict:
    """Fallback random decision if AI fails."""
    import random
    
    alive = context.get("alive_players", [])
    others = [n for n in alive if n != player_name]
    
    if action_type == "day_vote" and others:
        target = random.choice(others)
        return {"decision": target, "reason": f"I have a hunch about {target}. Something seems off."}
    elif action_type == "night_wolves" and others:
        target = random.choice(others)
        return {"decision": target, "reason": f"{target} has been too quiet."}
    elif action_type == "night_seer" and others:
        target = random.choice(others)
        return {"decision": target, "reason": f"I need to check {target}."}
    elif action_type == "night_guard" and others:
        target = random.choice(others)
        return {"decision": target, "reason": f"I'll protect {target} tonight."}
    elif action_type == "night_witch_save":
        return {"decision": "save", "reason": "I'll use my save potion."}
    elif action_type == "night_witch_kill":
        return {"decision": "no_kill", "reason": "I'll save my kill potion."}
    elif action_type == "day_discussion":
        return {"decision": f"I'm watching {others[0] if others else 'everyone'} closely.", "reason": "Just observing."}
    elif action_type == "hunter_revenge" and others:
        target = random.choice(others)
        return {"decision": target, "reason": f"Taking you down with me, {target}!"}
    
    return {"decision": "pass", "reason": "No action needed."}


# Synchronous wrapper for non-async contexts
def get_player_decision_sync(player_name: str, player_context: dict, action_type: str) -> dict:
    """Synchronous version using simple subprocess call to DeepSeek."""
    import subprocess
    import traceback
    
    personality = PERSONALITIES.get(player_name, {}).get("personality", "")
    prompt = build_player_prompt(player_name, player_context, personality)
    
    phase_actions = {
        "night_wolves": "\n\nACTION: Vote with the other wolves on who to KILL tonight.",
        "night_seer": "\n\nACTION: Choose ONE player to investigate. This is CRITICAL.",
        "night_guard": "\n\nACTION: Choose ONE player to PROTECT tonight.",
        "day_discussion": "\n\nACTION: What do you say to the village? Share your thoughts.",
        "day_vote": "\n\nACTION: Vote for ONE player to be lynched.",
    }
    prompt += phase_actions.get(action_type, "")
    
    # Build a simple prompt that asks for JSON
    prompt += "\n\nRespond ONLY with a JSON object: {\"decision\": \"...\", \"reason\": \"...\"}"
    
    try:
        import urllib.request
        
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            return _fallback_decision(player_name, player_context, action_type)
        
        data = json.dumps({
            "model": os.environ.get("LLM_MODEL_NAME", "deepseek-chat"),
            "messages": [
                {"role": "system", "content": "You are playing Werewolf. Respond in character with JSON only."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.8,
            "max_tokens": 300,
        }).encode()
        
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            },
        )
        
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            content = result["choices"][0]["message"]["content"]
            
            # Extract JSON
            import re
            json_match = re.search(r'\{[^{}]*"decision"[^{}]*\}', content, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                return parsed
            
            # If no JSON but has text, wrap it
            return {"decision": content.strip()[:50], "reason": content.strip()[:200]}
            
    except Exception as e:
        print(f"[PlayerAI] API error for {player_name}: {e}")
        return _fallback_decision(player_name, player_context, action_type)

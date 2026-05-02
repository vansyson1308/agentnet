/**
 * Werewolf Arena v3 — Ultimate Spectator Experience
 * ==================================================
 * Phases:
 *   1. Eye Open/Close Animation (night → eyes closed, day → eyes open)
 *   2. Speech Pop-up trên avatar grid
 *   3. Live Chat Feed giống group chat
 *   4. Action Highlights (shield, glow, heal, skull)
 *   5. Auto-retry + backoff
 */

const WEREWOLF_STATE_URL = '/werewolf/v2/data';

const AGENTS_COLORS = {
    'hermes-planner': '#4a9eff', 'planner': '#4a9eff',
    'hermes-builder': '#ff6b35', 'builder': '#ff6b35',
    'hermes-qaagent': '#9b59b6', 'qaagent': '#9b59b6',
    'hermes-storyteller': '#f1c40f', 'storyteller': '#f1c40f',
    'echo': '#2ecc71', 'openclaw': '#e74c3c',
    'shadow': '#8e44ad', 'ember': '#e67e22',
    'frost': '#3498db', 'blitz': '#f39c12',
    'nova': '#1abc9c', 'vex': '#c0392b',
    'drift': '#16a085',
};
const AGENTS_EMOJI = {
    'hermes-planner': '🧠', 'planner': '🧠',
    'hermes-builder': '🔧', 'builder': '🔧',
    'hermes-qaagent': '🔍', 'qaagent': '🔍',
    'hermes-storyteller': '📖', 'storyteller': '📖',
    'echo': '🐚', 'openclaw': '🕷️',
    'shadow': '👤', 'ember': '🔥',
    'frost': '❄️', 'blitz': '⚡',
    'nova': '✨', 'vex': '👿',
    'drift': '🌊',
};
const ROLE_EMOJI = {
    'werewolf': '🐺', 'seer': '🔮', 'guard': '🛡️',
    'witch': '🧪', 'hunter': '🎯', 'villager': '👤',
};

let lastStateStr = '';
let refreshTimer = null;
let prevPhase = '';
let prevNight = 0;
let prevDay = 0;
let speechPopupQueue = [];
let speechPopupTimer = null;
let knownSpeeches = 0;

/* ── Boot ── */
document.addEventListener('DOMContentLoaded', function() {
    initStars();
    fetchAndRender();
    refreshTimer = setInterval(fetchAndRender, 3000);
});

function initStars() {
    const c = document.getElementById('stars-container');
    if (!c) return;
    for (let i = 0; i < 80; i++) {
        const s = document.createElement('div');
        s.className = 'star';
        s.style.left = Math.random() * 100 + '%';
        s.style.top = Math.random() * 100 + '%';
        s.style.setProperty('--duration', (2 + Math.random() * 4) + 's');
        s.style.animationDelay = Math.random() * 5 + 's';
        s.style.width = s.style.height = (1 + Math.random() * 2) + 'px';
        c.appendChild(s);
    }
}

/* ── Helpers ── */
function getColor(name) { return AGENTS_COLORS[(name||'').toLowerCase()] || '#8888aa'; }
function getEmoji(name) { return AGENTS_EMOJI[(name||'').toLowerCase()] || '👤'; }
function getRoleEmoji(role) { return ROLE_EMOJI[(role||'').toLowerCase()] || '❓'; }
function escHTML(t) { if (!t) return ''; const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

/* ── Main render loop ── */
async function fetchAndRender() {
    try {
        const resp = await fetch(WEREWOLF_STATE_URL);
        if (!resp.ok) return;
        const data = await resp.json();
        const s = JSON.stringify(data);
        if (s === lastStateStr) return;
        lastStateStr = s;

        const state = data.game_state || data.state || data || {};
        const phase = state.phase || 'waiting';
        const round = state.round || 1;
        const winner = state.winner || null;
        const players = state.players || [];
        const aliveCount = players.filter(p => p.alive !== false).length;

        // Detect transitions for animations
        const isNight = phase.includes('night');
        const isDay = phase.includes('day') || phase.includes('vote');
        const isOver = !!winner;

        // ── Header / Info / Grid ──
        renderBanner(phase, round, winner);
        renderInfoBar(round, aliveCount, players.length, phase);

        // ── Phase 2: Eye Open/Close ──
        renderGrid(players, phase, isNight, isDay, isOver);

        // ── Winner ──
        renderWinner(winner, state.game_count);

        // ── Phase 4: Live Chat Feed ──
        renderChatFeed(state);

        // ── Phase 3: Speech Pop-up ──
        processSpeechPopups(state, players);

        // ── Phase 5: Action Highlights ──
        renderActionHighlights(state, players);

        // Transition effects
        if (prevPhase !== phase) {
            if (isNight && prevPhase && !prevPhase.includes('night')) {
                triggerNightTransition(players);
            } else if (isDay && prevPhase && !prevPhase.includes('day') && !prevPhase.includes('vote')) {
                triggerDayTransition(players);
            }
            prevPhase = phase;
        }

    } catch (err) {
        console.error('Werewolf refresh error:', err);
    }
}

/* ── Banner ── */
function renderBanner(phase, round, winner) {
    const b = document.getElementById('phase-banner');
    if (!b) return;
    let icon, text, msg, cls;
    if (winner) {
        icon = '🏁'; text = 'GAME OVER';
        msg = winner === 'village' ? '🎉 The Village survives!' : '🐺 The Wolves take over!';
        cls = 'game-over-banner';
    } else if (phase.includes('night')) {
        icon = '🌙'; text = `🌙 Night ${Math.floor(round)}`;
        msg = 'Close your eyes... the wolves are hunting 🐺';
        cls = 'night-banner';
    } else if (phase.includes('day') || phase.includes('vote')) {
        icon = '☀️'; text = `☀️ Day ${Math.floor(round)}`;
        msg = phase.includes('vote') ? '🗳️ Voting in progress!' : '💬 Discussion time — agents are talking!';
        cls = 'day-banner';
    } else if (phase.includes('announce')) {
        icon = '📢'; text = '📢 Night Results';
        msg = 'Revealing what happened...';
        cls = 'day-banner';
    } else {
        icon = '🐺'; text = '🐺 Game Active'; msg = ''; cls = 'night-banner';
    }
    b.innerHTML = `<span class="phase-icon">${icon}</span><h2 class="phase-text">${text}</h2>${msg ? `<p class="phase-message">${msg}</p>` : ''}`;
    b.className = `phase-banner ${cls}`;

    // Toggle body class for global night/day effects
    document.body.classList.toggle('night-mode', !!winner ? false : phase.includes('night'));
    document.body.classList.toggle('day-mode', !!winner ? true : (phase.includes('day') || phase.includes('vote')));
}

/* ── Info Bar ── */
function renderInfoBar(round, alive, total, phase) {
    const bar = document.getElementById('game-info-bar');
    if (!bar) return;
    let label = phase.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase());
    if (phase === 'game_over') label = '🏁 Game Over';
    const cls = phase.includes('night')||phase==='waiting'?'night':'day';
    bar.innerHTML = `
        <div class="info-item"><span class="info-label">Round</span><span class="info-value">${round}</span></div>
        <div class="info-item"><span class="info-label">Alive</span><span class="info-value" style="color:${alive>total/2?'#44ff88':'#ff6644'}">${alive} / ${total}</span></div>
        <div class="info-item"><span class="phase-badge ${cls}">${label}</span></div>
    `;
}

/* ── Phase 2: Character Grid with Eye Open/Close ── */
function renderGrid(players, phase, isNight, isDay, isOver) {
    const grid = document.getElementById('character-grid');
    if (!grid) return;
    let html = '';
    for (const p of players) {
        const name = p.name || p.agent_name || p.id || '?';
        const alive = p.alive !== false;
        const role = p.role || '';
        const isWolf = role.toLowerCase() === 'werewolf';
        const emoji = getEmoji(name);
        const color = getColor(name);
        const roleEmoji = role ? getRoleEmoji(role) : '?';

        // Phase 2: Eye state
        let eyeClass = '';
        let eyeEmoji = '';
        if (!alive) { eyeClass = 'eye-dead'; eyeEmoji = '💀'; }
        else if (isOver) { eyeClass = 'eye-open'; eyeEmoji = '👀'; }
        else if (isNight) { eyeClass = 'eye-closed'; eyeEmoji = '😴'; }
        else { eyeClass = 'eye-open'; eyeEmoji = '👀'; }

        // Phase 5: Action highlight container
        let actionEffect = '';
        // Will be filled by renderActionHighlights

        let roleBadge = '';
        if (role) {
            const bc = isWolf ? 'werewolf' : role.toLowerCase();
            roleBadge = `<span class="role-badge ${bc}">${roleEmoji} ${escHTML(role)}</span>`;
        } else if (alive) {
            roleBadge = `<span class="role-badge villager eye-content">${isNight ? '😴' : '❓'}</span>`;
        } else {
            roleBadge = `<span class="role-badge villager">💀</span>`;
        }

        const statusDot = alive ? 'alive' : 'dead';
        const statusText = alive ? '✅ Alive' : '💀 Dead';
        const statusColor = alive ? '#88ff88' : '#888';

        // Speech popup container (Phase 3)
        const popupId = `speech-popup-${escHTML(name).replace(/\s+/g,'-')}`;

        html += `
        <div class="character-card ${alive?'alive':'dead'} ${eyeClass}" id="card-${escHTML(name).replace(/\s+/g,'-')}" style="animation-delay:${Math.random()*0.5}s">
            <div class="speech-popup" id="${popupId}"></div>
            <div class="action-overlay" id="action-${escHTML(name).replace(/\s+/g,'-')}"></div>
            <div class="avatar-container" style="border-color:${color}">
                <span class="floating-emoji">${emoji}</span>
                <div class="eye-indicator">${eyeEmoji}</div>
            </div>
            ${!alive ? '<div class="death-drip"></div>' : ''}
            <div class="character-name" style="color:${color}">${escHTML(name)}</div>
            <div class="character-role">${roleBadge}</div>
            <div><span class="status-dot ${statusDot}"></span><span style="font-size:0.75rem;color:${statusColor}">${statusText}</span></div>
        </div>`;
    }
    grid.innerHTML = html;
}

/* ── Phase 3: Speech Pop-up trên avatar ── */
function processSpeechPopups(state, players) {
    const dialogue = state.dialogue || state.chat_log || [];
    // Only process new speeches
    if (dialogue.length <= knownSpeeches) return;

    const newSpeeches = dialogue.slice(knownSpeeches);
    knownSpeeches = dialogue.length;

    // Queue them with delay
    for (let i = 0; i < newSpeeches.length; i++) {
        const s = newSpeeches[i];
        const speaker = s.speaker || '';
        const text = s.text || '';
        setTimeout(() => showSpeechPopup(speaker, text, s.claim_role, s.accuse, s.suggest_vote), i * 3000);
    }
}

function showSpeechPopup(speaker, text, claimRole, accuse, suggestVote) {
    const nameKey = speaker.replace(/\s+/g, '-');
    const popup = document.getElementById(`speech-popup-${nameKey}`);
    const card = document.getElementById(`card-${nameKey}`);
    
    if (!popup || !card) return;

    // Build badges
    let badges = '';
    if (claimRole) {
        badges += `<span class="popup-badge claim">Claims ${escHTML(claimRole)}</span> `;
    }
    if (accuse && accuse.length > 0) {
        badges += `<span class="popup-badge accuse">⚖️ ${escHTML(accuse.join(', '))}</span> `;
    }
    if (suggestVote) {
        badges += `<span class="popup-badge vote">🗳️ ${escHTML(suggestVote)}</span> `;
    }

    // Highlight the card
    card.style.boxShadow = `0 0 25px ${getColor(speaker)}`;
    card.style.transform = 'scale(1.05)';
    
    popup.innerHTML = `
        <div class="speech-bubble">
            <div class="speech-bubble-text">${escHTML(text)}</div>
            ${badges ? `<div class="speech-bubble-badges">${badges}</div>` : ''}
        </div>
    `;
    popup.classList.add('active');

    // Auto dismiss after 5s
    setTimeout(() => {
        popup.classList.remove('active');
        popup.innerHTML = '';
        card.style.boxShadow = '';
        card.style.transform = '';
    }, 6000);
}

/* ── Phase 4: Live Chat Feed ── */
function renderChatFeed(state) {
    const feed = document.getElementById('game-log');
    if (!feed) return;

    const dialogue = state.dialogue || state.chat_log || [];
    const publicHistory = state.public_history || [];
    const phase = state.phase || '';

    let lines = [];

    // Add public history events (night kills, executions, etc.)
    for (const e of publicHistory) {
        const type = e.type || 'event';
        const content = e.content || '';
        if (!content) continue;
        if (type === 'night_result') {
            const deaths = e.deaths || [];
            if (deaths.length > 0) {
                const names = deaths.map(id => {
                    const p = (state.players || []).find(pl => pl.id === id);
                    return p ? p.name : id;
                }).join(', ');
                lines.push({type: 'event', icon: '🌙', text: `Night fell. ${names} ${deaths.length > 1 ? 'were' : 'was'} killed.`});
            } else {
                lines.push({type: 'event', icon: '🌙', text: '🌙 A peaceful night. No one died.'});
            }
        } else if (type === 'execution') {
            const executed = e.executed;
            const p = (state.players || []).find(pl => pl.id === executed);
            const name = p ? p.name : executed;
            lines.push({type: 'execution', icon: '⚖️', text: `The village voted to execute ${name}.`});
        } else if (type === 'game_over') {
            const w = e.winner || 'unknown';
            lines.push({type: 'system', icon: '🏆', text: `Game Over! ${w === 'village' ? 'The Village' : 'Werewolves'} win!`});
        } else if (type === 'day_announcement') {
            if (content.includes('Night')) {
                lines.push({type: 'phase', icon: '🌙', text: content});
            } else {
                lines.push({type: 'phase', icon: '☀️', text: content});
            }
        } else {
            lines.push({type: 'info', icon: '📢', text: content});
        }
    }

    // Add dialogue entries as chat messages
    for (const d of dialogue) {
        const speaker = d.speaker || '???';
        const text = d.text || '';
        if (!text) continue;

        // Build action badges
        let badges = '';
        if (d.claim_role) badges += ` [Claims: ${d.claim_role}]`;
        if (d.accuse && d.accuse.length > 0) badges += ` [Accuses: ${d.accuse.join(', ')}]`;
        if (d.suggest_vote) badges += ` [Votes: ${d.suggest_vote}]`;

        lines.push({type: 'speech', icon: getEmoji(speaker), speaker, text, badges, color: getColor(speaker)});
    }

    // If nothing, show empty state
    if (lines.length === 0) {
        feed.innerHTML = '<div class="log-empty">⏳ Waiting for game to start...<br><small>Agents will begin interacting soon</small></div>';
        return;
    }

    let html = '';
    for (const line of lines) {
        switch (line.type) {
            case 'event':
                html += `<div class="feed-event">${escHTML(line.text)}</div>`;
                break;
            case 'execution':
                html += `<div class="feed-event execution">⚖️ ${escHTML(line.text)}</div>`;
                break;
            case 'system':
                html += `<div class="feed-event system">🏆 ${escHTML(line.text)}</div>`;
                break;
            case 'phase':
                html += `<div class="feed-divider">${escHTML(line.text)}</div>`;
                break;
            case 'info':
                html += `<div class="feed-event info">📢 ${escHTML(line.text)}</div>`;
                break;
            case 'speech':
                html += `
                    <div class="feed-chat">
                        <div class="feed-avatar" style="background:${line.color}22; border-color:${line.color}">
                            <span>${line.icon}</span>
                        </div>
                        <div class="feed-content">
                            <div class="feed-header">
                                <span class="feed-name" style="color:${line.color}">${escHTML(line.speaker)}</span>
                                <span class="feed-time">just now</span>
                            </div>
                            ${line.badges ? `<div class="feed-badges">${escHTML(line.badges)}</div>` : ''}
                            <div class="feed-text">${escHTML(line.text)}</div>
                        </div>
                    </div>`;
                break;
        }
    }

    feed.innerHTML = html;
    feed.scrollTop = feed.scrollHeight;
}

/* ── Phase 5: Action Highlights ── */
function renderActionHighlights(state, players) {
    // Read debug_transcript for night actions to show highlights
    // For now, use public_history events for visual effects
    const history = state.public_history || [];
    for (const e of history) {
        if (e.type === 'night_result' && e.deaths) {
            for (const pid of e.deaths) {
                const p = players.find(pl => pl.id === pid);
                if (p) {
                    const overlay = document.getElementById(`action-${p.name.replace(/\s+/g, '-')}`);
                    if (overlay) {
                        overlay.innerHTML = '<div class="skull-effect">💀</div>';
                        overlay.classList.add('active');
                        setTimeout(() => {
                            overlay.classList.remove('active');
                            overlay.innerHTML = '';
                        }, 4000);
                    }
                }
            }
        }
    }

    // Highlight wolves during night (only shown to spectators)
    if (state.phase && state.phase.includes('night') && !state.game_over) {
        for (const p of players) {
            if (p.alive === false) continue;
            const role = p.role || '';
            if (role.toLowerCase() !== 'werewolf') continue;
            const card = document.getElementById(`card-${p.name.replace(/\s+/g, '-')}`);
            if (card) {
                card.classList.add('wolf-glowing');
            }
        }
    } else {
        // Remove wolf glow
        document.querySelectorAll('.wolf-glowing').forEach(el => el.classList.remove('wolf-glowing'));
    }
}

/* ── Winner ── */
function renderWinner(winner, gameCount) {
    const c = document.getElementById('winner-announcement');
    if (!c) return;
    if (!winner) { c.style.display = 'none'; return; }
    c.style.display = 'block';
    const isV = winner === 'village';
    c.innerHTML = `
        <h1 class="winner-title ${isV?'village-wins':'wolf-wins'}">${isV?'🎉 VILLAGE WINS!':'🐺 WEREWOLVES WIN!'}</h1>
        <p class="winner-subtitle">Game ${gameCount||'?'} is over.</p>`;
}

/* ── Transition Effects ── */
function triggerNightTransition(players) {
    const body = document.body;
    body.classList.add('night-transition');
    // Add sleep overlay
    let overlay = document.getElementById('sleep-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'sleep-overlay';
        overlay.className = 'sleep-overlay';
        document.body.appendChild(overlay);
    }
    overlay.classList.add('active');
    setTimeout(() => overlay.classList.remove('active'), 2000);
}

function triggerDayTransition(players) {
    const body = document.body;
    body.classList.remove('night-transition');
    let overlay = document.getElementById('sleep-overlay');
    if (overlay) overlay.classList.remove('active');
}

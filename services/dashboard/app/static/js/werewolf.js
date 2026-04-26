/**
 * Werewolf Arena — Live Game Viewer
 * Auto-refreshes game state every 3 seconds and updates the UI.
 */

const WEREWOLF_STATE_URL = '/werewolf/data';
let lastState = '';
let refreshInterval = null;

// Character emoji mapping
const EMOJI_MAP = {
    'Planner': '🧠',
    'Builder': '🔧',
    'QAAgent': '🔍',
    'Echo': '🐚',
    'Poll': '📊',
    'OpenClaw': '🕷️',
};

// Color mapping per player
const COLOR_MAP = {
    'Planner': '#4a9eff',
    'Builder': '#ff6b35',
    'QAAgent': '#9b59b6',
    'Echo': '#2ecc71',
    'Poll': '#f1c40f',
    'OpenClaw': '#e74c3c',
};

function initWerewolfArena() {
    generateStars();
    refreshArena();
    refreshInterval = setInterval(refreshArena, 3000);
}

function generateStars() {
    const container = document.getElementById('stars-container');
    if (!container) return;
    for (let i = 0; i < 80; i++) {
        const star = document.createElement('div');
        star.className = 'star';
        star.style.left = Math.random() * 100 + '%';
        star.style.top = Math.random() * 100 + '%';
        star.style.setProperty('--duration', (2 + Math.random() * 4) + 's');
        star.style.animationDelay = Math.random() * 5 + 's';
        star.style.width = star.style.height = (1 + Math.random() * 2) + 'px';
        container.appendChild(star);
    }
}

function getRoleBadgeClass(role) {
    const roleLower = (role || '').toLowerCase();
    if (roleLower === 'werewolf') return 'werewolf';
    if (roleLower === 'seer') return 'seer';
    if (roleLower === 'guard') return 'guard';
    if (roleLower === 'witch') return 'witch';
    if (roleLower === 'hunter') return 'hunter';
    return 'villager';
}

function formatTimestamp() {
    return new Date().toLocaleTimeString();
}

async function refreshArena() {
    try {
        const resp = await fetch(WEREWOLF_STATE_URL);
        const state = await resp.json();
        
        const stateStr = JSON.stringify(state);
        if (stateStr === lastState) return;
        lastState = stateStr;
        
        updatePhaseBanner(state);
        updateInfoBar(state);
        updateCharacterGrid(state);
        updateNightMessage(state);
        updateGameLog(state);
        updateWinner(state);
        
    } catch (err) {
        console.error('Failed to refresh werewolf state:', err);
    }
}

function updatePhaseBanner(state) {
    const banner = document.getElementById('phase-banner');
    if (!banner) return;
    
    const phase = state.phase || 'setup';
    const round = state.round || 1;
    
    let icon, text, message, cssClass;
    
    if (state.winner) {
        icon = '🏁';
        text = 'GAME OVER';
        message = state.winner === 'Village' ? 'The Village survives!' : 'The Wolves take over!';
        cssClass = 'game-over';
    } else if (phase.includes('night')) {
        icon = '🌙';
        text = `Night ${Math.floor(round)}`;
        message = 'The wolves are hunting...';
        cssClass = 'night';
    } else if (phase.includes('day') || phase.includes('vote')) {
        icon = '☀️';
        text = `Day ${Math.floor(round)}`;
        message = phase.includes('vote') ? 'Voting in progress!' : 'Discussion time';
        cssClass = 'day';
    } else if (phase.includes('announce')) {
        icon = '📢';
        text = 'Night Results';
        message = 'Revealing what happened...';
        cssClass = 'day';
    } else {
        icon = '🐺';
        text = 'Game Active';
        message = '';
        cssClass = 'night';
    }
    
    banner.innerHTML = `
        <span class="phase-icon">${icon}</span>
        <h2 class="phase-text">${text}</h2>
        ${message ? `<p class="phase-message">${message}</p>` : ''}
    `;
    banner.className = `phase-banner ${cssClass}-banner`;
}

function updateInfoBar(state) {
    const bar = document.getElementById('game-info-bar');
    if (!bar) return;
    
    const alive = state.alive_count || 0;
    const total = (state.players || []).length;
    const phase = state.phase || 'setup';
    const round = state.round || 1;
    
    let phaseLabel = phase.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
    if (phase === 'game_over') phaseLabel = 'Game Over';
    
    let phaseClass = 'day';
    if (phase.includes('night') || phase === 'setup') phaseClass = 'night';
    if (phase === 'game_over') phaseClass = 'game-over';
    
    bar.innerHTML = `
        <div class="info-item">
            <span class="info-label">Round</span>
            <span class="info-value">${round}</span>
        </div>
        <div class="info-item">
            <span class="info-label">Alive</span>
            <span class="info-value" style="color: ${alive > total/2 ? '#44ff88' : '#ff6644'}">${alive} / ${total}</span>
        </div>
        <div class="info-item">
            <span class="phase-badge ${phaseClass}">${phaseLabel}</span>
        </div>
    `;
}

function updateCharacterGrid(state) {
    const grid = document.getElementById('character-grid');
    if (!grid) return;
    
    const players = state.players || [];
    
    let html = '';
    for (const p of players) {
        const emoji = EMOJI_MAP[p.name] || '👤';
        const color = COLOR_MAP[p.name] || '#8888aa';
        const alive = p.alive !== false;
        const role = p.role || 'Unknown';
        
        let statusClass = alive ? 'alive' : 'dead';
        let roleLabel = '';
        let dotClass = alive ? 'alive' : 'dead';
        
        // Show role if revealed
        if (!alive || (role !== 'Unknown' && role !== 'Unknown')) {
            if (role !== 'Unknown') {
                const badgeClass = getRoleBadgeClass(role);
                roleLabel = `<span class="role-badge ${badgeClass}">${role}</span>`;
                if (role.toLowerCase() === 'werewolf' && !alive) {
                    statusClass += ' wolf-revealed';
                    dotClass = 'wolf';
                }
            }
        } else {
            roleLabel = '<span class="role-badge villager">?</span>';
        }
        
        const statusText = alive ? 'Alive' : 'Dead';
        const dripHtml = !alive ? '<div class="death-drip"></div>' : '';
        
        html += `
            <div class="character-card ${statusClass}" style="animation-delay: ${Math.random() * 0.5}s">
                <div class="avatar-container" style="border-color: ${color}">
                    <span class="floating-emoji">${emoji}</span>
                </div>
                ${dripHtml}
                <div class="character-name" style="color: ${color}">${p.name}</div>
                <div class="character-role">${roleLabel}</div>
                <div>
                    <span class="status-dot ${dotClass}"></span>
                    <span style="font-size: 0.75rem; color: ${alive ? '#88ff88' : '#888'}">${statusText}</span>
                </div>
            </div>
        `;
    }
    
    grid.innerHTML = html;
}

function updateNightMessage(state) {
    const bar = document.getElementById('night-message-bar');
    if (!bar) return;
    
    const msg = state.night_message || '';
    if (!msg) {
        bar.style.display = 'none';
        return;
    }
    
    bar.style.display = 'block';
    
    let cssClass = '';
    if (msg.includes('killed') || msg.includes('died') || msg.includes('poisoned')) {
        cssClass = 'death';
    } else if (msg.includes('saved') || msg.includes('peaceful')) {
        cssClass = 'safe';
    }
    
    bar.className = `night-message-bar ${cssClass}`;
    bar.textContent = msg;
}

function updateGameLog(state) {
    const log = document.getElementById('game-log');
    if (!log) return;
    
    const entries = state.game_log || [];
    let html = '';
    
    for (const entry of entries) {
        let cssClass = 'log-entry';
        
        if (entry.includes('killed') || entry.includes('die') || entry.includes('poisoned')) {
            cssClass += ' death';
        } else if (entry.includes('Night') || entry.includes('night')) {
            cssClass += ' night';
        } else if (entry.includes('vote') || entry.includes('Vote') || entry.includes('lynched')) {
            cssClass += ' vote';
        } else if (entry.includes(':')) {
            cssClass += ' speech';
        } else if (entry.includes('wins') || entry.includes('Wins') || entry.includes('over')) {
            cssClass += ' game-over';
        } else {
            cssClass += ' day';
        }
        
        html += `<div class="${cssClass}"><span class="log-timestamp">${formatTimestamp()}</span>${escapeHtml(entry)}</div>`;
    }
    
    log.innerHTML = html;
    log.scrollTop = log.scrollHeight;
}

function updateWinner(state) {
    const container = document.getElementById('winner-announcement');
    if (!container) return;
    
    if (!state.winner) {
        container.style.display = 'none';
        return;
    }
    
    container.style.display = 'block';
    const isVillage = state.winner === 'Village';
    
    container.innerHTML = `
        <h1 class="winner-title ${isVillage ? 'village-wins' : 'wolf-wins'}">
            ${isVillage ? '🎉 VILLAGE WINS!' : '🐺 WEREWOLVES WIN!'}
        </h1>
        <p class="winner-subtitle">Game ${state.game_count || '?'} is over. Next game starting soon...</p>
    `;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', initWerewolfArena);

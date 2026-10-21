/**
 * JevTrader Apex — Real-Time Cockpit Engine
 * Handles live telemetry polling, interactive ratchet visualization,
 * and Web Audio synthesized chimes.
 */

// Web Audio synthesizer for alert chimes
class ChimeSynth {
  constructor() {
    this.ctx = null;
    this.enabled = true;
  }

  _init() {
    if (!this.ctx) {
      const AudioContext = window.AudioContext || window.webkitAudioContext;
      this.ctx = new AudioContext();
    }
    if (this.ctx.state === 'suspended') {
      this.ctx.resume();
    }
  }

  playFillChime() {
    if (!this.enabled) return;
    try {
      this._init();
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(587.33, this.ctx.currentTime); // D5
      osc.frequency.exponentialRampToValueAtTime(880.00, this.ctx.currentTime + 0.15); // A5
      gain.gain.setValueAtTime(0.2, this.ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + 0.35);
      osc.connect(gain);
      gain.connect(this.ctx.destination);
      osc.start();
      osc.stop(this.ctx.currentTime + 0.36);
    } catch (e) {}
  }

  playRatchetChime() {
    if (!this.enabled) return;
    try {
      this._init();
      const osc = this.ctx.createOscillator();
      const gain = this.ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(440.00, this.ctx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(659.25, this.ctx.currentTime + 0.1);
      osc.frequency.exponentialRampToValueAtTime(880.00, this.ctx.currentTime + 0.2);
      gain.gain.setValueAtTime(0.18, this.ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + 0.4);
      osc.connect(gain);
      gain.connect(this.ctx.destination);
      osc.start();
      osc.stop(this.ctx.currentTime + 0.41);
    } catch (e) {}
  }
}

const chimes = new ChimeSynth();
let previousPositionsCount = 0;
let previousRatchetLevels = {};

// DOM Elements
const kpiEquity = document.getElementById('kpi-equity');
const kpiFreeMargin = document.getElementById('kpi-free-margin');
const kpiUsedMargin = document.getElementById('kpi-used-margin');
const marginBarFill = document.getElementById('margin-bar-fill');
const kpiPnlBadge = document.getElementById('kpi-pnl-badge');

const kpiBreadthPct = document.getElementById('kpi-breadth-pct');
const kpiBreadthRatio = document.getElementById('kpi-breadth-ratio');
const kpiRegimeTag = document.getElementById('kpi-regime-tag');
const breadthGaugeFill = document.getElementById('breadth-gauge-fill');

const kpiBtcPrice = document.getElementById('kpi-btc-price');
const kpiBtcChange = document.getElementById('kpi-btc-change');
const kpiBtcImpulse = document.getElementById('kpi-btc-impulse');

const kpiRealizedPnl = document.getElementById('kpi-realized-pnl');
const kpiActiveSlots = document.getElementById('kpi-active-slots');
const kpiCircuitStatus = document.getElementById('kpi-circuit-status');

const headerLastTick = document.getElementById('header-last-tick');
const headerModelName = document.getElementById('header-model-name');
const headerExitMode = document.getElementById('header-exit-mode');
const activePositionsContainer = document.getElementById('active-positions-container');

const scannerTbody = document.getElementById('scanner-tbody');
const scannerCount = document.getElementById('scanner-count');
const decisionsTbody = document.getElementById('decisions-tbody');
const decisionsCount = document.getElementById('decisions-count');
const tradesTbody = document.getElementById('trades-tbody');
const historyCount = document.getElementById('history-count');
const soundBtn = document.getElementById('sound-btn');
const soundIcon = document.getElementById('sound-icon');

// Sound toggle
soundBtn.addEventListener('click', () => {
  chimes.enabled = !chimes.enabled;
  soundIcon.textContent = chimes.enabled ? '🔊' : '🔇';
  soundBtn.style.opacity = chimes.enabled ? '1' : '0.5';
});

// Tab Switching
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    
    btn.classList.add('active');
    const targetId = btn.getAttribute('data-tab');
    const targetPanel = document.getElementById(targetId);
    if (targetPanel) {
      targetPanel.classList.add('active');
    }
  });
});

// Format numbers
function formatPrice(val) {
  if (val === undefined || val === null) return '0.00';
  const num = parseFloat(val);
  if (num >= 1000) return num.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 2 });
  if (num >= 1) return num.toFixed(4);
  return num.toFixed(6);
}

function formatUSD(val) {
  const num = parseFloat(val) || 0.0;
  return (num >= 0 ? '+' : '') + '$' + Math.abs(num).toFixed(2);
}

// 1. Fetch Live State (/api/state)
async function fetchState() {
  try {
    const res = await fetch('/api/state?t=' + Date.now());
    if (!res.ok) return;
    const data = await res.json();
    renderState(data);
  } catch (e) {
    console.error('Error fetching state:', e);
  }
}

function renderState(state) {
  // Update header time
  if (state.timestamp) {
    const dt = new Date(state.timestamp * 1000);
    headerLastTick.textContent = dt.toTimeString().split(' ')[0];
  }

  if (state.system) {
    headerModelName.textContent = state.system.entry_model || 'liquid/d1-ultra';
    headerExitMode.textContent = state.system.exit_mode === 'ratchet_trail' ? 'Ratchet Trailing 🔒' : 'Two-Tier (50/50)';
  }

  // 1. KPIs: Equity & Margin
  const eq = state.equity || 107.0;
  const freeM = state.free_margin !== undefined ? state.free_margin : eq;
  const usedM = state.used_margin || 0.0;
  kpiEquity.textContent = eq.toFixed(2);
  kpiFreeMargin.textContent = '$' + freeM.toFixed(2);
  kpiUsedMargin.textContent = '$' + usedM.toFixed(2);
  
  const marginPct = eq > 0 ? Math.min(100, Math.max(0, (usedM / eq) * 100)) : 0;
  marginBarFill.style.width = marginPct.toFixed(1) + '%';

  // 2. KPIs: Market Regime & Breadth
  const breadthPct = state.breadth_pct || 0.0;
  kpiBreadthPct.textContent = breadthPct.toFixed(1) + '%';
  kpiBreadthRatio.textContent = `${state.pairs_above_vwap || 0} / ${state.total_pairs || 804} USDT Pairs`;
  breadthGaugeFill.style.width = Math.min(100, Math.max(0, breadthPct)) + '%';

  const regime = state.regime || 'CHOP_ROTATION';
  kpiRegimeTag.textContent = regime.replace('_', ' ');
  kpiRegimeTag.className = 'regime-tag ' + (regime === 'BULL_ALPHA' ? 'bull' : (regime === 'BLOOD_STREETS' ? 'bear' : ''));

  // 3. KPIs: Bitcoin Sentinel
  kpiBtcPrice.textContent = (state.btc_price || 0).toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const btcChange = state.btc_change_24h_pct || 0.0;
  kpiBtcChange.textContent = (btcChange >= 0 ? '+' : '') + btcChange.toFixed(2) + '%';
  kpiBtcChange.className = 'btc-badge ' + (btcChange >= 0 ? 'positive' : 'negative');
  const impulse = state.btc_impulse_15m || 0.0;
  kpiBtcImpulse.textContent = (impulse >= 0 ? '+' : '') + impulse.toFixed(2) + '%';

  // 4. KPIs: Capital Velocity & Shield
  const realized = state.total_realized_pnl || 0.0;
  kpiRealizedPnl.textContent = (realized >= 0 ? '+' : '') + realized.toFixed(2);
  const activeCount = state.active_positions ? state.active_positions.length : 0;
  kpiActiveSlots.textContent = `${activeCount} / ${state.system?.max_positions || 8} Max`;

  if (state.circuit_breaker_active) {
    kpiCircuitStatus.textContent = `COOLDOWN (${state.circuit_breaker_seconds_left}s)`;
    kpiCircuitStatus.className = 'sub-val crimson-text';
  } else {
    kpiCircuitStatus.textContent = `${state.consecutive_losses || 0} Losses (OK)`;
    kpiCircuitStatus.className = 'sub-val text-emerald';
  }

  // 5. Active Positions & Ratchet Runner
  renderActivePositions(state.active_positions || []);

  // 6. Evaluated Candidates Table
  renderScannerFeed(state.evaluated_candidates || []);
}

function renderActivePositions(positions) {
  // Check for audio trigger
  if (positions.length > previousPositionsCount) {
    chimes.playFillChime();
  }
  previousPositionsCount = positions.length;

  if (positions.length === 0) {
    activePositionsContainer.innerHTML = `
      <div class="radar-empty-state glassmorphism" id="empty-positions-state">
        <div class="radar-circle-wrap">
          <div class="radar-pulse"></div>
          <div class="radar-sweep"></div>
          <div class="radar-center-icon">🎯</div>
        </div>
        <div class="empty-state-text">
          <h3>ACTIVE SCANNING & CONFLUENCE HUNTING</h3>
          <p>Analyzing orderbook walls, taker buy aggression, and VWAP impulses across 804 USDT pairs.</p>
          <div class="empty-tags">
            <span class="empty-tag">Anti-Clustering: Active</span>
            <span class="empty-tag">Min SL Buffer: 0.60%</span>
            <span class="empty-tag">Ratchet Trailing: Ready</span>
          </div>
        </div>
      </div>
    `;
    return;
  }

  let html = '';
  positions.forEach(pos => {
    const isLong = pos.direction === 'LONG';
    const pnlUsd = pos.unrealized_pnl_usd || 0.0;
    const pnlPct = pos.unrealized_pnl_pct || 0.0;
    const isProfit = pnlUsd >= 0;
    const ratchetLvl = pos.ratchet_level || 0;

    // Trigger chime on ratchet step advance
    if (previousRatchetLevels[pos.trade_id] !== undefined && ratchetLvl > previousRatchetLevels[pos.trade_id]) {
      chimes.playRatchetChime();
    }
    previousRatchetLevels[pos.trade_id] = ratchetLvl;

    html += `
      <div class="position-card glassmorphism ${isLong ? '' : 'short-pos'}">
        <div class="pos-card-header">
          <div class="pos-symbol-group">
            <span class="pos-symbol">${pos.symbol}</span>
            <span class="pos-direction-badge ${isLong ? 'long' : 'short'}">${pos.direction} ${pos.leverage ? pos.leverage + 'x' : '10x'}</span>
            ${pos.layer === 2 ? '<span class="pos-direction-badge gold-badge">⚡ LAYER 2 PYRAMID</span>' : ''}
            <span class="empty-tag">Qty: ${pos.remaining_qty}</span>
            <span class="empty-tag">Entry: $${formatPrice(pos.entry_price)}</span>
            <span class="empty-tag">Mark: $${formatPrice(pos.current_price)}</span>
            ${pos.funding_rate !== undefined ? `<span class="empty-tag ${pos.funding_rate < 0 ? 'emerald-text' : ''}">Funding: ${(pos.funding_rate >= 0 ? '+' : '')}${pos.funding_rate.toFixed(4)}%</span>` : ''}
          </div>
          <div class="pos-pnl-group">
            <div class="pos-pnl-val mono-font ${isProfit ? 'emerald-text' : 'crimson-text'}">
              ${formatUSD(pnlUsd)}
            </div>
            <div class="pos-pnl-pct mono-font ${isProfit ? 'emerald-text' : 'crimson-text'}">
              ${(pnlPct >= 0 ? '+' : '')}${pnlPct.toFixed(2)}% (${(pos.highest_r_reached || 0).toFixed(1)}R)
            </div>
          </div>
        </div>

        <!-- Ratchet Progress Track -->
        <div class="ratchet-track-container">
          <div class="ratchet-track-header">
            <span>RATCHET RUNNER TRAIL ENGINE</span>
            <span>CURRENT SL: $${formatPrice(pos.stop_loss_price)} ${pos.is_be_locked ? '🔒 (RISK: $0.00)' : ''}</span>
          </div>
          <div class="ratchet-steps-bar">
            <!-- Node 1: Entry -->
            <div class="ratchet-step-node completed">
              <div class="node-circle">0R</div>
              <span class="node-label">Entry</span>
            </div>

            <!-- Node 2: TP1 (+1.5R) -->
            <div class="ratchet-step-node ${pos.is_tp1_filled ? 'completed' : 'active'}">
              <div class="node-circle">${pos.is_tp1_filled ? '✓' : '1.5R'}</div>
              <span class="node-label">TP1 (+1.5R)</span>
            </div>

            <!-- Node 3: Ratchet 1 (+1.0R Lock) -->
            <div class="ratchet-step-node ${ratchetLvl >= 1 ? 'completed' : (pos.is_tp1_filled ? 'active' : '')}">
              <div class="node-circle">${ratchetLvl >= 1 ? '🔒' : '2R'}</div>
              <span class="node-label">Lock +1.0R</span>
            </div>

            <!-- Node 4: Ratchet 2 (+2.0R Lock) -->
            <div class="ratchet-step-node ${ratchetLvl >= 2 ? 'completed' : (ratchetLvl === 1 ? 'active' : '')}">
              <div class="node-circle">${ratchetLvl >= 2 ? '🔒' : '3R'}</div>
              <span class="node-label">Lock +2.0R</span>
            </div>

            <!-- Node 5: Moonshot Runner -->
            <div class="ratchet-step-node ${ratchetLvl >= 3 ? 'completed' : (ratchetLvl === 2 ? 'active' : '')}">
              <div class="node-circle">🚀</div>
              <span class="node-label">Moonshot</span>
            </div>
          </div>
        </div>
      </div>
    `;
  });

  activePositionsContainer.innerHTML = html;
}

function renderScannerFeed(candidates) {
  scannerCount.textContent = candidates.length;
  if (candidates.length === 0) {
    scannerTbody.innerHTML = '<tr><td colspan="10" class="table-loading">Scanning universe for qualified momentum breakouts...</td></tr>';
    return;
  }

  let html = '';
  candidates.forEach(c => {
    const isLong = c.direction === 'LONG';
    const score = c.score || 0;
    const scoreFillClass = score >= 80 ? 'high' : (score >= 65 ? 'medium' : 'low');
    const fr = c.funding_rate !== undefined ? c.funding_rate : 0.0;
    const isSqueeze = c.is_squeeze_breakout || false;

    const h4Trend = c.tf_4h_trend || 'NEUTRAL';
    const h1Trend = c.tf_1h_trend || 'NEUTRAL';
    const m15Trend = c.tf_15m_trend || 'NEUTRAL';
    const m5Trend = c.tf_5m_trend || 'NEUTRAL';
    const m1Trend = c.tf_1m_trend || 'NEUTRAL';

    const h4Color = h4Trend === 'BULL' ? 'emerald-dot' : (h4Trend === 'BEAR' ? 'crimson-dot' : 'amber-dot');
    const h1Color = h1Trend === 'BULL' ? 'emerald-dot' : (h1Trend === 'DUMPING' || h1Trend === 'BEAR' ? 'crimson-dot' : 'amber-dot');
    const m15Color = m15Trend === 'BULL' ? 'emerald-dot' : (m15Trend === 'BEAR' ? 'crimson-dot' : 'amber-dot');
    const m5Color = m5Trend === 'BULL' ? 'emerald-dot' : (m5Trend === 'BEAR' ? 'crimson-dot' : 'amber-dot');
    const m1Color = (m1Trend === 'BULL' || m1Trend === 'PUMPING') ? 'emerald-dot' : (m1Trend === 'BEAR' || m1Trend === 'DUMPING' ? 'crimson-dot' : 'amber-dot');

    const h4Change = c.tf_4h_change_pct !== undefined ? (c.tf_4h_change_pct >= 0 ? '+' : '') + c.tf_4h_change_pct.toFixed(1) + '%' : '';
    const h1Change = c.tf_1h_change_pct !== undefined ? (c.tf_1h_change_pct >= 0 ? '+' : '') + c.tf_1h_change_pct.toFixed(1) + '%' : '';
    const hasVeto = c.htf_conflict === true;

    html += `
      <tr>
        <td class="mono-font">
          <strong>${c.symbol}</strong>
          ${isSqueeze ? '<span class="squeeze-tag">⚡ SQUEEZE</span>' : ''}
        </td>
        <td><span class="pos-direction-badge ${isLong ? 'long' : 'short'}">${c.direction}</span></td>
        <td class="mono-font">$${formatPrice(c.price)}</td>
        <td>
          <div class="score-pill-wrap">
            <span class="mono-font">${score.toFixed(1)}</span>
            <div class="score-track">
              <div class="score-fill ${scoreFillClass}" style="width: ${Math.min(100, score)}%;"></div>
            </div>
          </div>
        </td>
        <td>
          <div class="mtf-matrix-wrap" title="4H: ${h4Trend} ${h4Change} | 1H: ${h1Trend} ${h1Change} | 15M: ${m15Trend} | 5M: ${m5Trend} | 1M: ${m1Trend}">
            <span class="mtf-badge"><span class="mtf-dot ${h4Color}"></span>4H</span>
            <span class="mtf-badge"><span class="mtf-dot ${h1Color}"></span>1H</span>
            <span class="mtf-badge"><span class="mtf-dot ${m15Color}"></span>15M</span>
            <span class="mtf-badge"><span class="mtf-dot ${m5Color}"></span>5M</span>
            <span class="mtf-badge"><span class="mtf-dot ${m1Color}"></span>1M</span>
            ${hasVeto ? '<span class="veto-tag">⛔ HTF VETO</span>' : ''}
          </div>
        </td>
        <td class="mono-font ${fr < 0 ? 'emerald-text' : (fr > 0.05 ? 'crimson-text' : '')}">
          ${(fr >= 0 ? '+' : '')}${fr.toFixed(4)}%
        </td>
        <td class="mono-font ${c.relative_strength >= 0 ? 'emerald-text' : 'crimson-text'}">
          ${(c.relative_strength >= 0 ? '+' : '')}${c.relative_strength.toFixed(1)}%
        </td>
        <td class="mono-font">${c.cvd_buy_pct.toFixed(1)}%</td>
        <td class="mono-font ${c.imbalance >= 0 ? 'cyan-text' : 'amber-text'}">
          ${(c.imbalance >= 0 ? '+' : '')}${c.imbalance.toFixed(2)}
        </td>
        <td class="mono-font">${c.wall_distance_pct.toFixed(2)}%</td>
        <td>
          <span class="empty-tag ${hasVeto ? 'crimson-text' : (score >= 80 ? 'high-conviction-tag' : (c.is_qualified ? 'emerald-text' : ''))}">
            ${hasVeto ? '⛔ HTF VETO' : (score >= 80 ? '🔥 CONVICTION' : (c.is_qualified ? 'QUALIFIED (≥70)' : 'EVALUATING'))}
          </span>
        </td>
      </tr>
    `;
  });
  scannerTbody.innerHTML = html;
}

// 2. Fetch AI Decisions Log (/api/decisions)
async function fetchDecisions() {
  try {
    const res = await fetch('/api/decisions?t=' + Date.now());
    if (!res.ok) return;
    const data = await res.json();
    const decisions = data.decisions || [];
    decisionsCount.textContent = decisions.length;

    if (decisions.length === 0) {
      decisionsTbody.innerHTML = '<tr><td colspan="9" class="table-loading">No decision records yet.</td></tr>';
      return;
    }

    let html = '';
    decisions.forEach(d => {
      const isLong = d.direction === 'LONG';
      const isApproved = d.action_prob >= 0.70;
      const dt = new Date(d.created_ms);
      const timeStr = dt.toTimeString().split(' ')[0];

      html += `
        <tr>
          <td class="mono-font">${timeStr}</td>
          <td class="mono-font"><strong>${d.symbol}</strong></td>
          <td><span class="pos-direction-badge ${isLong ? 'long' : 'short'}">${d.direction}</span></td>
          <td class="cyan-text mono-font">${d.model_name}</td>
          <td class="mono-font">${d.action_prob.toFixed(4)}</td>
          <td class="mono-font">${d.wall_prob.toFixed(4)}</td>
          <td class="mono-font">${(d.latency_sec * 1000).toFixed(0)}ms</td>
          <td class="mono-font">$${(d.cost_usd || 0).toFixed(6)}</td>
          <td>
            <span class="pnl-badge ${isApproved ? 'positive' : 'negative'}">
              ${isApproved ? 'APPROVED' : 'REJECTED'}
            </span>
          </td>
        </tr>
      `;
    });
    decisionsTbody.innerHTML = html;
  } catch (e) {
    console.error('Error fetching decisions:', e);
  }
}

// 3. Fetch Historical Trades (/api/trades)
async function fetchTrades() {
  try {
    const res = await fetch('/api/trades?t=' + Date.now());
    if (!res.ok) return;
    const data = await res.json();
    const trades = data.trades || [];
    historyCount.textContent = trades.length;

    if (trades.length === 0) {
      tradesTbody.innerHTML = '<tr><td colspan="8" class="table-loading">No historical trades found in SQLite.</td></tr>';
      return;
    }

    let html = '';
    trades.forEach(t => {
      const isLong = t.direction === 'LONG';
      const dt = new Date(t.updated_ms);
      const timeStr = dt.toLocaleTimeString();

      let statusBadgeClass = 'empty-tag';
      if (t.status.includes('TP1') || t.status.includes('TP2') || t.status.includes('RATCHET')) {
        statusBadgeClass = 'pnl-badge positive';
      } else if (t.status.includes('SL') || t.status.includes('WATCHDOG')) {
        statusBadgeClass = 'pnl-badge negative';
      }

      html += `
        <tr>
          <td class="mono-font">${t.trade_id}</td>
          <td class="mono-font"><strong>${t.symbol}</strong></td>
          <td><span class="pos-direction-badge ${isLong ? 'long' : 'short'}">${t.direction}</span></td>
          <td class="mono-font">$${formatPrice(t.entry_price)}</td>
          <td class="mono-font">$${formatPrice(t.stop_loss)}</td>
          <td class="mono-font">$${formatPrice(t.tp1)}</td>
          <td><span class="${statusBadgeClass}">${t.status}</span></td>
          <td class="mono-font">${timeStr}</td>
        </tr>
      `;
    });
    tradesTbody.innerHTML = html;
  } catch (e) {
    console.error('Error fetching trades:', e);
  }
}

// Kickoff polling cycles
fetchState();
fetchDecisions();
fetchTrades();

setInterval(fetchState, 1500);       // Live telemetry every 1.5s
setInterval(fetchDecisions, 4000);   // Decisions log every 4s
setInterval(fetchTrades, 6000);      // Trades list every 6s

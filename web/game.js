const detail = document.querySelector('#game-detail');
const query = new URLSearchParams(location.search);
const sport = query.get('sport');
const kalshiTicker = query.get('kalshi');
const polymarketSlug = query.get('polymarket');
const safe = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const price = value => value == null ? '—' : `$${Number(value).toFixed(3)}`;
const startTime = value => value ? new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(value)) : 'Time unavailable';

function venueCard(name, iconClass, icon, url, headings, rows) {
  const body = rows.length ? `<table><thead><tr>${headings.map(heading => `<th>${safe(heading)}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${value}</td>`).join('')}</tr>`).join('')}</tbody></table>` : '<p class="detail-empty">No current market quotes were returned.</p>';
  return `<section class="venue-detail"><header><b class="venue-icon ${iconClass}">${icon}</b><strong>${name}</strong>${url ? `<a href="${safe(url)}" target="_blank" rel="noopener noreferrer">Open venue ↗</a>` : ''}</header>${body}</section>`;
}

const categories = [
  ['moneyline', 'Two-way moneylines', 'Winner markets only; soccer three-way markets remain review-only.'],
  ['spread', 'Spreads', 'Compare the exact line, push rules, game period, and overtime treatment.'],
  ['total', 'Totals', 'Compare the exact total, over/under direction, and settlement scope.'],
  ['props', 'Player & live props', 'Shown when supplied by a venue. They are not automatically matched across venues.'],
];

function categoryMarkets(record, category) {
  const fallbackKalshi = category === 'moneyline' ? (record.kalshi_moneyline_asks || []).map(market => ({ ...market, market: market.contract })) : [];
  const fallbackPolymarket = category === 'moneyline' ? (record.polymarket_us_displayed_moneyline_quotes || []).map(quote => ({ ...quote, market: 'Moneyline' })) : [];
  let kalshi = (record.kalshi_market_breakdown || fallbackKalshi).filter(market => market.category === category || (!market.category && category === 'moneyline'));
  let polymarket = (record.polymarket_us_market_breakdown || fallbackPolymarket).filter(market => market.category === category || (!market.category && category === 'moneyline'));
  // Reports created before category support still have valid moneyline quotes.
  if (category === 'moneyline' && !kalshi.length) kalshi = fallbackKalshi;
  if (category === 'moneyline' && !polymarket.length) polymarket = fallbackPolymarket;
  return { kalshi, polymarket };
}

function marketPeriod(label) {
  const text = String(label || '').toLowerCase();
  const inning = text.match(/\b(\d+)(st|nd|rd|th)\s+inning\b/);
  if (/first 5|first five/.test(text)) return 'First 5 innings';
  if (/first half/.test(text)) return 'First half';
  if (/second half/.test(text)) return 'Second half';
  if (/first quarter/.test(text)) return 'First quarter';
  if (/second quarter/.test(text)) return 'Second quarter';
  if (/third quarter/.test(text)) return 'Third quarter';
  if (/fourth quarter/.test(text)) return 'Fourth quarter';
  return inning ? `${inning[1]}${inning[2]} inning` : 'Full game';
}

function teamKey(value) {
  return String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function canonicalTeam(record, value) {
  const candidate = teamKey(value);
  const team = (record.teams || []).find(item => {
    const name = teamKey(item.name);
    return candidate && name && (candidate === name || candidate.includes(name) || name.includes(candidate));
  });
  return team?.name || String(value || 'Unknown team');
}

function signedLine(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return null;
  return `${numeric > 0 ? '+' : '-'}${Math.abs(numeric).toFixed(1)}`;
}

function oppositeLine(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? signedLine(-numeric) : null;
}

function selection(key, label, quote) {
  return quote == null ? null : { key, label, quote };
}

function kalshiSelections(record, category, markets) {
  return markets.flatMap(market => {
    const label = String(market.market || market.contract || '');
    const period = marketPeriod(label);
    if (category === 'moneyline') {
      const match = label.match(/^(.*?)\s+wins?\??$/i);
      if (!match) return [];
      const team = canonicalTeam(record, match[1]);
      return [selection(`winner:${period}:${teamKey(team)}`, `${team} to win`, market.yes_ask)].filter(Boolean);
    }
    if (category === 'spread') {
      const match = label.match(/^(.*?)\s+wins?\s+by\s+over\s+([+-]?\d+(?:\.\d+)?)/i);
      if (!match) return [];
      const team = canonicalTeam(record, match[1]);
      const line = signedLine(-Number(match[2]));
      return [selection(`spread:${period}:${teamKey(team)}:${line}`, `${team} ${line}`, market.yes_ask)].filter(Boolean);
    }
    if (category === 'total') {
      const match = label.match(/^(over|under)\s+(\d+(?:\.\d+)?)/i);
      if (!match) return [];
      const line = Number(match[2]).toFixed(1);
      return [
        selection(`total:${period}:over:${line}`, `Over ${line}`, market.yes_ask),
        selection(`total:${period}:under:${line}`, `Under ${line}`, market.no_ask),
      ].filter(Boolean);
    }
    return [];
  });
}

function polymarketSelections(record, category, markets) {
  return markets.flatMap(market => {
    const label = String(market.market || '');
    const period = marketPeriod(label);
    const outcome = canonicalTeam(record, market.outcome);
    if (category === 'moneyline') {
      return [selection(`winner:${period}:${teamKey(outcome)}`, `${outcome} to win`, market.displayed_quote)].filter(Boolean);
    }
    if (category === 'spread') {
      const match = label.match(/will\s+the\s+(.+?)\s+cover\s+([+-]?\d+(?:\.\d+)?)\s+vs\b/i);
      if (!match) return [];
      const listedTeam = canonicalTeam(record, match[1]);
      const line = teamKey(listedTeam) === teamKey(outcome) ? signedLine(match[2]) : oppositeLine(match[2]);
      return [selection(`spread:${period}:${teamKey(outcome)}:${line}`, `${outcome} ${line}`, market.displayed_quote)].filter(Boolean);
    }
    if (category === 'total') {
      const match = label.match(/\bmore\s+than\s+(\d+(?:\.\d+)?)/i);
      const direction = /under/i.test(String(market.outcome)) ? 'under' : /over/i.test(String(market.outcome)) ? 'over' : null;
      if (!match || !direction) return [];
      const line = Number(match[1]).toFixed(1);
      return [selection(`total:${period}:${direction}:${line}`, `${direction[0].toUpperCase()}${direction.slice(1)} ${line}`, market.displayed_quote)].filter(Boolean);
    }
    return [];
  });
}

function categorySection(record, category, title, note) {
  const { kalshi, polymarket } = categoryMarkets(record, category);
  const kalshiByKey = new Map(kalshiSelections(record, category, kalshi).map(item => [item.key, item]));
  const polyByKey = new Map(polymarketSelections(record, category, polymarket).map(item => [item.key, item]));
  const rows = [...kalshiByKey.keys()].filter(key => polyByKey.has(key)).map(key => ({
    label: kalshiByKey.get(key).label,
    kalshi: kalshiByKey.get(key).quote,
    polymarket: polyByKey.get(key).quote,
  })).sort((left, right) => left.label.localeCompare(right.label));
  if (!rows.length) return '';
  const body = rows.map(row => `<tr><td><strong>${safe(row.label)}</strong></td><td><span class="price">${price(row.kalshi)}</span></td><td><span class="price">${price(row.polymarket)}</span></td></tr>`).join('');
  return `<section class="detail-market-section"><div><h2>${title}</h2><p>${note}</p></div><div class="market-comparison"><table><thead><tr><th>Market</th><th><span class="book-heading"><b class="venue-icon kalshi">K</b>Kalshi</span></th><th><span class="book-heading"><b class="venue-icon polymarket">◇</b>Polymarket US</span></th></tr></thead><tbody>${body}</tbody></table></div></section>`;
}

function render(record) {
  const title = record.polymarket_us_title || record.kalshi_title || 'Game breakdown';
  detail.innerHTML = `<div class="game-detail-header"><div><p class="eyebrow">${safe(sport || 'SPORT').toUpperCase()} · MARKET BREAKDOWN</p><h1>${safe(title)}</h1><p class="meta">Starts ${safe(startTime(record.start_time))}</p></div></div>${categories.map(category => categorySection(record, ...category)).join('')}<p class="detail-note">Quotes are for research. Confirm current executable prices, fees, market scope, and settlement rules on each venue before acting.</p>`;
}

async function load() {
  if (!sport || !kalshiTicker || !polymarketSlug) throw new Error('This game link is incomplete. Return to the market board and select the game again.');
  const requestQuery = new URLSearchParams({ sport, kalshi: kalshiTicker, polymarket: polymarketSlug });
  const response = await fetch(`/api/game?${requestQuery}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Unable to load this game.');
  const record = data.record;
  if (!record) throw new Error('This game is no longer active or is unavailable in the latest report.');
  render(record);
}

load().catch(error => { detail.innerHTML = `<div class="empty"><h2>Game unavailable</h2><p>${safe(error.message)}</p></div>`; });
setInterval(() => { document.querySelector('#clock').textContent = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' }).format(new Date()); }, 1000);

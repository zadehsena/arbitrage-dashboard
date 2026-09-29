const tabs = [...document.querySelectorAll('.tab')];
const tableWrap = document.querySelector('#table-wrap');
const meta = document.querySelector('#meta');
const notice = document.querySelector('#notice');
const refresh = document.querySelector('#refresh');
const home = document.querySelector('#home');
const matchSummary = document.querySelector('#match-summary');
const sourceSummary = document.querySelector('#source-summary');
let activeSport = null;
let activeLeagues = [];
let activeLabel = '';
let latestRecords = [];
let nextSportOffset = null;
let sportRecordTotal = 0;
const refreshingSports = new Set();
const oddsRefreshIntervalMs = 60_000;
const sportPageSize = 25;
const walletRefreshIntervalMs = 30_000;
let walletRefreshInFlight = false;
const renderedQuoteValues = new Map();

const price = value => value == null ? '—' : `$${Number(value).toFixed(3)}`;
const safe = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const startTime = value => value ? new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(value)) : 'Time unavailable';

function initials(name) {
  return String(name || '?').split(/\s+/).slice(0, 2).map(word => word[0]).join('').toUpperCase();
}

function teamMarkup(team) {
  const name = team.name || 'Unknown team';
  const subtitle = team.record || '';
  const image = team.logo ? `<img src="${safe(team.logo)}" alt="" loading="lazy" onerror="this.hidden=true;this.nextElementSibling.hidden=false">` : '';
  return `<div class="team"><span class="team-logo">${image}<b${team.logo ? ' hidden' : ''}>${safe(initials(name))}</b></span><span>${safe(name)}<small>${safe(subtitle)}</small></span></div>`;
}

const sportIconPaths = {
  baseball: '<circle cx="12" cy="12" r="9"/><path d="M6 5c3 3 3 11 0 14M18 5c-3 3-3 11 0 14"/>',
  basketball: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c4 4 4 14 0 18M5 5c4 3 10 3 14 0"/>',
  football: '<path d="M5 18c-3-3 0-9 5-12s10-2 11 1c1 4-3 9-8 11s-7 2-8 0Z"/><path d="m9 8 7 7M12 10l-2 2m5 1-2 2m1-5 2-2"/>',
  hockey: '<ellipse cx="10" cy="8" rx="6" ry="3"/><path d="M4 8v5c0 2 12 2 12 0V8M16 15h3l2 4h-8l-2-4 3-7"/>',
  mma: '<path d="M7 5h4v5H7zM13 14h4v5h-4zM11 7l2 2m-4 2 2 2m2-2 2-2"/><path d="M5 11h3v3H5zM16 8h3v3h-3z"/>',
  esports: '<path d="M7 9h10a3 3 0 0 1 3 3v3a3 3 0 0 1-3 3h-2l-2-2h-2l-2 2H7a3 3 0 0 1-3-3v-3a3 3 0 0 1 3-3Z"/><path d="M8 13h3m-1.5-1.5v3M16 13h.01M18 12h.01"/>',
  soccer: '<circle cx="12" cy="12" r="9"/><path d="m12 7 4 3-1 5h-6l-1-5 4-3Zm0 0V3m4 3 4 2m-5 7 3 4m-9-4-3 4m-1-9-4-2"/>',
  tennis: '<circle cx="12" cy="12" r="9"/><path d="M5 5c7 1 12 6 14 14M19 5C12 6 7 11 5 19"/>',
};

function homeSportIcon(sport) {
  const label = String(sport || 'Sport');
  const paths = sportIconPaths[label] || '<circle cx="12" cy="12" r="8"/><path d="M12 8v5m0 3h.01"/>';
  return `<span class="home-sport-icon" role="img" aria-label="${safe(label)}" title="${safe(label)}"><svg viewBox="0 0 24 24" aria-hidden="true">${paths}</svg></span>`;
}

function teamsForRecord(record) {
  if (record.teams?.length) return record.teams;
  // Older cached reports predate the team metadata. Preserve the same two-row
  // layout while using initials until the user refreshes the data for logos.
  return String(record.kalshi_title || '').split(/\s+vs\.?\s+/i)
    .filter(Boolean).map(name => ({ name }));
}

const mlbTeamAliases = {
  arizona: 'diamondbacks', atlanta: 'braves', baltimore: 'orioles', boston: 'redsox',
  chicagoc: 'cubs', chicagows: 'whitesox', cincinnati: 'reds', cleveland: 'guardians',
  colorado: 'rockies', detroit: 'tigers', houston: 'astros', kansascity: 'royals',
  laangels: 'angels', losangelesa: 'angels', ladodgers: 'dodgers', losangelesd: 'dodgers',
  miami: 'marlins', milwaukee: 'brewers',
  minnesota: 'twins', newyorkm: 'mets', newyorky: 'yankees', oakland: 'athletics',
  philadelphia: 'phillies', pittsburgh: 'pirates', sandiego: 'padres',
  sanfrancisco: 'giants', seattle: 'mariners', stlouis: 'cardinals',
  tampabay: 'rays', texas: 'rangers', toronto: 'bluejays', washington: 'nationals',
};

// The two venues occasionally use an abbreviation on one side and a city name
// on the other. Keep those labels in the same team slot.
const teamAliases = {
  nmstate: 'newmexicost',
  fcdallas: 'dallas',
  realsaltlake: 'saltlake',
  pitsteelers: 'pittsburgh',
  clebrowns: 'cleveland',
  indcolts: 'indianapolis',
  wascommanders: 'washington',
  aricardinals: 'arizona',
  nygiants: 'newyorkg',
  nepatriots: 'newengland',
  bufbills: 'buffalo',
  nyjets: 'newyorkj',
  chibears: 'chicago',
  dalcowboys: 'dallas',
  houtexans: 'houston',
  larams: 'losangelesr',
  phieagles: 'philadelphia',
  gbpackers: 'greenbay',
  tbbuccaneers: 'tampabay',
  tentitans: 'tennessee',
  balravens: 'baltimore',
  jacjaguars: 'jacksonville',
  cinbengals: 'cincinnati',
  miadolphins: 'miami',
  minvikings: 'minnesota',
  kcchiefs: 'kansascity',
  lvraiders: 'lasvegas',
  lachargers: 'losangelesc',
  seaseahawks: 'seattle',
  denbroncos: 'denver',
  sf49ers: 'sanfrancisco',
  detlions: 'detroit',
  carpanthers: 'carolina',
  atlfalcons: 'atlanta',
  nosaints: 'neworleans',
};

function normalizeTeam(value) {
  const normalized = String(value || '').toLowerCase().replace(/\bwins?\b/g, '').replace(/[^a-z0-9]/g, '');
  if (activeSport === 'baseball') return mlbTeamAliases[normalized] || normalized;
  return teamAliases[normalized] || normalized;
}

function orderedBookItems(record, items, labelField) {
  const unused = [...items];
  const ordered = teamsForRecord(record).map(team => {
    const name = normalizeTeam(team.name);
    const index = unused.findIndex(item => {
      const label = normalizeTeam(item[labelField]);
      // Kalshi's sports contracts often contain the complete question (for
      // example, "Will FURIA Esports win …"), while the team slot contains
      // only the competitor name. Match the embedded team name as well as
      // the shorter labels used by other venues.
      return label === name || label.includes(name) || name.includes(label)
        || label.startsWith(name) || name.startsWith(label);
    });
    return index >= 0 ? unused.splice(index, 1)[0] : null;
  });
  while (ordered.length < 2) ordered.push(unused.shift() || null);
  // Only soccer has a draw outcome. Every other sport on this dashboard is a
  // two-way winner market, so never let an unmatched label create a third tile.
  if (activeSport !== 'soccer') return ordered.slice(0, 2);
  const drawIndex = unused.findIndex(item => /^(draw|tie)/i.test(String(item[labelField])));
  if (drawIndex >= 0) {
    const draw = unused.splice(drawIndex, 1)[0];
    // Three-way sports markets always read: first team, draw, second team.
    return [ordered[0], draw, ordered[1]];
  }
  return ordered.slice(0, 2);
}

function rollingPrice(value, tone, key, title = '') {
  const numericValue = numericQuote(value);
  if (numericValue == null) return '<span class="price unavailable">—</span>';
  const tooltip = title ? ` title="${safe(title)}"` : '';
  return `<span class="price ${tone}" data-quote-key="${safe(key)}" data-quote-value="${numericValue}"${tooltip}>${price(numericValue)}</span>`;
}

function animateUpdatedQuotes() {
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  document.querySelectorAll('.price[data-quote-key]').forEach(element => {
    const key = element.dataset.quoteKey;
    const target = Number(element.dataset.quoteValue);
    const previous = renderedQuoteValues.get(key);
    renderedQuoteValues.set(key, target);
    if (reducedMotion || !Number.isFinite(previous) || previous === target) return;

    const startedAt = performance.now();
    const duration = 420;
    element.classList.add('price-rolling');
    const tick = now => {
      const progress = Math.min(1, (now - startedAt) / duration);
      const eased = 1 - ((1 - progress) ** 3);
      element.textContent = price(previous + ((target - previous) * eased));
      if (progress < 1) requestAnimationFrame(tick);
      else element.textContent = price(target);
    };
    requestAnimationFrame(tick);
  });
}

function pairForBook(record, items, labelField, priceField, venueUrl, venueName) {
  const ordered = orderedBookItems(record, items, labelField);
  const eventKey = record.kalshi_event_ticker || record.polymarket_us_event_slug || record.kalshi_title;
  const tiles = ordered.map((item, index) => {
    if (!item) return '<span class="outcome-tile"><span class="price unavailable">—</span></span>';
    const value = Number(item[priceField]);
    const tone = Number.isFinite(value) && value < 0.5 ? 'down' : 'up';
    const key = `${venueName}:${eventKey}:${item[labelField] || index}`;
    return `<span class="outcome-tile">${rollingPrice(item[priceField], tone, key, item[labelField])}</span>`;
  }).join('');
  if (!venueUrl) return `<div class="book-pair">${tiles}</div>`;
  return `<a class="book-link" href="${safe(venueUrl)}" target="_blank" rel="noopener noreferrer" aria-label="Open this event on ${safe(venueName)}"><span class="book-pair">${tiles}</span></a>`;
}

function numericQuote(value) {
  // A missing quote is not a zero-cost outcome. Treat null, undefined, and an
  // empty string as unavailable before doing any arbitrage arithmetic.
  if (value == null || value === '') return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function arbitrageCell(record) {
  const kalshi = orderedBookItems(record, record.kalshi_moneyline_asks, 'contract')
    .map(item => numericQuote(item?.yes_ask));
  const polymarket = orderedBookItems(record, record.polymarket_us_displayed_moneyline_quotes, 'outcome')
    .map(item => numericQuote(item?.displayed_quote));
  if (kalshi.length !== 2 || polymarket.length !== 2) return '<span class="no-arb">3-way review</span>';
  if ([...kalshi, ...polymarket].some(quote => quote == null)) {
    return '<span class="no-arb">Incomplete</span>';
  }
  const totals = [kalshi[0] + polymarket[1], kalshi[1] + polymarket[0]].filter(Number.isFinite);
  const bestTotal = Math.min(...totals);
  if (!Number.isFinite(bestTotal) || bestTotal >= 1) return '<span class="no-arb">—</span>';
  return `<div class="arb"><strong>+${((1 - bestTotal) * 100).toFixed(2)}%</strong></div>`;
}

function renderSport(data, append = false) {
  const pageRecords = data.records || [];
  latestRecords = append ? [...latestRecords, ...pageRecords] : pageRecords;
  nextSportOffset = data.next_offset;
  sportRecordTotal = data.total_records ?? latestRecords.length;
  const records = latestRecords;
  tableWrap.classList.remove('home-layout');
  const sportName = activeLabel || data.sport[0].toUpperCase() + data.sport.slice(1);
  document.querySelector('#sport-title').textContent = sportName;
  matchSummary.textContent = `${sportRecordTotal} matched ${sportName.toLowerCase()} games`;
  document.querySelector('#sidebar-updated').textContent = `Updated ${new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(new Date())}`;
  const sourceTotals = [
    ['Kalshi', data.kalshi_events_compared],
    ['Polymarket US', data.polymarket_events_compared],
  ].map(([venue, count]) => {
    const hasSourceTotal = count != null && Number.isFinite(Number(count));
    return { venue, count: hasSourceTotal ? count : sportRecordTotal, label: hasSourceTotal ? 'returned' : 'matched' };
  });
  sourceSummary.hidden = false;
  sourceSummary.innerHTML = sourceTotals.map(({ venue, count, label }) =>
    `<div><span>${safe(venue)}</span><b>${safe(count)} ${label}</b></div>`).join('');
  meta.textContent = '';
  notice.hidden = true;
  if (!records.length) { tableWrap.innerHTML = `<div class="empty"><h2>No matched ${safe(sportName.toLowerCase())} games</h2><p>There are no likely open cross-venue matches in the latest public feeds. Refresh to check again.</p></div>`; return; }
  const loadMore = nextSportOffset == null ? '' : `<div class="load-more"><button id="load-more" type="button">Load 25 more <span>Showing ${records.length} of ${sportRecordTotal}</span></button></div>`;
  tableWrap.innerHTML = `<table class="${activeSport === 'soccer' ? 'three-way-table' : ''}"><colgroup><col class="game-column"><col class="time-column"><col class="book-column"><col class="book-column"><col class="book-column"><col class="book-column"><col class="arbitrage-column"></colgroup><thead><tr><th>Game / Event</th><th>Start time</th><th><span class="book-heading"><b class="venue-icon kalshi">K</b>Kalshi</span></th><th><span class="book-heading"><b class="venue-icon polymarket">◇</b>Polymarket US</span></th><th><span class="book-heading"><b class="venue-icon novig">N</b>Novig</span></th><th><span class="book-heading"><b class="venue-icon prophetx">P</b>ProphetX</span></th><th>Arbitrage</th></tr></thead><tbody>${records.map((record, index) => `<tr class="game-row" data-game-index="${index}" tabindex="0" role="link" aria-label="Open ${safe(record.kalshi_title || 'game')} market breakdown">
    <td><div class="game-teams">${teamsForRecord(record).map(teamMarkup).join('')}</div></td>
    <td><span class="start">${safe(startTime(record.start_time))}</span></td>
    <td>${pairForBook(record, record.kalshi_moneyline_asks, 'contract', 'yes_ask', record.kalshi_url, 'Kalshi')}</td>
    <td>${pairForBook(record, record.polymarket_us_displayed_moneyline_quotes, 'outcome', 'displayed_quote', record.polymarket_us_url, 'Polymarket US')}</td>
    <td>${pairForBook(record, record.novig_moneyline_quotes || [], 'outcome', 'displayed_ask', null, 'Novig')}</td>
    <td class="unavailable-book" aria-label="ProphetX data not connected"></td>
    <td>${arbitrageCell(record)}</td>
  </tr>`).join('')}</tbody></table>${loadMore}`;
  animateUpdatedQuotes();
}

function walletCard(wallet) {
  const icons = { Kalshi: ['kalshi', 'K'], 'Polymarket US': ['polymarket', '◇'], Novig: ['novig', 'N'], ProphetX: ['prophetx', 'P'] };
  const [iconClass, icon] = icons[wallet.venue] || ['polymarket', '◇'];
  if (wallet.placeholder) {
    return `<article class="wallet-card wallet-placeholder"><div class="wallet-heading"><b class="venue-icon ${iconClass}">${icon}</b><span>${safe(wallet.venue)}</span><em>Placeholder</em></div><strong>—</strong><span>Wallet integration coming soon</span></article>`;
  }
  const balance = wallet.balance == null ? 'Unavailable' : `$${Number(wallet.balance).toFixed(2)}`;
  const portfolio = wallet.portfolio_value == null ? '' : `<small>Portfolio value ${safe(`$${Number(wallet.portfolio_value).toFixed(2)}`)}</small>`;
  const state = wallet.connected ? 'Connected' : 'Reconnect required';
  return `<article class="wallet-card"><div class="wallet-heading"><b class="venue-icon ${iconClass}">${icon}</b><span>${safe(wallet.venue)}</span><em class="${wallet.connected ? 'connected' : ''}">${state}</em></div><strong>${balance}</strong><span>Wallet balance</span>${portfolio}</article>`;
}

function renderHome(data, opportunityData = {}) {
  const wallets = data.wallets || [];
  const opportunities = opportunityData.opportunities || [];
  const sportCounts = opportunityData.sport_counts || {};
  latestRecords = opportunities;
  nextSportOffset = null;
  sportRecordTotal = 0;
  tableWrap.classList.add('home-layout');
  document.querySelector('#sport-title').textContent = 'Dashboard';
  matchSummary.textContent = '';
  sourceSummary.hidden = true;
  sourceSummary.textContent = '';
  document.querySelector('#sidebar-updated').textContent = `Updated ${new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(new Date(data.updated_at || Date.now()))}`;
  meta.textContent = 'Connected wallet balances · auto-refreshes every 30s';
  notice.hidden = true;
  const quotePair = (row, quotes, url, venue) => {
    const eventKey = row.kalshi_ticker || row.polymarket_us_slug || row.title;
    const tone = value => Number(value) < .5 ? 'down' : 'up';
    const values = quotes?.length ? quotes : [null, null];
    const pair = `<span class="book-pair">${values.map((value, index) =>
      rollingPrice(value, tone(value), `${venue}:${eventKey}:${index}`)).join('')}</span>`;
    return url ? `<a class="book-link" href="${safe(url)}" target="_blank" rel="noopener noreferrer" aria-label="Open this event on ${safe(venue)}">${pair}</a>` : pair;
  };
  const opportunitiesTable = opportunities.length ? opportunities.map((row, index) => `<tr class="game-row" data-game-index="${index}" tabindex="0" role="link" aria-label="Open ${safe(row.title || 'game')} market breakdown"><td class="sport-cell">${homeSportIcon(row.sport)}</td><td>${row.teams?.length ? `<div class="game-teams">${row.teams.map(teamMarkup).join('')}</div>` : `<span class="game">${safe(row.title)}</span>`}</td><td><span class="start">${safe(startTime(row.start_time))}</span></td><td>${quotePair(row, row.kalshi, row.kalshi_url, 'Kalshi')}</td><td>${quotePair(row, row.polymarket_us, row.polymarket_us_url, 'Polymarket US')}</td><td>${quotePair(row, row.novig, null, 'Novig')}</td><td class="unavailable-book"></td><td>${row.edge ? `<div class="arb"><strong>+${(row.edge * 100).toFixed(2)}%</strong></div>` : '<span class="no-arb">—</span>'}</td></tr>`).join('') : '<tr><td colspan="8" class="detail-empty">No current opportunities in cached reports. Select a sport and refresh its data.</td></tr>';
  const total = Object.values(sportCounts).reduce((sum, count) => sum + count, 0);
  const sportList = Object.entries(sportCounts).filter(([, count]) => count).sort((a, b) => b[1] - a[1]).map(([name, count]) => `<li><span>${safe(name)}</span><b>${count}</b></li>`).join('') || '<li><span>No report data yet</span></li>';
  tableWrap.innerHTML = `<section class="wallet-grid">${wallets.map(walletCard).join('')}</section><h2 class="home-section-title">Top Opportunities</h2><section class="opportunities"><div class="opportunity-table"><table><thead><tr><th class="sport-column"><span class="sr-only">Sport</span></th><th>Game / Event</th><th>Start time</th><th><span class="book-heading"><b class="venue-icon kalshi">K</b>Kalshi</span></th><th><span class="book-heading"><b class="venue-icon polymarket">◇</b>Polymarket US</span></th><th><span class="book-heading"><b class="venue-icon novig">N</b>Novig</span></th><th><span class="book-heading"><b class="venue-icon prophetx">P</b>ProphetX</span></th><th>Arbitrage</th></tr></thead><tbody>${opportunitiesTable}</tbody></table></div></section><section class="sport-summary"><div class="summary-ring"><strong>${total}</strong><span>Games</span></div><div><h2>Opportunities by sport</h2><ul>${sportList}</ul></div></section>`;
  animateUpdatedQuotes();
}

async function loadHome() {
  if (activeSport || walletRefreshInFlight) return;
  walletRefreshInFlight = true;
  refresh.disabled = true;
  refresh.textContent = 'Refreshing…';
  try {
    const [walletResponse, opportunitiesResponse] = await Promise.all([fetch('/api/account-summary'), fetch('/api/opportunities')]);
    const [data, opportunityData] = await Promise.all([walletResponse.json(), opportunitiesResponse.json()]);
    if (!walletResponse.ok || !opportunitiesResponse.ok) throw new Error('Unable to load dashboard data');
    if (!activeSport) renderHome(data, opportunityData);
  } catch (error) {
    if (!activeSport) {
      meta.textContent = 'Wallet balances unavailable.';
      notice.textContent = 'Check your local account credentials and refresh.';
      tableWrap.innerHTML = `<section class="home-empty"><h2>Wallet balances unavailable</h2><p>${safe(error.message)}</p></section>`;
    }
  } finally {
    walletRefreshInFlight = false;
    if (!activeSport) {
      refresh.disabled = false;
      refresh.textContent = 'Refresh';
    }
  }
}

async function loadSport(refreshData = false, append = false) {
  const sport = activeSport;
  const leagues = [...activeLeagues];
  const offset = append ? nextSportOffset : 0;
  const selectionKey = `${sport}:${leagues.join(',')}`;
  if (!sport || (append && offset == null) || refreshingSports.has(selectionKey)) return;
  refreshingSports.add(selectionKey);
  refresh.disabled = true;
  refresh.textContent = refreshData ? 'Refreshing…' : 'Refresh data';
  try {
    const query = new URLSearchParams({ offset: String(offset), limit: String(sportPageSize) });
    if (leagues.length) query.set('leagues', leagues.join(','));
    const endpoint = `/api/sports/${sport}${refreshData ? '/refresh' : ''}?${query}`;
    const response = await fetch(endpoint, { method: refreshData ? 'POST' : 'GET' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Unable to load ${sport} data`);
    // Do not overwrite a newly selected tab with a slower previous request.
    if (activeSport === sport && activeLeagues.join(',') === leagues.join(',')) renderSport(data, append);
  } catch (error) {
    if (activeSport === sport && activeLeagues.join(',') === leagues.join(',')) {
      meta.textContent = `Could not load ${sport} data.`;
      tableWrap.innerHTML = `<div class="empty"><h2>Data unavailable</h2><p>${safe(error.message)}</p></div>`;
    }
  } finally {
    refreshingSports.delete(selectionKey);
    if (activeSport === sport && activeLeagues.join(',') === leagues.join(',')) {
      refresh.disabled = false;
      refresh.textContent = 'Refresh data';
    }
  }
}

document.querySelectorAll('.sport-group-toggle').forEach(toggle => toggle.addEventListener('click', () => {
  const options = document.querySelector(`#${toggle.getAttribute('aria-controls')}`);
  const willExpand = toggle.getAttribute('aria-expanded') !== 'true';
  document.querySelectorAll('.sport-group-toggle').forEach(other => {
    const otherOptions = document.querySelector(`#${other.getAttribute('aria-controls')}`);
    other.setAttribute('aria-expanded', 'false');
    otherOptions.hidden = true;
  });
  toggle.setAttribute('aria-expanded', String(willExpand));
  options.hidden = !willExpand;
}));

tabs.forEach(tab => tab.addEventListener('click', () => {
  activeSport = tab.dataset.sport;
  activeLeagues = tab.dataset.leagues.split(',');
  activeLabel = tab.dataset.label;
  tabs.forEach(item => item.classList.toggle('active', item === tab));
  document.querySelectorAll('.sport-group').forEach(group => group.classList.toggle('active', group.contains(tab)));
  home.classList.remove('active');
  refresh.hidden = false;
  loadSport();
}));
home.addEventListener('click', event => {
  event.preventDefault();
  activeSport = null;
  activeLeagues = [];
  activeLabel = '';
  home.classList.add('active');
  tabs.forEach(item => item.classList.remove('active'));
  document.querySelectorAll('.sport-group').forEach(group => group.classList.remove('active'));
  loadHome();
});
refresh.addEventListener('click', () => activeSport ? loadSport(true) : loadHome());
function openGameDetail(record) {
  const sport = activeSport || record?.sport;
  const params = new URLSearchParams({
    sport,
    kalshi: record.kalshi_event_ticker || record.kalshi_ticker,
    polymarket: record.polymarket_us_event_slug || record.polymarket_slug,
  });
  if (activeLeagues.length) params.set('leagues', activeLeagues.join(','));
  window.location.assign(`/game.html?${params}`);
}
tableWrap.addEventListener('click', event => {
  if (event.target.closest('#load-more')) {
    loadSport(false, true);
    return;
  }
  const row = event.target.closest('.game-row');
  if (!row || event.target.closest('a')) return;
  const index = Number(row.dataset.gameIndex);
  openGameDetail(latestRecords[index]);
});
tableWrap.addEventListener('keydown', event => {
  if ((event.key === 'Enter' || event.key === ' ') && event.target.matches('.game-row')) {
    event.preventDefault();
    const index = Number(event.target.dataset.gameIndex);
    openGameDetail(latestRecords[index]);
  }
});
document.querySelector('#search').addEventListener('input', event => {
  const query = event.target.value.trim().toLowerCase();
  document.querySelectorAll('#table-wrap tbody tr').forEach((row, index) => {
    const record = latestRecords[index];
    row.hidden = Boolean(query) && !JSON.stringify(record).toLowerCase().includes(query);
  });
});
setInterval(() => { document.querySelector('#clock').textContent = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' }).format(new Date()); }, 1000);
setInterval(() => {
  if (activeSport && !document.hidden) loadSport(true);
}, oddsRefreshIntervalMs);
setInterval(() => {
  if (!activeSport && !document.hidden) loadHome();
}, walletRefreshIntervalMs);
document.addEventListener('visibilitychange', () => {
  if (activeSport && !document.hidden) loadSport(true);
  if (!activeSport && !document.hidden) loadHome();
});
loadHome();

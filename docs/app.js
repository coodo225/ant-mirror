/* ══════════════════════════════════════════════════════════
   개인 · 외국인 · 기관 — 한국 증시 수급 대시보드
   의존성 없음. docs/data/*.json 만 읽는다.
   ══════════════════════════════════════════════════════════ */

const ACTORS = {
  individual:  { name: '개인',   sub: '개미',   face: '🐜', color: 'var(--individual)', raw: '#ffb02e' },
  foreign:     { name: '외국인', sub: '외인',   face: '🦅', color: 'var(--foreign)',    raw: '#4fc3f7' },
  institution: { name: '기관',   sub: '기관계', face: '🐋', color: 'var(--institution)', raw: '#b388ff' },
  other_corp:  { name: '기타법인', sub: '일반 법인', face: '🏢', color: '#9aa5bd', raw: '#9aa5bd' },
};
const ACTOR_KEYS = ['individual', 'foreign', 'institution'];   // 성적표·장바구니 등 통계는 세 주체
const FLOW_KEYS = [...ACTOR_KEYS, 'other_corp'];             // 그날 수급은 기타법인까지(넷을 더하면 0)

const INST_PARTS = [
  ['inst_fin_inv',   '금융투자'],
  ['inst_trust',     '투신'],
  ['inst_pension',   '연기금'],
  ['inst_insurance', '보험'],
  ['inst_bank',      '은행'],
  ['inst_other_fin', '기타금융'],
];

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
/** 0 → 목표값 CSS 트랜지션을 걸어주는 지연 실행.
 *  requestAnimationFrame 은 탭이 백그라운드면 멈추므로 타이머만 쓴다. */
const growIn = (fn, delay) => setTimeout(fn, delay);

const D = {};                 // 로드된 데이터
let heroMarket = 'KOSPI';
let flowMode   = 'cum';
let flowRange  = 40;
let treeMarket = 'KOSPI';
let reportMarket = 'KOSPI';
let selectedStock = null;
let programMarket = 'KOSPI';
let shortMarket = 'KOSPI';
let rankActor  = 'foreign';
let rankMarket = 'KOSPI';
let rankPeriod = 'day';
let rankOpen   = false;

/* ── 포맷 ─────────────────────────────────────────────── */

/** 억원 단위 값을 사람이 읽는 문자열로. 1조 = 10,000억 */
function eok(v, { sign = true } = {}) {
  if (v == null || isNaN(v)) return '—';
  const s = v < 0 ? '-' : (sign ? '+' : '');
  const a = Math.abs(v);
  if (a >= 10000) return `${s}${(a / 10000).toFixed(2)}조`;
  if (a >= 1)     return `${s}${Math.round(a).toLocaleString()}억`;
  return `${s}${a.toFixed(0)}억`;
}
const pct  = v => v == null || isNaN(v) ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`;
const nfmt = (v, d = 2) => v == null || isNaN(v) ? '—'
  : v.toLocaleString('ko-KR', { minimumFractionDigits: d, maximumFractionDigits: d });
const dirCls = v => v == null ? 'flat' : v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
const mdy = iso => iso ? `${+iso.slice(5, 7)}/${+iso.slice(8, 10)}` : '';
/** 외부 소스(종목명·업종명·일정 등)의 문자열을 HTML 에 넣기 전에 */
const esc = v => String(v ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sgn = (v, d = 2) => v == null || isNaN(v) ? '—' : `${v >= 0 ? '+' : ''}${(+v).toFixed(d)}`;
/** 상·하위 비율. 0.5 미만이 '0%' 로 보이지 않게 */
const rankTxt = p => p < 1 ? '1% 미만' : `${p.toFixed(0)}%`;
/** 블록 부트스트랩 90% 범위 */
const ciTxt = ci => ci ? `90% 범위 ${sgn(ci[0])} ~ ${sgn(ci[1])}%p` : '';

/** KST 오늘(YYYY-MM-DD) */
const todayKST = () => new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);
/** '10/2(금)' */
const dayLabel = iso => {
  if (!iso) return '';
  const d = new Date(`${iso}T00:00:00Z`);
  return `${d.getUTCMonth() + 1}/${d.getUTCDate()}(${'일월화수목금토'[d.getUTCDay()]})`;
};
/** 데이터 날짜가 오늘이면 '오늘', 아니면 '10/2(금)' — 휴장일·다음 날 아침에 지난 거래일을 '오늘'이라 부르지 않게 */
const whenWord = iso => (!iso || iso === todayKST()) ? '오늘' : dayLabel(iso);
/** 그 날짜의 지수 등락률(%) — 지수 일봉에서 */
function dayChange(market, iso) {
  const h = D.market?.indices?.[market]?.history || [];
  const i = h.findIndex(x => x.date === iso);
  return i > 0 && h[i - 1].close ? (h[i].close / h[i - 1].close - 1) * 100 : null;
}
/** 정수 부호 + 천 단위 쉼표 */
const sgnInt = v => v == null || isNaN(v) ? '—' : `${v >= 0 ? '+' : ''}${Math.round(v).toLocaleString()}`;
/** 수급이 잠정인 시간대(장중·동시호가·마감 뒤 20시 전) */
const isProvisionalPhase = ph => /장중|동시호가|확정 반영중/.test(ph || '');
/** 부호를 진짜 빼기 기호(−)로 — 한 줄 요약처럼 크게 읽히는 자리에서 */
const pctM = (v, d = 2) => v == null || isNaN(v) ? '—'
  : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%`;
/** ISO(+09:00) 문자열에서 'HH:MM' */
const hhmm = iso => (iso && iso.length >= 16) ? iso.slice(11, 16) : '';
/** ' (23분 전)' — 시각 문자열에서 지금까지 */
function agoTxt(iso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return '';
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  return mins < 1 ? '방금' : mins < 60 ? `${mins}분 전`
       : mins < 1440 ? `${Math.floor(mins / 60)}시간 전` : `${Math.floor(mins / 1440)}일 전`;
}
/** 'YYYY-MM-DD' 에 n일을 더한 날짜 */
const addDays = (iso, n) => new Date(Date.parse(`${iso}T00:00:00Z`) + n * 864e5).toISOString().slice(0, 10);
/** 오늘(KST)에서 며칠 남았나 */
const ddayOf = iso => Math.round((Date.parse(iso) - Date.parse(todayKST())) / 864e5);
/** 수급 보드의 모양: 장중(live) · 마감 뒤 확정 대기(pending) · 마감 확정(final) · 장 전/휴장(pre) */
function boardMode() {
  const ph = D.meta?.phase || '';
  if (/장중|동시호가/.test(ph)) return 'live';
  if (/확정 반영중/.test(ph)) return 'pending';
  if (/장마감/.test(ph)) return 'final';
  return 'pre';
}
const mkName = m => m === 'KOSDAQ' ? '코스닥' : '코스피';
/** 비율(%)을 '12.2%' / '1% 미만' 으로 */
const pctTxt = p => p == null || isNaN(p) ? '—' : p < 1 ? '1% 미만' : `${(+p).toFixed(p < 10 ? 1 : 0)}%`;

/** 용어 풀이 버튼. 누르면 작은 설명 상자가 뜬다(wireTerms) */
const term = (key, label) =>
  `<button type="button" class="term" data-term="${key}" aria-expanded="false">${esc(label)}<span class="term-i" aria-hidden="true">ⓘ</span></button>`;

/* ── 브라우저 저장소 — 관심 종목과 접힘 상태만. 막혀 있어도(사생활 보호 모드 등) 화면은 그대로 돈다 ── */
const store = {
  ok: (() => {
    try { localStorage.setItem('__ant_t', '1'); localStorage.removeItem('__ant_t'); return true; }
    catch { return false; }
  })(),
  get(k, dflt) {
    try { const v = localStorage.getItem(k); return v == null ? dflt : JSON.parse(v); }
    catch { return dflt; }
  },
  set(k, v) {
    try { localStorage.setItem(k, JSON.stringify(v)); return true; }
    catch { return false; }
  },
};

/** 종목 수급은 '주' 단위 */
function shares(v) {
  if (v == null || isNaN(v)) return '—';
  const s = v < 0 ? '-' : '+';
  const a = Math.abs(v);
  if (a >= 10000) return `${s}${(a / 10000).toFixed(1)}만주`;
  return `${s}${Math.round(a).toLocaleString()}주`;
}

/* ── 부트 ─────────────────────────────────────────────── */

const FILES = ['meta', 'flows', 'ant', 'analog', 'antstocks', 'futures', 'credit',
               'market', 'stocks', 'global', 'events', 'insights', 'program', 'short',
               'ranks', 'today', 'intraday_hist'];

/** 응답이 없으면 무한정 기다리지 않는다. 실패하면 왜 실패했는지 남긴다.
 *  path 를 주면 data/ 밖의 정적 파일(evidence.json)을 읽는다. */
async function fetchJSON(name, { timeout = 8000, tries = 2, path = null } = {}) {
  const url = path || `data/${name}.json`;
  let last = '알 수 없는 오류';
  for (let i = 0; i < tries; i++) {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeout);
    try {
      const r = await fetch(`${url}?t=${Date.now()}`,
                            { cache: 'no-store', signal: ac.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) {
      last = e.name === 'AbortError' ? `${timeout / 1000}초 안에 응답 없음` : e.message;
      if (/HTTP 404/.test(last)) break;          // 없는 파일은 다시 물어도 없다
    } finally {
      clearTimeout(timer);
    }
  }
  throw new Error(`${url} — ${last}`);
}

function showFatal(err) {
  $('#loading').hidden = true;
  $('#fatal').hidden = false;
  $('#fatal-msg').textContent = err.message;

  const isFile = location.protocol === 'file:';
  const missing = /HTTP 404/.test(err.message);
  let hint;
  if (isFile) {
    hint = `<b>파일을 직접 연 것 같습니다.</b> 브라우저는 <code>file://</code> 에서 다른 파일을
            읽는 것을 막습니다. 정적 서버로 열어야 합니다.<br>
            <code>python -m http.server 8765 --directory docs</code><br>
            그 다음 <code>http://localhost:8765</code> 로 접속하세요.`;
  } else if (missing) {
    hint = `<b>데이터 파일이 아직 없습니다.</b> 수집기를 한 번 돌려야
            <code>docs/data/*.json</code> 이 생깁니다.<br>
            <code>python collector/collect.py</code>`;
  } else {
    hint = `<b>서버 응답을 받지 못했습니다.</b> 정적 서버가 살아 있는지 확인한 뒤 다시 시도하세요.<br>
            <code>python -m http.server 8765 --directory docs</code>`;
  }
  $('#fatal-hint').innerHTML = hint;
  $('#fatal-retry').onclick = () => {
    $('#fatal').hidden = true;
    $('#loading').hidden = false;
    $('#loading-msg').textContent = '다시 불러오는 중…';
    $('#loading-detail').textContent = '';
    boot();
  };
}

/** 한 섹션이 터져도 나머지 화면은 살린다 */
function safe(fn, label) {
  try {
    fn();
  } catch (e) {
    console.error(`[${label}] 렌더 실패`, e);
    RENDER_ERRORS.push(`${label}: ${e.message}`);
  }
}
const RENDER_ERRORS = [];

/** 파일을 못 받았을 때 대신 쓸 빈 모양 — 섹션은 '데이터 없음'으로 그려진다 */
const EMPTY = {
  flows: { markets: {} }, ant: {}, analog: {}, antstocks: {}, futures: { daily: [] },
  credit: { loans: [], money: [], latest: {} }, market: { indices: {} },
  stocks: { top: [], industries: [] }, global: { items: [], macro: [] },
  events: { upcoming: [] }, insights: { items: [] }, program: { markets: {} }, short: { markets: {} },
  ranks: { markets: {} }, today: { items: [] }, intraday_hist: { days: [], count: 0 },
};
const LOAD_ERRORS = [];

/** 새로 생긴 섹션 파일 — 새 수집기가 처음 돌기 전(배포 직후)엔 없을 수 있어 404 는 '없음'으로만 본다 */
const OPTIONAL = new Set(['program', 'short', 'ranks', 'today', 'intraday_hist']);

/** 속설 검증표(docs/evidence.json)는 수집과 상관없는 정적 파일 — 한 번만 받고, 없으면 그 칸만 비운다 */
async function loadEvidence() {
  if (D.evidence) return;
  try { D.evidence = await fetchJSON('evidence', { path: 'evidence.json', tries: 1 }); }
  catch (e) { console.warn('검증표를 받지 못함:', e.message); D.evidence = null; }
}

/** 파일마다 따로 받는다. 실패한 파일은 keep(이전 값) 또는 빈 모양으로 채우고 이유를 남긴다. */
async function loadFiles(files, opts, keep = false) {
  const res = await Promise.allSettled(files.map(f => fetchJSON(f, opts)));
  const errs = [];
  res.forEach((r, i) => {
    const f = files[i];
    if (r.status === 'fulfilled') D[f] = r.value;
    else {
      if (!(OPTIONAL.has(f) && /HTTP 404/.test(r.reason?.message || ''))) errs.push(r.reason?.message || `${f} 실패`);
      if (!keep || D[f] === undefined) D[f] = structuredClone(EMPTY[f] ?? {});
    }
  });
  return errs;
}

async function boot() {
  RENDER_ERRORS.length = 0;
  const slow = setTimeout(() => {
    $('#loading-detail').textContent = '예상보다 오래 걸립니다. 서버 응답을 기다리는 중…';
  }, 6000);

  try {
    // meta 가 없으면 서버나 경로 문제다 — 그때만 오류 화면
    D.meta = await fetchJSON('meta');
    LOAD_ERRORS.length = 0;
    const [errs] = await Promise.all([loadFiles(FILES.filter(f => f !== 'meta')), loadEvidence()]);
    LOAD_ERRORS.push(...errs);
  } catch (e) {
    showFatal(e);
    return;
  } finally {
    clearTimeout(slow);
  }

  $('#loading').hidden = true;
  $('#app').hidden = false;

  renderAll();
  safe(wireControls, '컨트롤');
  safe(initDetails, '접힘 상태');
  reportRenderErrors();
  LAST_W = document.documentElement.clientWidth;

  // 수집은 장중에도 30분 간격(그마저 GitHub 사정으로 늦어진다)이라 1분마다 파일 12개를 받을 이유가 없다.
  // 5분마다 meta 하나만 확인해 새 수집이 있을 때만 전부 다시 받는다. 탭이 안 보이면 쉰다.
  // 장 상태와 상관없이 돌려서, 장 전에 열어 둔 페이지도 장중 수집을 받는다.
  if (!POLL) POLL = setInterval(() => { if (!document.hidden) refresh(); }, 5 * 60 * 1000);
}
let POLL = null;

/** 데이터가 바뀌면 다시 그릴 섹션 전부 (컨트롤 연결은 한 번만) */
const RENDERERS = [
  [renderMeta, '헤더'], [renderPulse, '지수 한 줄·일정'], [renderHero, '수급 보드'], [renderIntraday, '장중 흐름'],
  [renderUnusual, '평소와 다른 것'], [renderInsights, '그 밖의 사실'], [renderWatch, '관심 종목'],
  [renderRanks, '종목 순위'],
  [renderThermo, '개미 온도계'], [renderContrarian, '온도계 과거 기록'], [renderFutures, '선물'], [renderCredit, '빚투 체온계'],
  [renderReportCard, '성적표'], [renderOppose, '반대로 가는 본능'], [renderHall, '흑역사'],
  [renderCaveats, '한계 고백'], [renderEvidence, '속설 검증표'],
  [renderAnalog, '유사 국면'], [renderBaskets, '장바구니 비교'], [renderFlowChart, '수급 차트'],
  [renderStreaks, '연속 매매'], [renderInstBreakdown, '기관 분해'],
  [renderProgram, '프로그램 매매'], [renderShort, '공매도'], [renderTreemap, '트리맵'],
  [renderIndustries, '업종'], [renderGlobals, '글로벌'], [renderEvents, '이벤트'],
  [renderIntraHist, '장중 기록'], [renderJump, '바로가기'],
];
function renderAll() { RENDERERS.forEach(([fn, label]) => safe(fn, label)); syncPressed(); scrollChartsToLatest(); }

/* ── 반응형 차트 ─────────────────────────────────────────
   viewBox 폭을 실제 상자 폭(CSS px)과 같게 잡아, 글자가 축소되지 않고 적힌 크기(10px 이상) 그대로 보이게 한다.
   화면 폭이 바뀌면(회전 포함) 차트만 다시 그린다. */

/** 차트 viewBox 폭. 접힌 <details> 안이라 폭이 0이면 카드 폭으로 어림하고, 펼칠 때 다시 그린다 */
function chartW(svg) {
  let w = svg?.parentElement?.clientWidth || 0;
  if (!w) {
    const c = svg?.closest('.card');
    w = c ? c.clientWidth - 36 : 0;
  }
  if (!(w > 0)) w = 640;
  return Math.round(Math.min(900, Math.max(300, w)));
}
/** 가로축 라벨 간격: 라벨 하나에 px 만큼 자리가 있게 */
const labelStep = (n, pw, px = 52) => Math.max(1, Math.ceil(n / Math.max(2, Math.floor(pw / px))));

/** 폭이 달라졌을 때 다시 그릴 차트들 */
function rerenderCharts() {
  [[renderIntraday, '장중 흐름'], [renderFutures, '선물'], [renderCredit, '빚투 체온계'],
   [renderTimingChart, '성적표 차트'], [renderFlowChart, '수급 차트'],
   [renderProgram, '프로그램 매매'], [renderShort, '공매도']].forEach(([fn, label]) => safe(fn, label));
  if (selectedStock && typeof STOCK_CHART === 'function') safe(STOCK_CHART, '종목 차트');
}
let LAST_W = 0;
function onResize() {
  clearTimeout(onResize._t);
  onResize._t = setTimeout(() => {
    const w = document.documentElement.clientWidth;
    if (!w || w === LAST_W || $('#app').hidden) return;     // 세로 길이만 바뀐 건(주소창 접힘 등) 무시
    LAST_W = w;
    rerenderCharts();
  }, 200);
}
window.addEventListener('resize', onResize);
window.addEventListener('orientationchange', onResize);

/** 차트가 화면보다 넓으면 최신(오른쪽) 구간부터 보이게 — 반응형이 된 뒤로는 안전장치일 뿐 */
function scrollChartsToLatest() {
  requestAnimationFrame(() => $$('.chart-wrap').forEach(w => {
    if (w.scrollWidth > w.clientWidth + 2) w.scrollLeft = w.scrollWidth;
  }));
}

/** 토글 버튼의 눌림 상태(.on)를 aria-pressed 로 */
function syncPressed() {
  $$('.seg button').forEach(b => b.setAttribute('aria-pressed', b.classList.contains('on') ? 'true' : 'false'));
}
document.addEventListener('click', e => {
  if (e.target.closest('.seg button')) setTimeout(() => { syncPressed(); scrollChartsToLatest(); });
});

/** 차트 툴팁: 터치에서는 손을 떼도 남겨 두고, 차트 바깥을 누르면 숨긴다 */
function hideChartTips() {
  $('#tooltip').hidden = true;
  ['#intra-cursor', '#flow-cursor'].forEach(sel => $(sel)?.setAttribute('visibility', 'hidden'));
}
document.addEventListener('pointerdown', ev => {
  if (ev.pointerType !== 'mouse' && !ev.target.closest('#intraday-chart, #flow-chart')) hideChartTips();
});

async function refresh() {
  try {
    const meta = await fetchJSON('meta', { timeout: 8000, tries: 1 });
    if (meta.generatedAt === D.meta.generatedAt) return;          // 새 수집이 없으면 아무것도 안 한다
    const others = FILES.filter(f => f !== 'meta');
    const errs = await loadFiles(others, { timeout: 8000, tries: 1 }, true);   // 실패한 파일은 이전 값 유지
    if (errs.length === others.length) throw new Error(errs[0]);
    LOAD_ERRORS.length = 0;
    LOAD_ERRORS.push(...errs);
    D.meta = meta;
    STOCKFLOWS = null;
    SERIES.clear();
  } catch (e) {
    console.warn('자동 갱신 실패, 다음 주기에 재시도:', e.message);
    return;                       // 이미 그려진 화면은 그대로 둔다
  }
  RENDER_ERRORS.length = 0;
  renderAll();
  if (selectedStock && !$('#stock-detail').hidden) safe(() => showStock(selectedStock, { scroll: false }), '종목 상세');
  reportRenderErrors();
}

/* ── 헤더 ─────────────────────────────────────────────── */

function renderMeta() {
  const m = D.meta;
  const badge = $('#phase-badge');
  badge.textContent = m.phase || '—';
  badge.classList.toggle('live', /장중|동시호가/.test(m.phase || ''));
  renderUpdated();
  // 섹션 제목의 '오늘'도 데이터 날짜에 맞춘다
  const dw = whenWord(m.dataDate || D.flows?.markets?.KOSPI?.latest?.date);
  setText('#hero-title', dw === '오늘' ? '오늘의 수급' : `${dw} 수급`);
  setText('#thermo-title', dw === '오늘' ? '오늘의 개미 온도계' : `${dw} 개미 온도계`);
  setText('#inst-when', dw);

  $('#sources').textContent = (m.sources || []).map(s => s.name).join(' · ');
  $('#caveat').textContent = m.caveat || '';

  // 경고에는 외부 응답 문구가 섞일 수 있으니 HTML 이 아니라 텍스트로 넣는다
  const w = $('#warnings');
  const lines = [...(m.warnings || [])];
  if (lines.length) {
    w.hidden = false;
    w.innerHTML = '<b>수집 경고</b>';
    lines.forEach(x => { const d = el('div'); d.textContent = `· ${x}`; w.append(d); });
  } else w.hidden = true;

  // 소스가 실패하거나 멈춰 갱신되지 않은 섹션 — 지난 숫자를 오늘 것처럼 보이지 않게 맨 위에 알린다
  const stale = m.stale || [];
  const st = $('#stale');
  if (st) {
    st.hidden = !stale.length;
    if (stale.length) {
      const items = stale.map(s =>
        s.label + (s.asOf ? ` (${s.asOf}까지)`
                 : s.since ? ` (${s.since.slice(0, 16).replace('T', ' ')} 수집분)` : ''));
      st.innerHTML = '<strong>지난 데이터</strong>';
      st.append(`갱신되지 않아 마지막으로 받은 데이터를 보여 주는 항목: ${items.join(' · ')}`);
    }
  }
}

/** '수집 10/5 14:13 (23분 전) · 데이터 10/2(금) 확정 · 다음 장 10/6(화)'. 장중에 수집이 45분 넘게 없으면 경고 */
function renderUpdated() {
  const m = D.meta, box = $('#updated');
  if (!m || !box) return;
  const gen = Date.parse(m.generatedAt);
  const mins = isNaN(gen) ? null : Math.max(0, Math.round((Date.now() - gen) / 60000));
  const ago = mins == null ? '' : mins < 1 ? ' (방금)' : mins < 60 ? ` (${mins}분 전)`
            : mins < 1440 ? ` (${Math.floor(mins / 60)}시간 전)` : ` (${Math.floor(mins / 1440)}일 전)`;
  const parts = [`수집 ${(m.generatedAtText || '').slice(5, 16)}${ago}`];
  if (m.dataDate) parts.push(`데이터 ${dayLabel(m.dataDate)} ${m.dataFinal ? '확정' : '잠정'}`);
  const live = /장중|동시호가/.test(m.phase || '');
  if (m.nextSession && !live) parts.push(`다음 장 ${m.nextSession === todayKST() ? '오늘' : dayLabel(m.nextSession)}`);
  const late = live && mins != null && mins > 45;
  box.textContent = parts.join(' · ') + (late ? ' · 새 수집이 늦어지고 있습니다' : '');
  box.classList.toggle('late', late);
  renderBoardStatus();
}
setInterval(() => { if (!document.hidden) renderUpdated(); }, 60 * 1000);

/** 못 받은 파일이나 렌더 중 터진 섹션이 있으면 조용히 넘어가지 않고 화면에 적는다 */
function reportRenderErrors() {
  const w = $('#warnings');
  [['데이터를 불러오지 못함', LOAD_ERRORS], ['화면 오류', RENDER_ERRORS]].forEach(([title, list]) => {
    if (!list.length) return;
    w.hidden = false;
    const b = el('b'); b.style.marginTop = '8px'; b.textContent = title; w.append(b);
    list.forEach(x => { const d = el('div'); d.textContent = `· ${x}`; w.append(d); });
  });
}

/* ── 지수 한 줄 + 사흘 안의 일정 칩 ───────────────────── */

function renderPulse() {
  const ix = D.market?.indices || {};
  const part = code => {
    const x = ix[code];
    if (!x || x.price == null) return '';
    const hi = x.high1y?.date && x.fromHigh != null
      ? ` <span class="idx-hi">· 1년 고점(${mdy(x.high1y.date)}) 대비 ${pctM(x.fromHigh, 1)}</span>` : '';
    return `<span class="idx-chunk"><b>${esc(x.name || mkName(code))}</b> <span class="mono">${nfmt(x.price)}</span> ` +
           `<b class="${dirCls(x.changeRate)}">${pctM(x.changeRate)}</b>${hi}</span>`;
  };
  const parts = ['KOSPI', 'KOSDAQ'].map(part).filter(Boolean);
  $('#idx-line').innerHTML = parts.length
    ? parts.join('<span class="idx-sep" aria-hidden="true"> · </span>')
    : '<span class="dim">지수 데이터를 받지 못했습니다.</span>';

  const chips = eventChips();
  $('#chips').innerHTML = chips.length
    ? chips.map(c => `<span class="chip${c.cls ? ' ' + c.cls : ''}"${c.title ? ` title="${esc(c.title)}"` : ''}>${esc(c.text)}</span>`).join('')
    : '<span class="chip-none">사흘 안에 잡힌 휴장·만기·금리 결정·국내 지표 일정 없음</span>';
}

/** events.upcoming 에서 D-0 ~ D-3 의 휴장·만기·금통위·FOMC·국내 지표만. 날짜만 적고 '주의'는 달지 않는다 */
const CHIP_TYPES = new Set(['휴장', '만기', '금통위', 'FOMC', '지표']);
function eventChips() {
  const ph = D.meta?.phase || '';
  const closed = /휴장일|주말/.test(ph);
  const out = [];
  if (closed && D.meta?.nextSession) {
    out.push({ text: `${ph === '주말' ? '주말' : '휴장'} · 다음 장 ${dayLabel(D.meta.nextSession)}`, cls: 'closed' });
  }
  const whenTxt = dd => dd === 0 ? '오늘' : dd === 1 ? '내일' : `D-${dd}`;
  (D.events?.upcoming || []).forEach(e => {
    if (!CHIP_TYPES.has(e.type) || !e.date) return;
    const dd = ddayOf(e.date);
    if (e.type === 'FOMC') {
      // 미국 날짜의 결정은 한국시간 다음 날 새벽에 나온다
      const kd = dd + 1;
      if (kd < 0 || kd > 3) return;
      out.push({ text: `FOMC 결과 ${kd <= 1 ? whenTxt(kd) : dayLabel(addDays(e.date, 1))} 새벽 3시경`, title: e.title });
      return;
    }
    if (dd < 0 || dd > 3) return;
    if (e.type === '휴장') {
      if (dd === 0 && closed) return;           // 위 '휴장 · 다음 장' 칩과 겹친다
      out.push({ text: `휴장 ${whenTxt(dd)} · ${dayLabel(e.date)}`, title: e.title });
      return;
    }
    const name = e.type === '금통위' ? '금통위' : (e.title || e.type);
    out.push({ text: `${name} ${whenTxt(dd)}`, title: e.note || e.title });
  });
  return out;
}

/* ── 수급 보드 (장 상태에 따라 모양이 바뀐다) ─────────── */

/** 보드 위 상태 한 줄: 잠정/확정과 수집 시각. renderUpdated 가 1분마다 다시 부른다 */
function renderBoardStatus() {
  const box = $('#board-status');
  if (!box || !D.meta) return;
  const m = D.meta, mode = boardMode();
  const date = D.flows?.markets?.[heroMarket]?.latest?.date || m.dataDate;
  const prov = '<span class="tag-prov">잠정</span>';
  const tm = term('prov', '잠정/확정');
  // 그날 지수 등락 — 제목에 날짜가 있으니 여기엔 시장과 등락만
  const chg = date ? dayChange(heroMarket, date) : null;
  const ix = D.market?.indices?.[heroMarket];
  const idxChg = chg != null ? chg : (mode === 'live' || mode === 'pending') && ix ? ix.changeRate : null;
  const idx = idxChg != null ? ` · ${mkName(heroMarket)} <b class="${dirCls(idxChg)}">${pctM(idxChg)}</b>` : '';
  let html;
  if (mode === 'live') {
    const at = (m.mode === 'intraday' && m.intradayAt) ? m.intradayAt : m.generatedAt;
    const ago = agoTxt(at);
    html = `${prov} ${hhmm(at) || '—'} 수집${ago ? `(${ago})` : ''}${idx} · 20시 무렵 확정 ${tm}`;
  } else if (mode === 'pending') {
    html = `${prov} 20시 확정 대기${idx} ${tm}`;
  } else if (mode === 'final') {
    html = m.dataFinal === false ? `${prov} 확정 전${idx} ${tm}` : `확정${idx} ${tm}`;
  } else {
    html = `지난 거래일 ${m.dataFinal === false ? '잠정' : '확정'}${idx} ${tm}`;
  }
  box.innerHTML = html;
}

function renderHero() {
  const mk = D.flows.markets[heroMarket];
  const tug = $('#tug');
  const mode = boardMode();
  $('#board').dataset.mode = mode;
  tug.classList.toggle('compact', mode === 'pre');
  renderBoardStatus();
  safe(() => renderPreopen(mode === 'pre'), '간밤 지표');
  if (!mk || !mk.latest) {
    tug.innerHTML = '<p class="dim">데이터가 없습니다.</p>';
    $('#board-program').hidden = true;
    return;
  }

  const last = mk.latest;
  // 제목은 데이터 날짜를 따른다(시장 탭을 바꿔도)
  const dw = whenWord(last.date);
  setText('#hero-title', dw === '오늘' ? '오늘의 수급' : `${dw} 수급`);

  // 프로그램 매매 한 줄 — 수급과 같은 날짜일 때만(장 전 화면은 칸이 모자라 뺀다)
  const pg = D.program?.markets?.[heroMarket]?.latest;
  const pl = $('#board-program');
  pl.hidden = !(pg && pg.date === last.date && mode !== 'pre');
  if (!pl.hidden) {
    pl.innerHTML = `프로그램${pg.provisional ? ' <span class="tag-prov">잠정</span>' : ''} 전체 ` +
      `<b class="${dirCls(pg.total)}">${eok(pg.total)}</b> · ${term('arb', '비차익')} ` +
      `<b class="${dirCls(pg.nonarb)}">${eok(pg.nonarb)}</b> · 차익 <b class="${dirCls(pg.arb)}">${eok(pg.arb)}</b>`;
  }

  const keys = FLOW_KEYS.filter(k => k !== 'other_corp' || last.other_corp != null);
  const vals = keys.map(k => ({ key: k, v: last[k] ?? 0 }));
  const max = Math.max(...vals.map(x => Math.abs(x.v)), 1);
  const lead = vals.reduce((a, b) => Math.abs(b.v) > Math.abs(a.v) ? b : a);

  // 주체별 다이버징 바
  const leadWord = whenWord(last.date) === '오늘' ? '오늘의 주역' : '그날의 주역';
  tug.innerHTML = '';
  vals.forEach(({ key, v }, i) => {
    const a = ACTORS[key];
    const lane = el('div', 'lane' + (key === lead.key ? ' grow' : ''));
    const half = Math.abs(v) / max * 50;   // 트랙 절반 기준 %
    const inside = half > 34;              // 막대가 길면 값을 막대 안에(좁은 화면에서 이름과 겹치지 않게)

    lane.innerHTML = `
      <div class="lane-who">
        <div class="lane-face" aria-hidden="true">${a.face}</div>
        <div class="lane-name">${a.name}<small>${key === lead.key ? leadWord : a.sub}</small></div>
      </div>
      <div class="lane-track">
        <div class="lane-bar ${v >= 0 ? 'buy' : 'sell'}"
             style="background:linear-gradient(${v >= 0 ? '90deg' : '270deg'}, ${a.raw}dd, ${a.raw}77);
                    ${v >= 0 ? '' : `left:${50 - half}%;`}">
          <span class="lane-val${inside ? ' in' : ''}" style="${inside ? '' : `color:${a.raw}`}">${eok(v)}</span>
        </div>
      </div>`;
    tug.appendChild(lane);
    growIn(() => { $('.lane-bar', lane).style.width = `${Math.max(half, 0.6)}%`; }, 60 + i * 90);
  });

  renderBalance(vals.filter(x => x.key !== 'other_corp'), last);
}

/** 파는 진영 vs 사는 진영 구성 막대.
 *  개인·외국인·기관만 더하면 양쪽이 맞지 않는다 — 기타법인까지 넣어야 순매수 합이 0 이 된다. */
const GAUGE_EXTRA = { other_corp: { name: '기타법인', raw: '#7a869e' } };

function renderBalance(vals, last) {
  const hasCorp = last && last.other_corp != null;
  const all = hasCorp ? vals.concat([{ key: 'other_corp', v: last.other_corp }]) : vals;
  const who = k => ACTORS[k] || GAUGE_EXTRA[k];
  const sellers = all.filter(x => x.v < 0).sort((a, b) => a.v - b.v);
  const buyers  = all.filter(x => x.v > 0).sort((a, b) => b.v - a.v);
  const sellSum = sellers.reduce((s, x) => s + Math.abs(x.v), 0);
  const buySum  = buyers.reduce((s, x) => s + x.v, 0);
  const total   = sellSum + buySum || 1;

  const wrap = $('.balance');
  const track = $('.balance-track', wrap);
  track.innerHTML = '';
  let cursor = 0;
  const put = (arr, isSell) => arr.forEach(({ key, v }) => {
    const wpc = Math.abs(v) / total * 100;
    const seg = el('div', 'bseg');
    Object.assign(seg.style, {
      position: 'absolute', top: '0', bottom: '0',
      left: `${cursor}%`, width: '0%',
      background: who(key).raw,
      opacity: isSell ? '.85' : '1',
      transition: 'width .9s cubic-bezier(.22,1,.36,1)',
    });
    seg.title = `${who(key).name} ${eok(v)}`;
    track.appendChild(seg);
    growIn(() => { seg.style.width = `${wpc}%`; }, 80);
    cursor += wpc;
  });
  put(sellers, true);
  put(buyers, false);

  const names = a => a.map(x => who(x.key).name).join(' · ') || '없음';
  $('#balance-caption').innerHTML =
    `파는 쪽 <b>${names(sellers)}</b> ${eok(-sellSum)} &nbsp;·&nbsp; ` +
    `사는 쪽 <b>${names(buyers)}</b> ${eok(buySum)}` +
    `<span class="balance-note dim small">${hasCorp
      ? `개인·외국인·기관에 ${term('corp', '기타법인')}까지 더하면 순매수 합은 0입니다.`
      : '순매수와 순매도는 서로의 거울입니다. 누군가 판 물량은 누군가 받습니다.'}</span>`;
}

/* ── 간밤 지표 네 칸 (장 전·휴장일) ─────────────────────── */

const PRE_SHORT = { '^SOX': '미 반도체', '^GSPC': 'S&P500', 'EWY': 'EWY', 'KRW=X': '원/달러' };

/** 지금 '간밤'이라 부를 미국 거래일: 어제(KST)에서 주말을 건너뛴 날. 미국 휴일은 따지지 않는다(그날은 날짜를 적는다) */
function lastUSSession() {
  let d = addDays(todayKST(), -1);
  for (let i = 0; i < 3; i++) {
    const wd = new Date(`${d}T00:00:00Z`).getUTCDay();
    if (wd !== 0 && wd !== 6) break;
    d = addDays(d, -1);
  }
  return d;
}

/** '1년 중 움직임 상위 8%' — 등락 폭(절댓값)의 1년 백분위 */
function moveTxt(p) {
  if (p == null || isNaN(p)) return '';
  return p >= 50 ? `1년 중 움직임 상위 ${rankTxt(100 - p)}` : `1년 중 움직임 하위 ${rankTxt(p)}`;
}

const PRE_DEFAULT = ['^SOX', '^GSPC', 'EWY', 'KRW=X'];

function renderPreopen(show) {
  const box = $('#preopen');
  const items = (D.global?.preopen || PRE_DEFAULT)
    .map(s => (D.global?.items || []).find(g => g.symbol === s)).filter(Boolean);
  box.hidden = !show || !items.length;
  if (box.hidden) return;
  const expect = lastUSSession();
  setText('#preopen-when', `· ${dayLabel(expect)} 미국장 기준`);
  $('#preopen-grid').innerHTML = items.map(g => {
    const old = g.asOf && g.asOf < expect;
    return `<div class="pcell" title="${esc(g.name)} · ${esc(g.asOf)} 기준">
      <div class="pcell-name">${esc(PRE_SHORT[g.symbol] || g.name)}</div>
      <div class="pcell-chg ${dirCls(g.changeRate)}">${pctM(g.changeRate)}</div>
      <div class="pcell-sub">${moveTxt(g.movePctl)}${old ? ` · ${mdy(g.asOf)} 값` : ''}</div>
    </div>`;
  }).join('');
}

/* ── 장중 수급 흐름 ──────────────────────────────────── */

const INTRA_EXTRA = { other_corp: { name: '기타법인', raw: '#7a869e' } };

/** 가장 최근 거래일의 분 단위 누적 순매수(5분 간격). 줄다리기의 코스피/코스닥 선택을 따른다. */
/** 장중엔 수급 보드 바로 아래, 그 밖엔 바로가기 아래 */
function placeIntraday() {
  const card = $('#intraday-card');
  const anchor = boardMode() === 'live' ? $('#board') : $('#jump');
  if (card && anchor && anchor.nextElementSibling !== card) anchor.after(card);
}

function renderIntraday() {
  const card = $('#intraday-card');
  placeIntraday();
  const it = D.flows.markets[heroMarket]?.intraday;
  card.hidden = !(it && it.points?.length >= 2);
  if (card.hidden) return;

  const today = todayKST();
  const settled = !!it.final;             // 20시 이후 값이 있어야 확정. 그 전(장중·마감 직후)은 잠정
  $('#intraday-title').textContent = it.date === today ? '오늘 장중 수급 흐름' : `${dayLabel(it.date)} 장중 수급 흐름`;
  $('#intraday-date').textContent =
    `${mkName(heroMarket)} · 누적 순매수(억원)${settled ? '' : ' · 잠정치'}`;

  const keys = ['individual', 'foreign', 'institution', 'other_corp'];
  const who = k => ACTORS[k] || INTRA_EXTRA[k];
  const pts = it.points;
  const mins = t => +t.slice(0, 2) * 60 + +t.slice(3, 5);
  const T0 = mins(it.open || '09:00'), T1 = mins(it.close || '15:30');   // 수능일은 10:00~16:30

  const svg = $('#intraday-chart');
  const W = chartW(svg), narrow = W < 520;
  const H = narrow ? 240 : 260, M = { t: 14, r: narrow ? 58 : 86, b: 28, l: narrow ? 50 : 62 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const vals = pts.flatMap(p => keys.map(k => p[k] ?? 0));
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const X = t => M.l + (Math.min(Math.max(mins(t), T0), T1) - T0) / (T1 - T0) * pw;
  const Y = v => M.t + ph - (v - lo) / (hi - lo) * ph;

  let g = '';
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}" stroke="#232b40"/>`;
    g += `<text x="${M.l - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#7f89a2"
           font-size="10.5" font-family="ui-monospace,monospace">${eok(v, { sign: false })}</text>`;
  }
  g += `<line x1="${M.l}" y1="${Y(0).toFixed(1)}" x2="${W - M.r}" y2="${Y(0).toFixed(1)}" stroke="#4a5570" stroke-width="1.3"/>`;
  const hh = n => `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`;
  // 시각 라벨은 폭에 맞춰 1시간 또는 2시간 간격
  const every = (T1 - T0) / 60 * 38 > pw ? 120 : 60;
  const ticks = [];
  for (let m = Math.ceil(T0 / 60) * 60; m < T1 - every * 0.6; m += every) ticks.push(hh(m));
  if (!ticks.length || mins(ticks[0]) - T0 >= 30) ticks.unshift(hh(T0));
  ticks.push(hh(T1));
  ticks.forEach(t => {
    g += `<text x="${X(t).toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="#7f89a2"
           font-size="10" font-family="ui-monospace,monospace">${t}</text>`;
  });
  // 끝값 라벨이 겹치지 않게 위아래로 밀어 둔다
  const ends = keys.map(k => ({ k, v: pts[pts.length - 1][k] ?? 0 })).sort((a, b) => Y(a.v) - Y(b.v));
  let lastY = -Infinity;
  ends.forEach(e => { e.y = Math.max(Y(e.v), lastY + 13); lastY = e.y; });
  keys.forEach(k => {
    const line = pts.map(p => `${X(p.t).toFixed(1)},${Y(p[k] ?? 0).toFixed(1)}`).join(' ');
    g += `<polyline points="${line}" fill="none" stroke="${who(k).raw}" stroke-width="${k === 'other_corp' ? 1.6 : 2.2}"
           ${k === 'other_corp' ? 'stroke-dasharray="5 3"' : ''} stroke-linejoin="round" stroke-linecap="round"/>`;
    const e = ends.find(x => x.k === k);
    g += `<text x="${(X(pts[pts.length - 1].t) + 6).toFixed(1)}" y="${(e.y + 4).toFixed(1)}" fill="${who(k).raw}"
           font-size="10.5" font-family="ui-monospace,monospace">${eok(e.v)}</text>`;
  });
  g += `<line id="intra-cursor" y1="${M.t}" y2="${M.t + ph}" stroke="#ffffff" stroke-opacity=".22" visibility="hidden"/>`;
  g += `<rect id="intra-hit" x="${M.l}" y="${M.t}" width="${pw}" height="${ph}" fill="transparent"/>`;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;

  $('#intraday-legend').innerHTML = keys.map(k =>
    `<span><i style="background:${who(k).raw}"></i>${who(k).name}</span>`).join('');

  const f = it.final, a = it.after;
  const close = it.close || '15:30';
  const list = x => keys.map(k => `${who(k).name} ${eok(x[k])}`).join(' · ');
  $('#intraday-note').textContent = f
    ? `정규장 마감(${close}) 뒤에도 값은 조금씩 바뀝니다. ${f.t} 기준 확정: ${list(f)}.`
    : a
      ? `정규장 마감(${close}) 뒤 ${a.t} 기준(20시 전후 확정 전까지 바뀝니다): ${list(a)}.`
      : `${pts[pts.length - 1].t}까지의 누적입니다. 장중 값은 잠정치이고, 장 마감 후 확정치로 바뀝니다.`;

  // 포인터 이벤트라 터치에서도 동작하고, 세로 스크롤은 막지 않는다
  const hit = $('#intra-hit', svg), cursor = $('#intra-cursor', svg), tip = $('#tooltip');
  const move = ev => {
    const box = svg.getBoundingClientRect();
    const px = (ev.clientX - box.left) / box.width * W;
    let i = 0;
    pts.forEach((p, j) => { if (Math.abs(X(p.t) - px) < Math.abs(X(pts[i].t) - px)) i = j; });
    const p = pts[i];
    cursor.setAttribute('x1', X(p.t)); cursor.setAttribute('x2', X(p.t));
    cursor.setAttribute('visibility', 'visible');
    tip.hidden = false;
    tip.innerHTML = `<div class="t-date">${it.date} ${p.t} · 누적</div>` + keys.map(k => `<div class="t-row">
        <span><i style="background:${who(k).raw}"></i>${who(k).name}</span>
        <b class="${dirCls(p[k])}">${eok(p[k])}</b></div>`).join('');
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = `${Math.min(ev.clientX + 14, window.innerWidth - tw - 8)}px`;
    tip.style.top = `${Math.max(8, ev.clientY - th - 12)}px`;
  };
  // 터치는 손을 떼는 순간 pointerleave 가 오므로, 마우스일 때만 떠날 때 숨긴다(바깥을 누르면 hideChartTips).
  // 터치에서 가로로 끌면 차트 상자가 스크롤되고(좁은 화면), 값은 누른 자리의 것이 남는다.
  hit.addEventListener('pointermove', move);
  hit.addEventListener('pointerdown', move);
  hit.addEventListener('pointerleave', ev => { if (ev.pointerType === 'mouse') hideChartTips(); });
}

/* ══════════════════════════════════════════════════════
   개미 파트 — 풍자는 톤에만, 숫자는 있는 그대로
   ══════════════════════════════════════════════════════ */

const hasAnt = () => D.ant && D.ant.actors;
/** 순위·임계값의 기준. 예비 소스로 금액 기준이 됐거나 예전 형식 데이터면 'amount' */
const basisOf = x => (x && x.basis) || 'amount';
const setText = (sel, text) => { const n = $(sel); if (n) n.textContent = text; };

/** 오늘의 매수 강도(거래대금 대비 순매수)를 표본 기간 분포 안에 놓아 본다 */
function renderThermo() {
  $('#thermo-card').hidden = !hasAnt();
  if (!hasAnt()) return;
  const a = D.ant.actors.individual;
  const p = a.todayPercentile;

  $('#thermo-sample').textContent = D.ant.sample.days;
  $('#thermo-basis').innerHTML = basisOf(D.ant) === 'intensity'
    ? `(${term('intensity', '거래대금 대비 순매수')})` : '(순매수 금액 기준)';
  growIn(() => { $('#thermo-ant').style.left = `${p}%`; }, 120);

  const mood = p >= 80 ? { t: '강한 순매수 구간', c: 'up' }
             : p >= 60 ? { t: '사는 쪽', c: 'up' }
             : p > 40  ? { t: '보통', c: 'flat' }
             : p > 20  ? { t: '파는 쪽', c: 'down' }
             :           { t: '강한 순매도 구간', c: 'down' };

  const day = D.ant.sample.to;
  const provisional = day === todayKST() && isProvisionalPhase(D.meta.phase);
  const chg = dayChange('KOSPI', day);
  $('#thermo-nums').innerHTML = `
    <div>
      <span class="thermo-big ${mood.c}">${Math.max(1, Math.round(p))}<small style="font-size:14px">번째</small></span>
      ${term('pctl', '백분위')}
      <span class="thermo-desc" style="margin-left:6px">${mood.t}${provisional ? ' · 장중 잠정치 기준' : ''}</span>
    </div>
    <div class="thermo-desc">${whenWord(day)} 개인 순매수 <b class="${dirCls(a.todayValue)}">${eok(a.todayValue)}</b>` +
    (a.todayIntensity != null ? ` · 거래대금의 <b class="${dirCls(a.todayIntensity)}">${sgn(a.todayIntensity, 1)}%</b>` : '') +
    (chg != null ? ` · 그날 코스피 <b class="${dirCls(chg)}">${sgn(chg)}%</b>` : '') +
    `</div>`;
}

/** 온도계가 지금 같은 극단이었던 과거 날들의 20거래일 뒤 (통계·해설 안) */
function renderContrarian() {
  const box = $('#contrarian');
  if (!hasAnt()) { box.innerHTML = '<p class="dim">데이터 없음</p>'; return; }
  const cr = D.ant.contrarianRead;
  if (!cr) {
    box.innerHTML = `<div class="c-head">오늘은 참고할 만한 극단이 아닙니다</div>
      개미의 매수 강도가 평범한 구간이라 과거 비교 표본을 뽑지 않았습니다.
      <div class="c-note">상·하위 20% 구간에 들어오면 과거 같은 국면의 20거래일 성적을 보여줍니다.</div>`;
    return;
  }
  const better = cr.excess >= 0;
  const lr = cr.longRun;
  const sigNote = (!cr.excessCI ? '' : cr.significant
    ? ` 이 기간만 보면 ${ciTxt(cr.excessCI)}로 0을 포함하지 않지만, 화면의 여러 비교 중 하나이고 이 기간은 상승장이었습니다.`
    : ` ${ciTxt(cr.excessCI)} — 0을 포함해 시장 평균과 구분되지 않습니다.`) +
    (lr ? ` <b>${lr.period} 같은 계산에서는 ${sgn(lr.excess)}%p</b>(90% 범위 ${sgn(lr.ci[0])} ~ ${sgn(lr.ci[1])}%p)로 시장 평균과 구분되지 않았습니다.` : '');
  box.innerHTML = `
    <div class="c-head">🐜 ${esc(cr.label)} — 과거 ${cr.n}번</div>
    그 <b>20거래일 뒤</b> 지수는 평균 <b class="${better ? 'up' : 'down'}">${sgn(cr.r20)}%</b>,
    같은 기간 시장 평균은 <b>${sgn(cr.baseline20)}%</b>였습니다.
    시장 평균 대비 <b class="${better ? 'up' : 'down'}">${sgn(cr.excess)}%p</b>.
    <div class="c-note">과거 기록이지 예측이 아닙니다. 날짜가 서로 겹쳐 독립 사례는 ${cr.n}번보다 훨씬 적습니다.${sigNote}
      매매 근거로 쓰기엔 약한 숫자입니다.</div>`;
}

/* ── 지금과 비슷했던 날들 ────────────────────────────── */

function renderAnalog() {
  const card = $('#analog-card');
  const a = D.analog;
  if (!a || !a.matches?.length) { card.hidden = true; return; }
  card.hidden = false;

  // 검증표의 '유사 국면' 행에서 적중률을 가져온다(없으면 사전 등록 검정 때의 값)
  const evRow = (D.evidence?.rows || []).find(r => r.id === '유사 국면');
  const acc = (evRow?.oos || '').match(/(\d+(?:\.\d+)?)%/)?.[1] || '52.8';
  setText('#analog-acc', `매일 그날까지의 정보로만 맞혀 본 방향 적중률 ${acc}% — 동전 던지기 수준`);

  $('#analog-sample').textContent =
    `${a.sample.from} ~ ${a.sample.to} · ${a.sample.days}거래일에서 검색`;
  const fresh = a.above !== undefined;
  setText('#analog-intro',
    `오늘의 수급(${basisOf(a) === 'intensity' ? '거래대금 대비 순매수 강도' : '순매수 금액'}) + 가격 움직임 조합과 가장 비슷했던 과거의 날을` +
    (fresh ? ' 서로 20거래일 이상 떨어진 날로' : '') + ' 골라, 그 날들로부터 20거래일 뒤 지수가 어떻게 됐는지 보여줍니다.' +
    (fresh ? ' 수급과 가격은 같은 비중으로 비교합니다.' : '') + ' 예측이 아니라 기록입니다.');

  const t = a.today;
  $('#analog-today').innerHTML =
    `오늘(${t.date})의 조합 — 코스피 <b class="${dirCls(t.ret1)}">${t.ret1 >= 0 ? '+' : ''}${t.ret1}%</b>, ` +
    `개인 <b class="${dirCls(t.individual)}">${eok(t.individual)}</b> · ` +
    `외국인 <b class="${dirCls(t.foreign)}">${eok(t.foreign)}</b> · ` +
    `기관 <b class="${dirCls(t.institution)}">${eok(t.institution)}</b>. ` +
    `이 조합과 가장 가까웠던 ${a.matches.length}일:`;

  const list = $('#analog-list');
  list.innerHTML = '';
  a.matches.forEach((m, i) => {
    const row = el('div', 'analog-row', `
      <div class="analog-date">${m.date}</div>
      <div class="analog-desc">코스피 <b class="${dirCls(m.ret1)}">${m.ret1 >= 0 ? '+' : ''}${m.ret1}%</b> ·
        개인 <b class="${dirCls(m.individual)}">${eok(m.individual)}</b> ·
        외인 <b class="${dirCls(m.foreign)}">${eok(m.foreign)}</b> · ${nfmt(m.close, 0)}p</div>
      <div class="analog-out ${dirCls(m.ret20)}">${m.ret20 >= 0 ? '+' : ''}${m.ret20}%<small>20거래일 뒤</small></div>`);
    row.style.animationDelay = `${i * 50}ms`;
    list.appendChild(row);
  });

  const n = a.matches.length;
  if (a.above == null) {          // 예전 형식 데이터
    const better = a.avgRet20 >= a.baseline20;
    $('#analog-verdict').innerHTML =
      `이 ${n}일의 20거래일 뒤 평균은 <b class="${dirCls(a.avgRet20)}">${sgn(a.avgRet20)}%</b> ` +
      `(전체 기간 평균 <b>${sgn(a.baseline20)}%</b>) — ` +
      (better ? '비슷한 날들의 뒤가 평균보다 좋았습니다.' : '비슷한 날들의 뒤가 평균보다 나빴습니다.') +
      `<br><span class="dim small">표본 ${n}개는 통계가 아니라 일화입니다. 그날과 지금은 다른 시장입니다.</span>`;
    return;
  }
  const head = a.verdict === 'better' ? '비슷한 날들의 뒤는 대체로 시장 평균보다 좋았습니다.'
             : a.verdict === 'worse'  ? '비슷한 날들의 뒤는 대체로 시장 평균보다 나빴습니다.'
             :                          '비슷한 날들의 뒤는 엇갈렸습니다.';
  const sim = a.similarity || {};
  const simNote = sim.rare ? '오늘과 닮은 날이 드뭅니다. 위 날들은 "그나마 가까운" 날입니다. '
                : sim.nearest > sim.typical ? '평소보다 닮은 정도가 약한 날들입니다. ' : '';
  $('#analog-verdict').innerHTML =
    `이 ${n}일 중 <b>${a.above}번</b>이 20거래일 뒤 시장 평균(<b>${sgn(a.baseline20)}%</b>)보다 좋았습니다 ` +
    `(평균 ${sgn(a.avgRet20)}%, 범위 ${sgn(a.minRet20)} ~ ${sgn(a.maxRet20)}%) — ${head}` +
    `<br><span class="dim small">${simNote}표본 ${n}개는 통계가 아니라 일화입니다. ` +
    `매칭일끼리는 20거래일 이상 떨어뜨려 결과 구간이 겹치지 않게 했습니다.</span>`;
}

/* ── 개미 장바구니 vs 외인 장바구니 ──────────────────── */

function renderBaskets() {
  const card = $('#basket-card');
  const b = D.antstocks;
  if (!b || !b.antBasket?.length) { card.hidden = true; return; }
  card.hidden = false;

  $('#basket-window').textContent =
    `최근 ${b.window.days}거래일 (${b.window.from.slice(4,6)}/${b.window.from.slice(6,8)} ~ ` +
    `${b.window.to.slice(4,6)}/${b.window.to.slice(6,8)}) · 시총상위 ${b.universe}종목 대상`;

  const since = b.antAvgSinceBuy !== undefined;
  const antV = since ? b.antAvgSinceBuy : b.antAvgChange;
  const forV = since ? b.foreignAvgSinceBuy : b.foreignAvgChange;
  // 같은 잣대의 기준선: 같은 날짜·같은 수량 비중으로 그 시장 지수를 샀다면.
  // 두 장바구니는 담은 시점이 달라 지수 흐름도 다르다 — 그래서 '지수 대비'끼리 견준다
  const vsIdx = m => since && m != null ? `<br>같은 날 지수를 샀다면 ${sgn(m)}%` : '';
  const label = since ? '산 뒤 평균 등락(추정)' : '평균 등락률';
  const exA = since && antV != null && b.antAvgMarketSinceBuy != null ? antV - b.antAvgMarketSinceBuy : null;
  const exF = since && forV != null && b.foreignAvgMarketSinceBuy != null ? forV - b.foreignAvgMarketSinceBuy : null;
  const exTxt = ex => ex == null ? '' : `<div class="bh-ex">지수 대비 <b class="${dirCls(ex)}">${sgn(ex)}%p</b></div>`;
  $('#basket-headline').innerHTML = `
    <div class="bh-side">
      <div class="bh-who">🐜 개미가 담은 ${b.antBasket.length}종목</div>
      <div class="bh-chg ${dirCls(antV)}">${sgn(antV)}%</div>
      <div class="bh-sub">${label}${vsIdx(b.antAvgMarketSinceBuy)}</div>
      ${exTxt(exA)}
    </div>
    <div class="bh-vs">VS</div>
    <div class="bh-side">
      <div class="bh-who">🦅 외인이 담은 ${b.foreignBasket.length}종목</div>
      <div class="bh-chg ${dirCls(forV)}">${sgn(forV)}%</div>
      <div class="bh-sub">${label}${vsIdx(b.foreignAvgMarketSinceBuy)}</div>
      ${exTxt(exF)}
    </div>`;
  let verdict = '';
  if (exA != null && exF != null) {
    const d = exA - exF;
    verdict = Math.abs(d) < 1
      ? `지수 대비로 견주면 두 장바구니의 차이는 ${Math.abs(d).toFixed(1)}%p로 1%p 안쪽입니다. 어느 쪽이 나았다고 말하기 어렵습니다.`
      : `지수 대비로 견주면 ${d > 0 ? '개미' : '외인'} 장바구니가 ${Math.abs(d).toFixed(1)}%p 높았습니다. ` +
        `판 날을 빼고 계산한 추정치이고, ${b.window.days}거래일 한 구간의 기록입니다.`;
  } else {
    verdict = '같은 날 지수를 샀을 때의 기준선이 없는 예전 형식 데이터라, 두 장바구니를 견주는 문장은 생략합니다.';
  }
  setText('#basket-verdict', verdict);

  const ret = x => since ? x.sinceBuy : x.change;
  const col = (title, items) => `
    <div class="basket-col">
      <h3>${title}</h3>
      ${items.map(x => `
        <div class="basket-item">
          <span class="bi-name">${esc(x.name)}<small>${x.market === 'KOSDAQ' ? '코스닥' : ''}</small></span>
          <span class="bi-val">${eok(x.value)}</span>
          <span class="bi-chg ${dirCls(ret(x))}">${sgn(ret(x))}%</span>
        </div>`).join('')}
    </div>`;
  $('#basket-grid').innerHTML =
    col(`🐜 개미 순매수 상위 ${b.antBasket.length}`, b.antBasket) +
    col(`🦅 외인 순매수 상위 ${b.foreignBasket.length}`, b.foreignBasket);

  const exRow = x => `<div class="row"><span>${esc(x.name)}</span>
    <b class="${dirCls(ret(x))}">${sgn(ret(x))}%</b></div>`;
  const rowsOr = arr => arr?.length ? arr.map(exRow).join('') : '<div class="row dim">해당 종목 없음</div>';
  $('#basket-extremes').innerHTML = `
    <div class="extreme"><h3>개미가 담은 뒤 오른 종목</h3>${rowsOr(b.wins)}</div>
    <div class="extreme"><h3>개미가 담은 뒤 내린 종목</h3>${rowsOr(b.tears)}</div>`;

  $('#basket-note').textContent = b.note || '';
}

/* ── 외국인 현물·선물 비교 ───────────────────────────── */

function renderFutures() {
  const card = $('#futures-card');
  const f = D.futures;
  card.hidden = !(f && f.daily?.length);
  if (card.hidden) return;

  const div = f.divergence;
  const box = $('#fut-divergence');
  if (div) {
    const state = div.state || (div.aligned ? 'aligned' : 'split');
    const period = div.from ? `${mdy(div.from)}~${mdy(div.to)}` : `최근 ${div.window}거래일`;
    const both = `외국인 현물 <b>${eok(div.spotForeign)}</b>, 선물 <b>${sgnInt(div.futuresForeign)}계약</b>`;
    const typ = `최근 ${div.typicalDays || 120}거래일 5일 합계의 중앙값`;
    if (!div.state) {               // 예전 형식: 부호만 비교한 결과라 크기를 말할 근거가 없다
      box.className = div.aligned ? 'divergence' : 'divergence split';
      box.innerHTML = `
        <div class="div-head">${div.aligned ? '현물과 선물이 같은 방향입니다' : '현물과 선물의 방향이 다릅니다'}</div>
        ${period} ${both}.`;
    } else if (state === 'split') {
      const h = div.history;
      box.className = 'divergence split';
      box.innerHTML = `
        <div class="div-head">현물과 선물이 갈립니다</div>
        ${period} ${both}. 둘 다 평소(${typ})보다 큰 움직임입니다.` +
        (h ? `<br>과거 같은 모양으로 갈렸던 ${h.n}일(${h.episodes}개 국면)의 20거래일 뒤 코스피는 평균
              <b class="${dirCls(h.r20)}">${sgn(h.r20)}%</b>, 모든 날 평균은 ${sgn(h.baseline20)}%였습니다.
              <span class="dim">예측이 아니라 기록입니다.</span>` : '');
    } else if (state === 'weak') {
      const small = [];
      if (div.spotTypical != null && Math.abs(div.spotForeign) < div.spotTypical) small.push(`현물(평소 ${eok(div.spotTypical, { sign: false })})`);
      if (div.futuresTypical != null && Math.abs(div.futuresForeign) < div.futuresTypical) small.push(`선물(평소 ${div.futuresTypical.toLocaleString()}계약)`);
      const names = small.map(x => x.split('(')[0]).join('·') || '한쪽';
      box.className = 'divergence';
      box.innerHTML = `
        <div class="div-head">${names} 쪽 움직임이 평소보다 작습니다</div>
        ${period} ${both}. ${small.join('·') || '한쪽'} — 평소(${typ})에 못 미쳐 갈림·일치를 판단하지 않습니다.`;
    } else {
      box.className = 'divergence';
      box.innerHTML = `
        <div class="div-head">현물과 선물이 같은 방향입니다</div>
        ${period} ${both}. 둘이 엇갈리지 않았습니다.`;
    }
  }

  // 최근 20일 외국인 선물 순매수 막대 차트
  const rows = f.daily.slice(-20);
  const svg = $('#fut-chart');
  const W = chartW(svg), H = 190, M = { t: 12, r: 10, b: 24, l: 50 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const vals = rows.map(r => r.foreign ?? 0);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const Y = v => M.t + ph - (v - lo) / (hi - lo) * ph;
  const bw = pw / rows.length;

  let g = '';
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * i / 3, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}" stroke="#232b40"/>`;
    g += `<text x="${M.l - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#7f89a2"
           font-size="10.5" font-family="ui-monospace,monospace">${Math.round(v).toLocaleString()}</text>`;
  }
  g += `<line x1="${M.l}" y1="${Y(0).toFixed(1)}" x2="${W - M.r}" y2="${Y(0).toFixed(1)}" stroke="#4a5570" stroke-width="1.3"/>`;
  const step = labelStep(rows.length, pw);
  rows.forEach((r, i) => {
    const v = r.foreign ?? 0;
    const x = M.l + i * bw + bw * 0.18;
    const y0 = Y(0), y1 = Y(v);
    g += `<rect x="${x.toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" width="${(bw * 0.64).toFixed(1)}"
           height="${Math.max(Math.abs(y1 - y0), 0.8).toFixed(1)}" rx="2"
           fill="${v >= 0 ? '#ff4d4d' : '#4d94ff'}" opacity=".88"><title>${r.date} ${v >= 0 ? '+' : ''}${v.toLocaleString()}계약</title></rect>`;
    if ((rows.length - 1 - i) % step === 0)
      g += `<text x="${(x + bw * 0.32).toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="#7f89a2"
             font-size="10" font-family="ui-monospace,monospace">${mdy(r.date)}</text>`;
  });
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;

  const st = f.streaks?.foreign;
  const last = f.latest;
  $('#fut-note').textContent =
    `외국인 선물 순매수 최근 20거래일. ${whenWord(last.date)} ${sgnInt(last.foreign ?? 0)}계약` +
    (st && st.days >= 2 ? ` · ${st.days}일 연속 ${st.side === 'buy' ? '매수' : '매도'}` : '') + '.';
}

/* ── 프로그램 매매 · 공매도 ─────────────────────────────── */

const provTag = r => r && r.provisional ? ' <span class="tag-prov">잠정</span>' : '';

/** 0 기준 막대 차트 (색 = 부호). 잠정치 막대는 흐리게 */
function drawBars(svg, rows, val, title, fmt) {
  const W = chartW(svg), H = 190, M = { t: 12, r: 10, b: 24, l: 58 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const vals = rows.map(r => val(r) ?? 0);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const Y = v => M.t + ph - (v - lo) / (hi - lo) * ph;
  const bw = pw / rows.length;
  let g = '';
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * i / 3, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}" stroke="#232b40"/>`;
    g += `<text x="${M.l - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#7f89a2"
           font-size="10.5" font-family="ui-monospace,monospace">${fmt(v)}</text>`;
  }
  g += `<line x1="${M.l}" y1="${Y(0).toFixed(1)}" x2="${W - M.r}" y2="${Y(0).toFixed(1)}" stroke="#4a5570" stroke-width="1.3"/>`;
  const step = labelStep(rows.length, pw);
  rows.forEach((r, i) => {
    const v = val(r) ?? 0;
    const x = M.l + i * bw + bw * 0.15;
    const y0 = Y(0), y1 = Y(v);
    g += `<rect x="${x.toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" width="${(bw * 0.7).toFixed(1)}"
           height="${Math.max(Math.abs(y1 - y0), 0.8).toFixed(1)}" rx="1.5"
           fill="${v >= 0 ? '#ff4d4d' : '#4d94ff'}" opacity="${r.provisional ? 0.4 : 0.88}"><title>${esc(title(r))}</title></rect>`;
    if ((rows.length - 1 - i) % step === 0)
      g += `<text x="${(x + bw * 0.35).toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="#7f89a2"
             font-size="10" font-family="ui-monospace,monospace">${mdy(r.date)}</text>`;
  });
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;
}

function renderProgram() {
  const card = $('#program-card');
  const all = D.program?.markets || {};
  card.hidden = !Object.keys(all).length;
  if (card.hidden) return;
  $$('#program-market-seg button').forEach(b => b.classList.toggle('on', b.dataset.market === programMarket));
  const m = all[programMarket];
  if (!m || !m.daily?.length) {
    $('#program-stats').innerHTML = '<p class="dim">이 시장의 프로그램 매매 데이터를 받지 못했습니다.</p>';
    $('#program-chart').innerHTML = '';
    $('#program-note').textContent = '';
    return;
  }
  const lt = m.latest, st = lt.streak || {};
  const q = lt.pctl, buy = lt.total > 0;
  // 순위는 극단일 때만 말한다 — 부호를 섞은 백분위로 '순매수 쪽 상위 45%'라고 하면 작은 매수도 두드러져 보인다
  const rank = q == null ? '' :
    (buy && q >= 80) || (!buy && q <= 20)
      ? `최근 ${lt.days}거래일 중 ${buy ? '순매수' : '순매도'} 쪽 상위 ${rankTxt(buy ? 100 - q : q)}`
      : `최근 ${lt.days}거래일 기준 평소 범위`;
  const rankNote = rank && lt.provisional ? `${rank} · 잠정치 기준` : rank;
  $('#program-stats').innerHTML = `
    <div class="cstat">
      <div class="cstat-label">전체 순매수 · ${mdy(lt.date)}${provTag(lt)}</div>
      <div class="cstat-val ${dirCls(lt.total)}">${eok(lt.total)}</div>
      <div class="cstat-sub">${rankNote}</div>
    </div>
    <div class="cstat">
      <div class="cstat-label">연속</div>
      <div class="cstat-val">${st.days ? `${st.days}일 ${st.side === 'buy' ? '순매수' : '순매도'}` : '—'}</div>
      <div class="cstat-sub">${st.days ? `그동안 ${eok(st.total)}` : ''}</div>
    </div>
    <div class="cstat">
      <div class="cstat-label">비차익 (바스켓)</div>
      <div class="cstat-val ${dirCls(lt.nonarb)}">${eok(lt.nonarb)}</div>
      <div class="cstat-sub">여러 종목을 한꺼번에</div>
    </div>
    <div class="cstat">
      <div class="cstat-label">차익 (선물↔현물)</div>
      <div class="cstat-val ${dirCls(lt.arb)}">${eok(lt.arb)}</div>
      <div class="cstat-sub">가격 차를 노린 매매</div>
    </div>`;
  drawBars($('#program-chart'), m.daily, r => r.total,
           r => `${r.date} 전체 ${eok(r.total)} · 비차익 ${eok(r.nonarb)} · 차익 ${eok(r.arb)}${r.provisional ? ' (잠정)' : ''}`,
           v => eok(v, { sign: false }));
  $('#program-note').textContent =
    `막대 = 전체 순매수, 최근 ${m.daily.length}거래일. 장 마감 뒤에도 20시 무렵까지 값이 바뀌어, 그 전 수집분은 잠정치입니다.`;
}

/** 선 차트: 값 + 가로 기준선(평균) */
function drawLine(svg, rows, val, fmt, ref, color) {
  const W = chartW(svg), H = 190, M = { t: 12, r: 14, b: 24, l: 46 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const vals = rows.map(val).concat(ref != null ? [ref] : []);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  const X = i => M.l + (rows.length === 1 ? pw / 2 : i / (rows.length - 1) * pw);
  const Y = v => M.t + ph - (v - lo) / (hi - lo) * ph;
  let g = '';
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * i / 3, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}" stroke="#232b40"/>`;
    g += `<text x="${M.l - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#7f89a2"
           font-size="10.5" font-family="ui-monospace,monospace">${fmt(v)}</text>`;
  }
  if (ref != null)
    g += `<line x1="${M.l}" y1="${Y(ref).toFixed(1)}" x2="${W - M.r}" y2="${Y(ref).toFixed(1)}" stroke="#ffffff"
           stroke-opacity=".35" stroke-dasharray="4 3"/>`;
  g += `<polyline points="${rows.map((r, i) => `${X(i).toFixed(1)},${Y(val(r)).toFixed(1)}`).join(' ')}" fill="none"
         stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const n = rows.length - 1;
  g += `<circle cx="${X(n).toFixed(1)}" cy="${Y(val(rows[n])).toFixed(1)}" r="3.4" fill="${color}"/>`;
  const step = labelStep(rows.length, pw);
  rows.forEach((r, i) => {
    if ((n - i) % step) return;
    g += `<text x="${Math.min(X(i), W - 16).toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="#7f89a2"
           font-size="10" font-family="ui-monospace,monospace">${mdy(r.date)}</text>`;
  });
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;
}

function renderShort() {
  const card = $('#short-card');
  const all = D.short?.markets || {};
  card.hidden = !Object.keys(all).length;
  if (card.hidden) return;
  $$('#short-market-seg button').forEach(b => b.classList.toggle('on', b.dataset.market === shortMarket));
  const m = all[shortMarket];
  const d = m?.latest?.daily, b = m?.latest?.balance;
  const balNote = '순보유잔고는 보고 의무(상장주식의 0.01% 이상 등)가 있는 물량만 합친 값이라 실제 잔고보다 작습니다. 출처 한국거래소.';
  if (!d && !b) {
    $('#short-stats').innerHTML = '<p class="dim">이 시장의 공매도 데이터를 받지 못했습니다.</p>';
    $('#short-chart').innerHTML = '';
    $('#short-note').textContent = '';
    return;
  }
  // 잔고는 원래 2거래일 늦다 — 실제로 몇 거래일 뒤처졌는지 거래 기록 날짜로 센다
  const lag = b ? (m.daily || []).filter(r => r.date > b.date).length : 0;
  const q = d?.pctPctl;
  const where = q == null ? '' : q >= 80 ? `재개 뒤 ${d.days}거래일 중 비중 상위 ${rankTxt(100 - q)}`
              : q <= 20 ? `재개 뒤 ${d.days}거래일 중 비중 하위 ${rankTxt(q)}` : `재개 뒤 ${d.days}거래일 기준 평소 범위`;
  const whereNote = where && d.provisional ? `${where} · 잠정치 기준` : where;
  $('#short-stats').innerHTML = (d ? `
    <div class="cstat">
      <div class="cstat-label">공매도 거래대금 · ${mdy(d.date)}${provTag(d)}</div>
      <div class="cstat-val">${eok(d.value, { sign: false })}</div>
      <div class="cstat-sub">전체 거래대금 ${eok(d.total, { sign: false })}</div>
    </div>
    <div class="cstat">
      <div class="cstat-label">거래대금 대비 비중</div>
      <div class="cstat-val">${d.pct.toFixed(2)}%</div>
      <div class="cstat-sub">20일 평균 ${d.pctAvg20 != null ? d.pctAvg20.toFixed(2) + '%' : '—'}${whereNote ? ' · ' + whereNote : ''}</div>
    </div>` : '<p class="dim">공매도 거래 데이터를 받지 못했습니다.</p>') + (b ? `
    <div class="cstat">
      <div class="cstat-label">순보유잔고 · ${mdy(b.date)}${lag ? ` <span class="dim">(${lag}거래일 늦음)</span>` : ''}</div>
      <div class="cstat-val">${eok(b.value, { sign: false })}</div>
      <div class="cstat-sub">시가총액의 ${b.pct ?? '—'}%</div>
    </div>
    <div class="cstat">
      <div class="cstat-label">잔고 변화</div>
      <div class="cstat-val ${dirCls(b.d20)}">${b.d20 != null ? eok(b.d20) : '—'}</div>
      <div class="cstat-sub">20거래일 · 5거래일 ${b.d5 != null ? eok(b.d5) : '—'}</div>
    </div>` : '');
  if (!d) {
    $('#short-chart').innerHTML = '';
    $('#short-note').textContent = balNote;
    return;
  }
  const rows = m.daily.slice(-120);
  const settled = m.daily.filter(r => !r.provisional);
  const avg = settled.length ? settled.reduce((s, r) => s + r.pct, 0) / settled.length : null;
  drawLine($('#short-chart'), rows, r => r.pct, v => `${v.toFixed(1)}%`, avg, '#c58bff');
  $('#short-note').innerHTML =
    `<span style="color:#c58bff">━</span> 거래대금 대비 공매도 비중, 최근 ${rows.length}거래일 · ` +
    `<span style="opacity:.5">┄</span> 재개 뒤 평균${avg != null ? ` ${avg.toFixed(2)}%` : ''}. ` + balNote;
}

/* ── 빚투 체온계 ─────────────────────────────────────── */

function renderCredit() {
  const card = $('#credit-card');
  const c = D.credit;
  card.hidden = !(c && c.loans?.length);
  if (card.hidden) return;

  const ln = c.latest.loans || {};
  const mo = c.latest.money || {};
  // 20일 평균의 2배여도 절대 규모가 작으면 경보가 아니다 — 이 기간 반대매매 중 상위 20% 일 때만
  const liqHot = mo.liquidation != null && mo.liqAvg20 && mo.liquidation >= mo.liqAvg20 * 2 &&
                 (mo.liqPctl == null || mo.liqPctl >= 80);

  $('#credit-stats').innerHTML = `
    <div class="cstat">
      <div class="cstat-label">신용융자 잔고 (빚투)</div>
      <div class="cstat-val">${eok(ln.total, { sign: false })}</div>
      <div class="cstat-sub">5일 ${ln.d5 != null ? eok(ln.d5) : '—'} · 20일 ${ln.d20 != null ? eok(ln.d20) : '—'}</div>
    </div>
    <div class="cstat ${liqHot ? 'alert' : ''}">
      <div class="cstat-label">반대매매${liqHot ? ' · 20일 평균의 2배 이상' : ''}</div>
      <div class="cstat-val">${eok(mo.liquidation, { sign: false })}</div>
      <div class="cstat-sub">20일 평균 ${eok(mo.liqAvg20, { sign: false })} · 미수금 대비 ${mo.liqRatio ?? '—'}%` +
      (mo.liqPctl != null ? ` · ${mo.days}일 중 상위 ${rankTxt(100 - mo.liqPctl)}` : '') + `</div>
    </div>
    <div class="cstat">
      <div class="cstat-label">투자자 예탁금 (대기 자금)</div>
      <div class="cstat-val">${eok(mo.deposits, { sign: false })}</div>
      <div class="cstat-sub">${mo.date || ''}</div>
    </div>
    <div class="cstat">
      <div class="cstat-label">위탁매매 미수금</div>
      <div class="cstat-val">${eok(mo.receivables, { sign: false })}</div>
      <div class="cstat-sub">외상으로 산 금액</div>
    </div>`;

  // 신용융자 잔고 라인 + 코스피 오버레이
  const rows = c.loans.slice(-120).filter(r => r.total != null);
  const closeBy = {};
  (D.market.indices?.KOSPI?.history || []).forEach(h => { closeBy[h.date] = h.close; });

  const svg = $('#credit-chart');
  const W = chartW(svg), H = 190, M = { t: 12, r: 16, b: 24, l: 56 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const vals = rows.map(r => r.total);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const X = i => M.l + (rows.length === 1 ? pw / 2 : i / (rows.length - 1) * pw);
  const Y = v => M.t + ph - (v - lo) / (hi - lo) * ph;

  const closes = rows.map(r => closeBy[r.date] ?? null);
  const cv = closes.filter(v => v != null);
  const cLo = cv.length ? Math.min(...cv) : 0, cHi = cv.length ? Math.max(...cv) : 1;
  const CY = v => M.t + ph - (v - cLo) / ((cHi - cLo) || 1) * ph;

  let g = '';
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * i / 3, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}" stroke="#232b40"/>`;
    g += `<text x="${M.l - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#7f89a2"
           font-size="10.5" font-family="ui-monospace,monospace">${eok(v, { sign: false })}</text>`;
  }
  if (cv.length > 1) {
    const pts = closes.map((v, i) => v == null ? null : `${X(i).toFixed(1)},${CY(v).toFixed(1)}`)
                      .filter(Boolean).join(' ');
    g += `<polyline points="${pts}" fill="none" stroke="#ffffff" stroke-opacity=".3"
           stroke-width="1.4" stroke-dasharray="4 3"/>`;
  }
  const pts = rows.map((r, i) => `${X(i).toFixed(1)},${Y(r.total).toFixed(1)}`).join(' ');
  g += `<polyline points="${pts}" fill="none" stroke="#ffb02e" stroke-width="2.2"
         stroke-linejoin="round" stroke-linecap="round"/>`;
  g += `<circle cx="${X(rows.length - 1).toFixed(1)}" cy="${Y(rows[rows.length - 1].total).toFixed(1)}"
         r="3.4" fill="#ffb02e"/>`;
  const step = labelStep(rows.length, pw);
  rows.forEach((r, i) => {
    if ((rows.length - 1 - i) % step) return;
    g += `<text x="${Math.min(X(i), W - 16).toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="#7f89a2"
           font-size="10" font-family="ui-monospace,monospace">${mdy(r.date)}</text>`;
  });
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;

  // 순위는 극단일 때만 말한다. 증가·감소를 섞은 백분위를 '증가 쪽 상위 60%'처럼 쓰면 가장 작은 증가도 두드러져 보인다
  const q = ln.d20Pctl;
  const fast = q != null && ln.d20 != null && ((ln.d20 > 0 && q >= 90) || (ln.d20 < 0 && q <= 10));
  const trend = ln.d20 == null ? '' :
    `최근 20거래일 ${eok(ln.d20)}` +
    (fast ? ` — 이 기간 20일 변화 가운데 ${ln.d20 > 0 ? '가장 크게 는 쪽' : '가장 크게 준 쪽'} ` +
            `${rankTxt(ln.d20 > 0 ? 100 - q : q)}.` : '.') +
    (fast && ln.d20 > 0 ? ' 빚으로 산 물량은 주가가 빠지면 반대매매로 나올 수 있습니다.' : '');
  $('#credit-note').innerHTML =
    `<span style="color:#ffb02e">━</span> 신용융자 잔고 · <span style="opacity:.5">┄</span> 코스피. ${trend}`;
}

function quip(k, grade, significant) {
  // 시장 평균과 구분되지 않거나 차이가 작으면 풍자도 하지 않는다 — 숫자가 말하지 않는 걸 말하지 않기
  if (significant === false) {
    return '이 표본에서는 시장 평균과 구분되지 않습니다. 잘했다고도, 못했다고도 말하기 어렵습니다.';
  }
  if (grade === 'C') {
    return significant
      ? '시장 평균과 차이는 있지만 1%p 안쪽으로 작습니다.'
      : '시장 평균과 거의 같았습니다.';
  }
  const good = grade === 'A' || grade === 'B';
  if (k === 'individual') {
    return good
      ? '이 표본에서는 개미가 산 시점이 나쁘지 않았습니다. 흔한 일은 아니니 기록해 둡시다.'
      : '열심히 산 게 문제가 아니라, 살 때를 고른 게 문제였습니다.';
  }
  if (k === 'foreign') {
    return good
      ? '급할 게 없는 쪽이 유리했습니다. 자금 크기보다 버틸 수 있는 시간의 차이일지도 모릅니다.'
      : '외국인도 틀립니다. 적어도 이 표본에서는 그랬습니다.';
  }
  return good
    ? '기관이 제 몫을 한 표본입니다.'
    : '기관이라고 다 잘하지는 않습니다. 남의 돈이라 그런지도 모르겠습니다.';
}

/** 성적표에 쓸 시장의 계산 결과. 코스닥은 ant.byMarket 안에 있다(없으면 코스피). */
function reportAnt() {
  const kq = D.ant.byMarket?.KOSDAQ;
  if (reportMarket === 'KOSDAQ' && !kq) reportMarket = 'KOSPI';
  return reportMarket === 'KOSDAQ' ? kq : D.ant;
}

function renderReportCard() {
  $('#report-card').hidden = !hasAnt();
  if (!hasAnt()) return;
  const ant = reportAnt(), base = ant.baseline;
  const mkName = reportMarket === 'KOSDAQ' ? '코스닥' : '코스피';
  const seg = $('#report-market-seg');
  if (seg) {
    seg.hidden = !D.ant.byMarket?.KOSDAQ;
    $$('button', seg).forEach(b => b.classList.toggle('on', b.dataset.market === reportMarket));
  }
  $('#report-sample').textContent =
    `${ant.sample.from} ~ ${ant.sample.to} · ${ant.sample.days}거래일 · ${mkName} 기준`;
  // 흑역사·반대로 가는 본능·한계 고백·온도계는 코스피 기준이다 — 코스닥 성적표를 보는 동안 헷갈리지 않게
  setText('#kospi-only-note', reportMarket === 'KOSDAQ'
    ? '아래 흑역사·반대로 가는 본능·한계 고백과 위 온도계는 코스피 기준입니다.' : '');
  const hasCI = ACTOR_KEYS.some(k => ant.actors[k].excessCI !== undefined);
  const rule = basisOf(ant) === 'intensity' ? '그날 거래대금 대비 순매수 상위 20%' : '순매수 금액 상위 20%';
  setText('#report-intro',
    `각 주체가 크게 사들인 날(${rule})로부터 20거래일 뒤, ${mkName} 지수가 얼마나 움직였는지를 시장 평균과 비교했습니다. ` +
    `실제 손익이 아니라 '산 시점'의 채점입니다.` +
    (hasCI ? ' 차이가 표본의 잡음 범위(90% 범위가 0을 포함) 안이면 C로 둡니다.' : ''));

  // 최고·최저 강조는 시장 평균과 구분되는 주체에만 — 잡음끼리 순위를 매기지 않는다
  const marked = ACTOR_KEYS.filter(k => ant.actors[k].significant !== false);
  const excesses = marked.map(k => ant.actors[k].excess20 ?? 0);
  const worst = Math.min(...excesses), best = Math.max(...excesses);

  const box = $('#grades');
  box.innerHTML = '';
  ACTOR_KEYS.forEach(k => {
    const a = ant.actors[k], A = ACTORS[k];
    const ex = a.excess20;
    const cls = !marked.includes(k) || marked.length < 2 ? '' : ex === worst ? ' worst' : ex === best ? ' best' : '';
    box.appendChild(el('div', `grade-card${cls}`, `
      <div class="grade-top">
        <span class="grade-face" aria-hidden="true">${A.face}</span>
        <span class="grade-who">${A.name}<small>크게 산 날 ${a.heavyBuy.n20 ?? a.heavyBuy.n}일 기준</small></span>
        <span class="grade-letter g-${a.grade}">${a.grade}${a.significant === false ? '<small>구분 안 됨</small>' : ''}</span>
      </div>
      <div class="grade-rows">
        ${[1, 5, 20].map(h => `
          <div class="grade-row">
            <span>${h}거래일 뒤</span>
            <b class="${dirCls(a.heavyBuy['r' + h])}">${sgn(a.heavyBuy['r' + h])}%</b>
          </div>`).join('')}
        <div class="grade-row">
          <span>시장 평균(20일)</span><b class="dim">${sgn(base.r20)}%</b>
        </div>
      </div>
      <div class="grade-excess">
        <span>시장 대비</span>
        <b class="${dirCls(ex)}">${sgn(ex)}%p</b>
      </div>
      ${a.excessCI ? `<div class="grade-ci">${ciTxt(a.excessCI)}${a.significant ? '' : ' · 0을 포함 → 구분 안 됨'}</div>` : ''}
      <p class="grade-quip">${quip(k, a.grade, a.significant)}</p>`));
  });

  renderTimingChart();
}

/** 주체별 · 기간별 '크게 산 날 이후 수익률' 을 시장 평균선과 함께 */
function renderTimingChart() {
  if (!hasAnt()) return;
  const svg = $('#timing-chart'), ant = reportAnt();
  const W = chartW(svg), H = W < 520 ? 220 : 230, M = { t: 16, r: 10, b: 34, l: W < 520 ? 44 : 56 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const HS = [1, 5, 20];

  const vals = ACTOR_KEYS.flatMap(k => HS.map(h => ant.actors[k].heavyBuy['r' + h] ?? 0))
                         .concat(HS.map(h => ant.baseline['r' + h]));
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const pad = (hi - lo) * 0.14 || 1; lo -= pad; hi += pad;
  const Y = v => M.t + ph - (v - lo) / (hi - lo) * ph;

  const gw = pw / HS.length;            // 기간 그룹 폭
  const bw = Math.min(46, gw / 4.4);    // 막대 폭

  let g = '';
  for (let i = 0; i <= 4; i++) {
    const v = lo + (hi - lo) * i / 4, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}" stroke="#232b40"/>`;
    g += `<text x="${M.l - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#7f89a2"
           font-size="11" font-family="ui-monospace,monospace">${v.toFixed(0)}%</text>`;
  }
  g += `<line x1="${M.l}" y1="${Y(0).toFixed(1)}" x2="${W - M.r}" y2="${Y(0).toFixed(1)}" stroke="#4a5570" stroke-width="1.4"/>`;

  HS.forEach((h, gi) => {
    const cx = M.l + gw * gi + gw / 2;
    ACTOR_KEYS.forEach((k, ai) => {
      const v = ant.actors[k].heavyBuy['r' + h] ?? 0;
      const x = cx - bw * 1.5 - 4 + ai * (bw + 4);
      const y0 = Y(0), y1 = Y(v);
      g += `<rect x="${x.toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" width="${bw.toFixed(1)}"
             height="${Math.max(Math.abs(y1 - y0), 1).toFixed(1)}" rx="3" fill="${ACTORS[k].raw}" opacity=".9"/>`;
      g += `<text x="${(x + bw / 2).toFixed(1)}" y="${(v >= 0 ? y1 - 5 : y1 + 13).toFixed(1)}"
             text-anchor="middle" fill="${ACTORS[k].raw}" font-size="10"
             font-family="ui-monospace,monospace">${v >= 0 ? '+' : ''}${bw < 30 ? (+v).toFixed(1) : v}</text>`;
    });
    // 시장 평균 기준선
    const b = ant.baseline['r' + h];
    g += `<line x1="${(cx - gw / 2 + 10).toFixed(1)}" y1="${Y(b).toFixed(1)}"
           x2="${(cx + gw / 2 - 10).toFixed(1)}" y2="${Y(b).toFixed(1)}"
           stroke="#ffffff" stroke-opacity=".55" stroke-width="1.6" stroke-dasharray="5 3"/>`;
    g += `<text x="${cx.toFixed(1)}" y="${H - 10}" text-anchor="middle" fill="#8b96ad" font-size="12"
           font-weight="600">${h}거래일 뒤</text>`;
  });

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;

  $('#timing-legend').innerHTML =
    ACTOR_KEYS.map(k => `<span><i style="background:${ACTORS[k].raw}"></i>${ACTORS[k].name}이 크게 산 날</span>`).join('') +
    '<span><i class="lineish" style="background:#ffffff8c"></i>시장 평균</span>';

  renderYearly();
}

/** 연도별 분해 — 학점이 장세 탓인지 볼 수 있게 한다. 결론 문장도 표의 숫자에서 만든다. */
function renderYearly() {
  const box = $('#yearly');
  const rows = reportAnt().yearly || [];
  if (rows.length < 2) { box.innerHTML = ''; return; }

  const gc = { A: '#4dd4ac', B: '#4dd4ac', C: '#7a869e', D: '#ffb02e', F: '#ff4d4d' };
  const worst = rows.reduce((a, b) => b.excess < a.excess ? b : a);
  const best = rows.reduce((a, b) => b.excess > a.excess ? b : a);
  const corrs = rows.map(y => y.corrIF);
  const hasCI = rows.some(y => y.excessCI);
  const sig = rows.filter(y => y.significant);
  box.innerHTML = `
    <table>
      <thead><tr>
        <th>연도</th><th>개미↔외인 상관</th><th>개미가 크게 산 날 +20일</th>
        <th>시장 평균</th><th>격차</th><th>학점</th>
      </tr></thead>
      <tbody>${rows.map(y => `
        <tr>
          <td>${y.year}</td>
          <td>${y.corrIF.toFixed(3)}</td>
          <td class="${dirCls(y.indivHeavyR20)}">${sgn(y.indivHeavyR20)}%</td>
          <td class="dim">${sgn(y.baseline20)}%</td>
          <td class="${dirCls(y.excess)}" title="${ciTxt(y.excessCI)}">${sgn(y.excess)}%p</td>
          <td class="yg" style="color:${gc[y.grade] || '#7a869e'}">${y.grade}</td>
        </tr>`).join('')}
      </tbody>
    </table>
    <p class="yearly-note">
      연도별로 쪼개 보면 개미의 성적은 해마다 다릅니다.
      격차가 가장 나빴던 해는 <b>${worst.year}년</b>(${sgn(worst.excess)}%p, 그해 시장 평균 ${sgn(worst.baseline20)}%),
      가장 좋았던 해는 <b>${best.year}년</b>(${sgn(best.excess)}%p, 그해 시장 평균 ${sgn(best.baseline20)}%)입니다.
      ${hasCI ? (sig.length
        ? `시장 평균과 통계적으로 구분되는 해는 ${sig.map(y => y.year).join('·')}년입니다.`
        : '다만 어느 해도 시장 평균과 통계적으로 구분되지는 않습니다.') : ''}
      반대로 가는 습관(상관계수 ${Math.min(...corrs).toFixed(2)} ~ ${Math.max(...corrs).toFixed(2)})은 해마다 비슷합니다.
    </p>`;
}

function renderOppose() {
  const box = $('#oppose');
  if (!hasAnt()) { box.innerHTML = '<p class="dim">데이터 없음</p>'; return; }
  const ant = D.ant;

  const rows = [
    { label: '개미 ↔ 외국인 상관계수', v: ant.correlation.individual_foreign, kind: 'corr',
      note: '−1 에 가까울수록 완벽히 반대로 움직였다는 뜻' },
    { label: '개미 ↔ 기관 상관계수', v: ant.correlation.individual_institution, kind: 'corr',
      note: '기관은 개미와 외국인 사이 어딘가에 있습니다' },
    { label: '외국인과 정반대였던 날', v: ant.oppositeRate.vsForeign, kind: 'pct',
      note: `${ant.sample.days}거래일 기준` },
    { label: '기관과 정반대였던 날', v: ant.oppositeRate.vsInstitution, kind: 'pct', note: '' },
  ];

  box.innerHTML = '';
  rows.forEach((r, i) => {
    const w = r.kind === 'corr' ? Math.abs(r.v) * 100 : r.v;
    const color = r.kind === 'corr'
      ? (r.v < 0 ? '#4d94ff' : '#ff4d4d')
      : '#ffb02e';
    const node = el('div', 'opp-item', `
      <div class="opp-head"><span>${r.label}</span>
        <b style="color:${color}">${r.kind === 'corr' ? r.v.toFixed(3) : r.v + '%'}</b></div>
      <div class="opp-track"><div class="opp-fill" style="background:${color}"></div></div>
      ${r.note ? `<div class="opp-note">${r.note}</div>` : ''}`);
    box.appendChild(node);
    growIn(() => { $('.opp-fill', node).style.width = `${w}%`; }, 80 + i * 70);
  });
}

function renderHall() {
  const box = $('#hall'), ant = D.ant;
  if (!hasAnt() || !ant.hallOfFame?.length) {
    box.innerHTML = '<p class="dim">데이터 없음</p>';
    $('#hall-verdict').hidden = true;
    return;
  }
  const rows = ant.hallOfFame;
  const max = Math.max(...rows.map(r => Math.abs(r.return20)), 1);
  setText('#hall-sub', rows[0].intensity != null
    ? '코스피 · 거래대금 대비 순매수 기준 · 서로 다른 국면 · 20거래일 뒤 벌어진 일'
    : '코스피 · 순매수 금액 기준 · 그리고 20거래일 뒤 벌어진 일');

  box.innerHTML = '';
  rows.forEach((r, i) => {
    const bad = r.return20 < 0;
    const half = Math.abs(r.return20) / max * 50;
    const node = el('div', `hall-row ${bad ? 'bad' : 'good'}`, `
      <div class="hall-date">${r.date}</div>
      <div class="hall-bar-cell">
        <div class="hall-bar" style="background:${bad ? '#4d94ff' : '#ff4d4d'};
             ${bad ? `left:${50 - half}%` : 'left:50%'}"></div>
      </div>
      <div class="hall-amt">개미 ${eok(r.amount)} 매수${r.intensity != null ? ` · 거래대금의 ${r.intensity.toFixed(1)}%` : ''}${r.dayChange != null ? ` · 그날 코스피 <b class="${dirCls(r.dayChange)}">${sgn(r.dayChange)}%</b>` : ''}</div>
      <div class="hall-ret ${dirCls(r.return20)}">${sgn(r.return20)}%</div>`);
    box.appendChild(node);
    growIn(() => { $('.hall-bar', node).style.width = `${Math.max(half, 0.6)}%`; }, 70 + i * 65);
  });

  // 오르기만 해도 '맞았다'고 세면 상승장에서는 누구나 맞는다 — 시장 평균과 견준다
  const n = rows.length;
  const base = ant.baseline.r20;
  const up = rows.filter(r => r.return20 > 0).length;
  const beat = rows.filter(r => r.return20 > base).length;
  const avg = rows.reduce((s, r) => s + r.return20, 0) / n;
  const apart = rows[0].intensity !== undefined ? '(서로 다른 국면)' : '';
  const punch = beat <= 1 ? '크게 지른 날의 뒤끝은 대체로 평균만 못했습니다.'
              : beat >= n - 1 ? '"개미는 항상 틀린다"는 말이 항상 맞지는 않습니다.'
              : '맞은 날도, 틀린 날도 있었습니다.';
  $('#hall-verdict').hidden = false;
  $('#hall-verdict').innerHTML =
    `개미가 가장 강하게 질렀던 ${n}번${apart}, 20거래일 뒤 지수가 오른 건 ${up}번,
     시장 평균(${sgn(base)}%)보다 좋았던 건 <b>${beat}번</b>입니다. 평균 ${sgn(avg)}%.
     ${punch} 다만 ${n}번은 통계가 아니라 일화이고,${rows.some(r => r.dayChange != null && r.dayChange < -2) ? ' 대부분 지수가 크게 빠진 날이라 그 뒤의 반등이 섞여 있습니다.' : ''}
     성적표처럼 '크게 산 날' 전체로 보면 시장 평균과 구분되지 않습니다.`;
}

function renderCaveats() {
  const box = $('#caveats');
  const list = (D.ant && D.ant.caveats) || [];
  box.innerHTML = list.length
    ? list.map(c => `<li>${esc(c)}</li>`).join('')
    : '<li class="dim">표시할 내용이 없습니다.</li>';
}

/* ── 인사이트 ─────────────────────────────────────────── */

function renderInsights() {
  const box = $('#insights');
  const icons = { buy: '📈', sell: '📉', neutral: '🔎' };
  box.innerHTML = '';
  (D.insights.items || []).forEach((t, i) => {
    const n = el('div', `tip ${t.tone}`,
      `<span class="tip-icon" aria-hidden="true">${icons[t.tone] || '🔎'}</span><span>${esc(t.text)}</span>`);
    n.style.animationDelay = `${i * 55}ms`;
    box.appendChild(n);
  });
  if (!box.children.length) box.innerHTML = '<p class="dim">표시할 브리핑이 없습니다.</p>';
}

/* ── 작은 추이선 (바깥 지표·종목 상세) ───────────────────── */

function sparkSVG(data, cls, dir) {
  if (!data || data.length < 2) return `<svg class="${cls}"></svg>`;
  const w = 150, h = 54, p = 3;
  const mn = Math.min(...data), mx = Math.max(...data), rg = (mx - mn) || 1;
  const pts = data.map((v, i) => [
    p + i / (data.length - 1) * (w - 2 * p),
    h - p - (v - mn) / rg * (h - 2 * p),
  ]);
  const color = dir == null ? '#7a869e' : dir >= 0 ? '#ff4d4d' : '#4d94ff';
  const line = pts.map(pt => pt.map(n => n.toFixed(1)).join(',')).join(' ');
  const area = `${p},${h - p} ${line} ${w - p},${h - p}`;
  const uid = 'g' + Math.random().toString(36).slice(2, 8);
  return `<svg class="${cls}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="${uid}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="${color}" stop-opacity=".35"/>
      <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
    </linearGradient></defs>
    <polygon points="${area}" fill="url(#${uid})"/>
    <polyline points="${line}" fill="none" stroke="${color}" stroke-width="1.8"
              stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}

/* ── 수급 흐름 차트 ───────────────────────────────────── */

function renderFlowChart() {
  const mk = D.flows.markets[heroMarket];
  const svg = $('#flow-chart');
  if (!mk || !mk.daily?.length) { svg.innerHTML = ''; return; }

  const rows = mk.daily.slice(-flowRange);
  const W = chartW(svg), narrow = W < 520;
  const H = narrow ? 260 : 320, M = { t: 14, r: narrow ? 46 : 58, b: 26, l: narrow ? 50 : 62 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;

  // 지수 종가를 날짜로 맞춰 붙인다
  const closeBy = {};
  (D.market.indices?.[heroMarket]?.history || []).forEach(h => { closeBy[h.date] = h.close; });
  const closes = rows.map(r => closeBy[r.date] ?? null);

  // 누적은 보이는 구간의 시작점을 0 으로 다시 잡는다
  const keys = FLOW_KEYS.filter(k => rows.some(r => r[k] != null));
  const series = keys.map(k => {
    if (flowMode !== 'cum') return { key: k, vals: rows.map(r => r[k] ?? 0) };
    let acc = 0;
    return { key: k, vals: rows.map(r => (acc += r[k] ?? 0)) };
  });

  const all = series.flatMap(s => s.vals).filter(v => v != null);
  let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  const padv = (hi - lo) * 0.08 || 1;
  lo -= padv; hi += padv;

  const X  = i => M.l + (rows.length === 1 ? pw / 2 : i / (rows.length - 1) * pw);
  const Y  = v => M.t + ph - (v - lo) / (hi - lo) * ph;
  const bw = pw / rows.length;

  const cv = closes.filter(v => v != null);
  const cLo = cv.length ? Math.min(...cv) : 0, cHi = cv.length ? Math.max(...cv) : 1;
  const CY = v => M.t + ph - (v - cLo) / ((cHi - cLo) || 1) * ph;

  let g = '';

  // 가로 눈금
  const ticks = narrow ? 4 : 5;
  for (let i = 0; i <= ticks; i++) {
    const v = lo + (hi - lo) * i / ticks, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}"
           stroke="#232b40" stroke-width="1"/>`;
    g += `<text x="${M.l - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end"
           fill="#7f89a2" font-size="${narrow ? 10 : 11}" font-family="ui-monospace,monospace">${eok(v, { sign: false })}</text>`;
  }
  g += `<line x1="${M.l}" y1="${Y(0).toFixed(1)}" x2="${W - M.r}" y2="${Y(0).toFixed(1)}"
         stroke="#4a5570" stroke-width="1.4"/>`;

  // 지수 라인 (보조축)
  if (cv.length > 1) {
    const pts = closes.map((c, i) => c == null ? null : `${X(i).toFixed(1)},${CY(c).toFixed(1)}`)
                      .filter(Boolean).join(' ');
    g += `<polyline points="${pts}" fill="none" stroke="#ffffff" stroke-opacity=".38"
           stroke-width="1.6" stroke-dasharray="4 3"/>`;
    [cHi, cLo].forEach(v => {
      g += `<text x="${W - M.r + 4}" y="${(CY(v) + 4).toFixed(1)}" fill="#7f89a2" font-size="10"
             font-family="ui-monospace,monospace">${nfmt(v, 0)}</text>`;
    });
  }

  // 본체
  if (flowMode === 'cum') {
    series.forEach(s => {
      const pts = s.vals.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
      g += `<polyline points="${pts}" fill="none" stroke="${ACTORS[s.key].raw}"
             stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>`;
      const li = s.vals.length - 1;
      g += `<circle cx="${X(li).toFixed(1)}" cy="${Y(s.vals[li]).toFixed(1)}" r="3.6"
             fill="${ACTORS[s.key].raw}"/>`;
    });
  } else {
    const nk = keys.length;
    const sub = Math.max(1.2, bw / nk - 1.2);
    rows.forEach((r, i) => {
      keys.forEach((k, j) => {
        const v = r[k] ?? 0;
        const x = X(i) - bw / 2 + j * (bw / nk) + (bw / nk - sub) / 2;
        const y0 = Y(0), y1 = Y(v);
        g += `<rect x="${x.toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}"
               width="${sub.toFixed(1)}" height="${Math.max(Math.abs(y1 - y0), 0.8).toFixed(1)}"
               fill="${ACTORS[k].raw}" opacity=".88" rx="1"/>`;
      });
    });
  }

  // 날짜 축 — 최신 날짜부터 거꾸로 간격을 둬서 마지막 날은 늘 보이게
  const step = labelStep(rows.length, pw, 56);
  rows.forEach((r, i) => {
    if ((rows.length - 1 - i) % step) return;
    g += `<text x="${X(i).toFixed(1)}" y="${H - 8}" text-anchor="middle"
           fill="#7f89a2" font-size="10.5" font-family="ui-monospace,monospace">${mdy(r.date)}</text>`;
  });

  // 호버 레이어
  g += `<rect id="flow-hit" x="${M.l}" y="${M.t}" width="${pw}" height="${ph}" fill="transparent"/>`;
  g += `<line id="flow-cursor" y1="${M.t}" y2="${M.t + ph}" stroke="#ffffff" stroke-opacity=".22"
         stroke-width="1" visibility="hidden"/>`;

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;

  $('#flow-legend').innerHTML =
    keys.map(k => `<span><i style="background:${ACTORS[k].raw}"></i>${ACTORS[k].name}</span>`).join('') +
    `<span><i class="lineish" style="background:#ffffff66"></i>${heroMarket === 'KOSPI' ? '코스피' : '코스닥'} 지수</span>`;

  $('#flow-note').textContent = flowMode === 'cum'
    ? `누적: ${rows.length}거래일 전을 0으로 두고 매일의 순매수를 더해간 값입니다. ` +
      '선이 우하향이면 그 주체가 그동안 계속 팔았다는 뜻입니다.'
    : `일별: ${rows.length}거래일간 하루하루의 순매수(+) / 순매도(−) 금액입니다.`;

  wireFlowHover(svg, rows, series, X, W, M, pw);
}

function wireFlowHover(svg, rows, series, X, W, M, pw) {
  const hit = $('#flow-hit', svg), cursor = $('#flow-cursor', svg), tip = $('#tooltip');
  if (!hit) return;

  const move = ev => {
    const box = svg.getBoundingClientRect();
    const px = (ev.clientX - box.left) / box.width * W;
    let i = Math.round((px - M.l) / pw * (rows.length - 1));
    i = Math.max(0, Math.min(rows.length - 1, i));
    const r = rows[i];

    cursor.setAttribute('x1', X(i)); cursor.setAttribute('x2', X(i));
    cursor.setAttribute('visibility', 'visible');

    tip.hidden = false;
    tip.innerHTML = `<div class="t-date">${r.date} · ${flowMode === 'cum' ? '누적' : '일별'}</div>` +
      series.map(s => `<div class="t-row">
          <span><i style="background:${ACTORS[s.key].raw}"></i>${ACTORS[s.key].name}</span>
          <b class="${dirCls(s.vals[i])}">${eok(s.vals[i])}</b></div>`).join('');

    const tw = tip.offsetWidth, th = tip.offsetHeight;
    tip.style.left = `${Math.min(ev.clientX + 14, window.innerWidth - tw - 8)}px`;
    tip.style.top  = `${Math.max(8, ev.clientY - th - 12)}px`;
  };

  hit.addEventListener('pointermove', move);
  hit.addEventListener('pointerdown', move);
  hit.addEventListener('pointerleave', ev => { if (ev.pointerType === 'mouse') hideChartTips(); });
}

/* ── 연속 매매 ────────────────────────────────────────── */

function renderStreaks() {
  const mk = D.flows.markets[heroMarket];
  const box = $('#streaks');
  box.innerHTML = '';
  setText('#streak-baseline', D.flows?.streakBaseline || '');
  if (!mk || !mk.daily) return;

  const recent = mk.daily.slice(-10);
  FLOW_KEYS.forEach(k => {
    const s = mk.streaks?.[k] || streakOf(mk.daily, k), a = ACTORS[k];
    if (!s) return;
    const buying = s.side === 'buy';
    const label = s.days === 0 ? '보합' : `${s.days}일 연속 ${buying ? '순매수' : '순매도'}`;
    const dots = recent.map(r => {
      const v = r[k] ?? 0;
      const c = v > 0 ? '#ff4d4d' : v < 0 ? '#4d94ff' : '#252d42';
      return `<i style="background:${c}"></i>`;
    }).join('');
    // 이 길이가 2009년 이후 얼마나 드문가 — 빈도일 뿐 방향을 말하지 않는다
    const r = s.rarity;
    const since = r?.since ? `${r.since.slice(0, 4)}년 이후` : '';
    const lg = r?.longest?.[s.side];
    const longest = lg?.days ? ` · 최장 ${lg.days}일${lg.ended ? `(${lg.ended.slice(0, 7)})` : ''}` : '';
    const rare = r && r.pctRuns != null && s.days > 0
      ? `<div class="streak-rare">${since} 같은 방향 연속 구간 중 ${pctTxt(r.pctRuns)}${r.pctRuns < 50 ? '만' : '가'} 이 길이까지${longest}</div>`
      : longest && s.days > 0 ? `<div class="streak-rare">${since} ${longest.slice(3)}</div>` : '';

    box.appendChild(el('div', 'streak', `
      <div class="streak-face" aria-hidden="true">${a.face}</div>
      <div class="streak-body">
        <div class="streak-title">${a.name} · <span class="${s.days ? (buying ? 'up' : 'down') : 'flat'}">${label}</span></div>
        <div class="streak-sub">기간 누적 ${eok(s.total)}</div>
        ${rare}
        <div class="dots" title="최근 10거래일 (빨강=순매수, 파랑=순매도)">${dots}</div>
      </div>
      <div class="streak-count ${s.days ? (buying ? 'up' : 'down') : 'flat'}">${s.days}<small>일</small></div>`));
  });
}

/** 수집기가 연속 기록을 안 준 주체(예전 데이터의 기타법인)는 화면에서 센다 */
function streakOf(rows, k) {
  if (!rows?.length || rows[rows.length - 1][k] == null) return null;
  const last = rows[rows.length - 1][k];
  if (!last) return { days: 0, side: 'flat', total: 0 };
  let days = 0, total = 0;
  for (let i = rows.length - 1; i >= 0; i--) {
    const v = rows[i][k];
    if (v == null || v === 0 || (v > 0) !== (last > 0)) break;
    days++; total += v;
  }
  return { days, side: last > 0 ? 'buy' : 'sell', total };
}

/* ── 기관 분해 ────────────────────────────────────────── */

function renderInstBreakdown() {
  const mk = D.flows.markets[heroMarket];
  const box = $('#inst-breakdown');
  box.innerHTML = '';
  if (!mk) return;

  const last = mk.latest;
  const parts = INST_PARTS.map(([k, n]) => ({ k, n, v: last[k] ?? 0 }))
                          .sort((a, b) => Math.abs(b.v) - Math.abs(a.v));
  const max = Math.max(...parts.map(p => Math.abs(p.v)), 1);

  parts.forEach((p, i) => {
    const half = Math.abs(p.v) / max * 50;
    const row = el('div', 'inst-row', `
      <div class="inst-name">${p.n}</div>
      <div class="inst-track">
        <div class="inst-bar" style="background:${p.v >= 0 ? '#ff4d4d' : '#4d94ff'};
             ${p.v >= 0 ? 'left:50%;' : `left:${50 - half}%;`} width:0%"></div>
      </div>
      <div class="inst-val ${dirCls(p.v)}">${eok(p.v)}</div>`);
    box.appendChild(row);
    growIn(() => { $('.inst-bar', row).style.width = `${Math.max(half, 0.4)}%`; }, 60 + i * 55);
  });

  box.appendChild(el('p', 'dim small',
    '기관은 하나가 아닙니다. 증권사 자기매매(금융투자), 펀드(투신), 국민연금 등(연기금)은 ' +
    '성격도 목적도 달라 방향이 서로 엇갈리는 일이 흔합니다.'));
}

/* ── 종목 트리맵 ──────────────────────────────────────── */

function heatColor(rate) {
  if (rate == null) return 'rgb(42,49,69)';
  const t = Math.min(1, Math.abs(rate) / 6);
  const base = [42, 49, 69];
  const tgt = rate >= 0 ? [255, 77, 77] : [77, 148, 255];
  return `rgb(${base.map((b, i) => Math.round(b + (tgt[i] - b) * (0.16 + 0.84 * t))).join(',')})`;
}

/** 면적 비례 이진 분할 트리맵 (내림차순 정렬 입력 가정) */
function partition(items, x, y, w, h, out) {
  if (!items.length || w <= 0 || h <= 0) return;
  if (items.length === 1) { out.push({ ...items[0], x, y, w, h }); return; }
  const total = items.reduce((s, d) => s + d.area, 0);
  let acc = 0, i = 0;
  for (; i < items.length - 1; i++) {
    if (acc + items[i].area > total / 2) break;
    acc += items[i].area;
  }
  const a = items.slice(0, i + 1), b = items.slice(i + 1);
  const ratio = a.reduce((s, d) => s + d.area, 0) / total;
  if (w >= h) {
    partition(a, x, y, w * ratio, h, out);
    partition(b, x + w * ratio, y, w * (1 - ratio), h, out);
  } else {
    partition(a, x, y, w, h * ratio, out);
    partition(b, x, y + h * ratio, w, h * (1 - ratio), out);
  }
}

function renderTreemap() {
  const box = $('#treemap');
  const items = (D.stocks.top || [])
    .filter(s => s.marketCap > 0 && (s.market || 'KOSPI') === treeMarket)
    .map(s => ({ ...s, area: s.marketCap }))
    .sort((a, b) => b.area - a.area)
    .slice(0, 24);
  if (!items.length) { box.innerHTML = '<p class="dim">종목 데이터가 없습니다.</p>'; return; }

  const draw = () => {
    const W = box.clientWidth || 900, H = box.clientHeight || 420;
    // 좁은 화면에서 24칸을 다 그리면 전부 글자도 안 보이는 조각이 된다
    const limit = W < 460 ? 10 : W < 700 ? 16 : items.length;
    const shown = items.slice(0, limit);
    const total = shown.reduce((s, d) => s + d.area, 0);
    const scaled = shown.map(d => ({ ...d, area: d.area / total * W * H }));
    const out = [];
    partition(scaled, 0, 0, W, H, out);

    const searchable = new Set([...(D.stocks.top || []), ...(D.stocks.universe || [])].map(s => s.code)).size;
    const cap = $('#treemap-caption');
    if (cap) cap.textContent =
      `${mkName(treeMarket)} 시총 상위 ${Math.min(limit, items.length)}종목 표시` +
      ` · 검색하면 수집된 ${searchable}종목(코스피·코스닥)에서 찾습니다`;

    box.innerHTML = '';
    out.forEach(t => {
      const size = (t.w < 74 || t.h < 40) ? ' tiny' : (t.w < 112 ? ' narrow' : '');
      const tile = el('button', 'tile' + size + (selectedStock === t.code ? ' sel' : ''));
      Object.assign(tile.style, {
        left: `${t.x}px`, top: `${t.y}px`,
        width: `${Math.max(t.w - 3, 1)}px`, height: `${Math.max(t.h - 3, 1)}px`,
        background: heatColor(t.changeRate),
        color: '#fff', font: 'inherit', textAlign: 'left',
      });
      tile.innerHTML = `<div class="tile-name">${esc(t.name)}</div>
                        <div class="tile-chg">${pct(t.changeRate)}</div>`;
      tile.title = `${t.name}  ${nfmt(t.price, 0)}원  ${pct(t.changeRate)}\n시총 ${t.marketCapText || ''}`;
      tile.dataset.code = t.code;
      tile.addEventListener('click', () => showStock(t.code));
      box.appendChild(tile);
    });
  };

  draw();
  renderTreemap._draw = draw;

  // 순매수가 몇 종목에 몰렸나 — 지수 전체와 나머지 종목을 따로 읽을 수 있게(사실만)
  const cc = D.stocks.concentration, cp = $('#concentration');
  const part = k => {
    const x = cc?.[k];
    if (!x || x.share == null || !x.top?.length) return '';
    return `${ACTORS[k].name} ${eok(x.total)} 중 ${x.top.map(t => esc(t.name)).join('·')} ${(+x.share).toFixed(0)}%`;
  };
  const parts = ['foreign', 'institution'].map(part).filter(Boolean);
  cp.hidden = !parts.length;
  if (parts.length) {
    cp.innerHTML = `최근 ${cc.days || 60}거래일 순매수 쏠림(수집 ${cc.universe ?? '—'}종목, 금액은 근사): ${parts.join(' · ')}` +
      (['foreign', 'institution'].some(k => cc[k]?.share > 100) ? ' — 100%를 넘으면 나머지 종목은 합쳐서 반대 방향입니다.' : '');
  }
  clearTimeout(renderTreemap._t);
  if (!renderTreemap._bound) {
    renderTreemap._bound = true;
    window.addEventListener('resize', () => {
      clearTimeout(renderTreemap._t);
      renderTreemap._t = setTimeout(() => renderTreemap._draw(), 180);
    });
  }
}

/** 종목 정보: 시총 상위(top, 트리맵) → 수집 범위(universe) 순서로 찾아 한 모양으로 묶는다 */
function stockInfo(code) {
  const t = (D.stocks.top || []).find(x => x.code === code);
  const u = (D.stocks.universe || []).find(x => x.code === code);
  if (!t && !u) return null;
  return {
    code, name: t?.name ?? u.name, market: t?.market || u?.market || 'KOSPI',
    price: t?.price ?? u?.price, changeRate: t?.changeRate ?? u?.chg,
    marketCapText: t?.marketCapText, foreignHoldRatio: t?.foreignHoldRatio ?? u?.holdRatio,
    flow: t?.flow, stat60: t?.stat60, uni: u || null, top: t || null,
  };
}

/** 한 주체의 그 종목 연속 매매. universe 값이 있으면 그것, 없으면 최근 며칠 흐름에서 센다 */
function stockStreak(info, k) {
  const s = info?.uni?.[k]?.streak;
  if (s) return s.days ? { days: s.days, side: s.side } : null;
  const f = info?.flow || [];
  const st = f.length ? streakOf(f, k) : null;
  if (!st || !st.days) return null;
  return { days: st.days, side: st.side, atLeast: st.days === f.length };
}
const streakTxt = s => s ? `${s.days}일${s.atLeast ? '+' : ''} 연속 ${s.side === 'buy' ? '순매수' : '순매도'}` : '연속 없음';

/** 다른 곳(순위·관심 종목·검색)에서 종목을 열 때. 상세를 못 보여 주면 false */
function openStock(code) {
  const info = stockInfo(code);
  if (!info) return false;
  if (info.market !== treeMarket) {
    treeMarket = info.market;
    $$('#tree-market-seg button').forEach(x => x.classList.toggle('on', x.dataset.tmarket === treeMarket));
    syncPressed();
    safe(renderTreemap, '트리맵');
  }
  showStock(code, { far: true });
  return true;
}

function showStock(code, { scroll = true, far = false } = {}) {
  const s = stockInfo(code);
  const box = $('#stock-detail');
  if (!s) { box.hidden = true; selectedStock = null; STOCK_CHART = null; return; }
  selectedStock = code;
  $$('.tile').forEach(t => t.classList.remove('sel'));

  const flow = s.flow || [];
  const max = Math.max(...flow.flatMap(d => ACTOR_KEYS.map(k => Math.abs(d[k] ?? 0))), 1);
  const u = s.uni;
  const hold = s.foreignHoldRatio != null
    ? ` · 외국인 보유 ${s.foreignHoldRatio}%${u?.holdChg20 != null ? ` (20일 ${u.holdChg20 >= 0 ? '+' : ''}${(+u.holdChg20).toFixed(2)}%p)` : ''}` : '';

  // 수집 범위(universe)의 종목별 수급 요약: 연속 · 5일 · 20일(주) · 20일 금액(근사)
  const UK = ['foreign', 'institution', 'individual'];
  const longest = u ? UK.filter(k => u[k]?.longest)
    .map(k => `${ACTORS[k].name} 순매수 ${u[k].longest.buy ?? '—'}일·순매도 ${u[k].longest.sell ?? '—'}일`).join(' · ') : '';
  const facts = u ? `
    <div class="sd-facts-wrap">
      <table class="sd-facts">
        <thead><tr><th>주체</th><th>연속</th><th>5일</th><th>20일</th></tr></thead>
        <tbody>${UK.map(k => {
          const x = u[k] || {};
          const st = x.streak?.days ? x.streak : null;
          return `<tr>
            <th scope="row">${ACTORS[k].name}</th>
            <td class="${st ? (st.side === 'buy' ? 'up' : 'down') : 'dim'}">${st ? `${st.days}일 ${st.side === 'buy' ? '순매수' : '순매도'}` : '—'}</td>
            <td class="${dirCls(x.d5)}">${shares(x.d5)}</td>
            <td class="${dirCls(x.d20)}">${shares(x.d20)}${x.v20 != null ? `<small>≈${eok(x.v20)}</small>` : ''}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
      <p class="dim small">${u.asOf ? `${dayLabel(u.asOf)}까지 · ` : ''}수량은 주, 금액은 수량 × 종가로 어림한 값입니다.${
        longest ? `<br>수집 기간 중 가장 길었던 연속: ${longest}` : ''}</p>
    </div>` : '';

  box.hidden = false;
  box.innerHTML = `
    <div class="sd-head">
      <span class="sd-name">${esc(s.name)}</span>
      ${starBtn(code, s.name)}
      <span class="sd-price ${dirCls(s.changeRate)}">${s.price != null ? `${nfmt(s.price, 0)}원 ` : ''}${pct(s.changeRate)}</span>
      <span class="sd-meta">${mkName(s.market)}${s.marketCapText ? ` · 시총 ${esc(s.marketCapText)}` : ''}${hold}</span>
    </div>
    ${facts}
    ${flow.length ? `
    <div class="sd-flow">${flow.map(d => `
        <div class="sd-day">
          <span class="sd-date">${d.date ? `${d.date.slice(4, 6)}/${d.date.slice(6, 8)}` : ''}</span>
          <div class="sd-bars">${ACTOR_KEYS.map(k => {
            const v = d[k] ?? 0;
            return `<div class="sd-seg" title="${ACTORS[k].name} ${shares(v)}"
                      style="background:${ACTORS[k].raw};opacity:${v >= 0 ? 1 : .42};
                             width:${(Math.abs(v) / max * 100).toFixed(1)}%"></div>`;
          }).join('')}</div>
        </div>`).join('')}
    </div>
    <div class="sd-legend">${
      ACTOR_KEYS.map(k => `<span><i style="background:${ACTORS[k].raw}"></i>${ACTORS[k].name}</span>`).join('')
    }<span class="dim">진한 색 = 순매수, 흐린 색 = 순매도 · 막대 길이 = 수량</span></div>` : ''}
    <div class="sd-60" id="sd-60"><p class="dim small">종목 수급 추이를 불러오는 중…</p></div>`;
  renderStockChart($('#sd-60', box), code, s);

  $$('.tile').forEach(t => t.classList.toggle('sel', t.dataset.code === code));
  if (scroll) box.scrollIntoView({ behavior: 'smooth', block: far ? 'start' : 'nearest' });
}

/* ── 종목 수급 추이 (상세를 처음 열 때만 받는다) ─────────
   1년치(stockseries/{code}.json)가 있으면 그것, 없으면 60일치(stockflows.json) */

let STOCKFLOWS = null;
function loadStockFlows() {
  if (!STOCKFLOWS) {
    STOCKFLOWS = fetchJSON('stockflows', { timeout: 8000, tries: 2 })
      .catch(e => { STOCKFLOWS = null; throw e; });
  }
  return STOCKFLOWS;
}

/** 종목별 1년 수급. 없는 종목(404)은 null 로 기억하고, 그 밖의 실패는 다음에 다시 묻는다 */
const SERIES = new Map();
function loadSeries(code) {
  if (!/^[0-9A-Za-z]{6}$/.test(code)) return Promise.resolve(null);
  if (!SERIES.has(code)) {
    SERIES.set(code, fetchJSON(`stockseries/${code}`, { timeout: 8000, tries: 1 }).catch(e => {
      if (!/HTTP 404/.test(e.message)) SERIES.delete(code);
      return null;
    }));
  }
  return SERIES.get(code);
}

/** 화면 폭이 바뀌면 열려 있는 종목 차트를 다시 그리는 함수 */
let STOCK_CHART = null;

/** 배열 길이가 서로 달라도 최신(끝) 쪽으로 맞춘다 */
function alignSeries(x) {
  if (!x || !Array.isArray(x.d)) return null;
  const keys = ['d', 'i', 'f', 'o', 'c'];
  if (keys.some(k => !Array.isArray(x[k]))) return null;
  const n = Math.min(...keys.map(k => x[k].length));
  if (n < 2) return null;
  const out = {};
  keys.forEach(k => { out[k] = x[k].slice(-n); });
  out.h = Array.isArray(x.h) ? x.h.slice(-n) : null;
  return out;
}

/** 축 눈금용 짧은 주식 수: '-9,777만' · '1.2억' (단위 '주'는 제목에) */
function sharesAxis(v) {
  const s = v < 0 ? '-' : '', a = Math.abs(v);
  if (a >= 1e8) return `${s}${(a / 1e8).toFixed(1)}억`;
  if (a >= 1e4) return `${s}${Math.round(a / 1e4).toLocaleString()}만`;
  return `${s}${Math.round(a).toLocaleString()}`;
}

/** 주체별 누적 순매수(주) + 종가(점선). 좌축 = 누적 주식 수, 우축 = 종가 */
function stockChartSVG(svg, x, name) {
  const n = x.d.length;
  const W = chartW(svg), narrow = W < 520;
  const H = narrow ? 210 : 220, M = { t: 12, r: narrow ? 54 : 60, b: 24, l: narrow ? 52 : 60 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const cum = k => { let acc = 0; return x[k].map(v => (acc += v || 0)); };
  const lines = [['individual', cum('i')], ['foreign', cum('f')], ['institution', cum('o')]];
  const vals = lines.flatMap(([, v]) => v);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const pad = (hi - lo) * 0.1 || 1; lo -= pad; hi += pad;
  const cs = x.c.filter(v => v != null);
  const cLo = cs.length ? Math.min(...cs) : 0, cHi = cs.length ? Math.max(...cs) : 1;
  const X = i => M.l + i / (n - 1) * pw;
  const Y = v => M.t + ph - (v - lo) / (hi - lo) * ph;
  const CY = v => M.t + ph - (v - cLo) / ((cHi - cLo) || 1) * ph;
  let g = '';
  for (let i = 0; i <= 3; i++) {
    const v = lo + (hi - lo) * i / 3, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}" stroke="#232b40"/>`;
    g += `<text x="${M.l - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#7f89a2"
           font-size="10.5" font-family="ui-monospace,monospace">${sharesAxis(v)}</text>`;
    if (cs.length) {
      const cv = cLo + (cHi - cLo) * i / 3;
      g += `<text x="${W - M.r + 5}" y="${(CY(cv) + 4).toFixed(1)}" fill="#7f89a2"
             font-size="10.5" font-family="ui-monospace,monospace">${nfmt(cv, 0)}</text>`;
    }
  }
  g += `<line x1="${M.l}" y1="${Y(0).toFixed(1)}" x2="${W - M.r}" y2="${Y(0).toFixed(1)}" stroke="#4a5570" stroke-width="1.2"/>`;
  if (cs.length > 1) {
    const price = x.c.map((v, i) => v == null ? null : `${X(i).toFixed(1)},${CY(v).toFixed(1)}`).filter(Boolean).join(' ');
    g += `<polyline points="${price}" fill="none" stroke="#ffffff" stroke-opacity=".35" stroke-width="1.4" stroke-dasharray="4 3"/>`;
  }
  lines.forEach(([k, v]) => {
    g += `<polyline points="${v.map((y, i) => `${X(i).toFixed(1)},${Y(y).toFixed(1)}`).join(' ')}" fill="none"
           stroke="${ACTORS[k].raw}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
  });
  const step = labelStep(n, pw, 56);
  for (let i = n - 1; i >= 0; i -= step) {
    const d = x.d[i];
    g += `<text x="${Math.max(M.l + 14, Math.min(X(i), W - M.r)).toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="#7f89a2"
           font-size="10.5" font-family="ui-monospace,monospace">${+d.slice(4, 6)}/${+d.slice(6, 8)}</text>`;
  }
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('aria-label', `${name} 최근 ${n}거래일 개인·외국인·기관 누적 순매수와 종가`);
  svg.innerHTML = g;
}

/** 1년치가 있으면 1년, 없으면 60일 차트. 아래에 60일 '산 뒤 등락' 요약 */
function renderStockChart(box, code, s) {
  STOCK_CHART = null;
  const st = s.stat60;
  const sinceRow = (label, v, m) => v == null ? '' :
    `<span><b>${label}</b> <b class="${dirCls(v)}">${sgn(v)}%</b>${m != null ? ` <span class="dim">(같은 날 지수였다면 ${sgn(m)}%)</span>` : ''}</span>`;
  const summary = st ? `
    <div class="sd-60-sum">
      <span class="dim">최근 ${st.days || 60}거래일 동안 순매수한 날들의 평균 가격 대비 지금(추정)</span>
      ${sinceRow('개인', st.indivSinceBuy, st.indivMarketSinceBuy)}
      ${sinceRow('외국인', st.foreignSinceBuy, st.foreignMarketSinceBuy)}
      <span class="dim">60일 순매수 금액(근사) 개인 ${eok(st.indivValue)} · 외국인 ${eok(st.foreignValue)} · 기관 ${eok(st.instValue)}</span>
    </div>` : '';
  const legend = `<div class="sd-legend">${
    ACTOR_KEYS.map(k => `<span><i style="background:${ACTORS[k].raw}"></i>${ACTORS[k].name}</span>`).join('')
  }<span><i class="lineish" style="background:#ffffff66;height:3px"></i>종가</span></div>`;

  const draw = (x, title, holdNote) => {
    box.innerHTML = `
      <div class="sd-60-head">${title} <span class="dim">· 시작일을 0으로 둔 누적 순매수(주)</span></div>
      ${legend}
      <div class="chart-wrap"><svg class="chart" role="img"></svg></div>
      ${holdNote}${summary}`;
    const svg = $('svg', box);
    STOCK_CHART = () => { if (selectedStock === code && svg.isConnected) stockChartSVG(svg, x, s.name); };
    stockChartSVG(svg, x, s.name);
  };

  const fallback60 = () => {
    if (!s.top) { box.innerHTML = '<p class="dim small">이 종목의 수급 추이 데이터가 없습니다.</p>' + summary; return; }
    loadStockFlows().then(sf => {
      if (selectedStock !== code) return;                    // 그사이 다른 종목을 골랐다
      const x = alignSeries(sf.stocks?.[code]);
      if (!x) { box.innerHTML = '<p class="dim small">60일 수급 데이터가 없습니다.</p>' + summary; return; }
      draw(x, `최근 ${x.d.length}거래일`, '');
    }).catch(() => {
      if (selectedStock === code) box.innerHTML = '<p class="dim small">60일 수급을 불러오지 못했습니다.</p>' + summary;
    });
  };

  loadSeries(code).then(raw => {
    if (selectedStock !== code) return;
    const x = alignSeries(raw);
    if (!x) { fallback60(); return; }
    // 외국인 보유 비율은 축을 하나 더 만들지 않고 처음·끝 값과 작은 추이선으로
    const h = (x.h || []).filter(v => v != null);
    const holdNote = h.length >= 2 ? `
      <div class="sd-hold">외국인 보유 비율 ${sparkSVG(h, 'sd-hold-spark', null)}
        <span>${h[0].toFixed(2)}% → <b>${h[h.length - 1].toFixed(2)}%</b>
        <span class="dim">(${sgn(h[h.length - 1] - h[0])}%p, ${h.length}거래일)</span></span></div>` : '';
    draw(x, `최근 ${x.d.length}거래일(약 ${Math.max(1, Math.round(x.d.length / 21))}개월)`, holdNote);
  });
}

/* ── 업종 ─────────────────────────────────────────────── */

function renderIndustries() {
  const list = (D.stocks.industries || []).filter(g => g.changeRate != null);
  const up = list.slice(-8).reverse(), down = list.slice(0, 8);
  const max = Math.max(...list.map(g => Math.abs(g.changeRate)), 1);

  const paint = (box, arr) => {
    box.innerHTML = '';
    arr.forEach((g, i) => {
      const row = el('div', 'bar-row', `
        <div class="bar-shell">
          <div class="bar-fill" style="width:0%;background:linear-gradient(90deg,${
            g.changeRate >= 0 ? '#ff4d4dcc,#ff4d4d55' : '#4d94ffcc,#4d94ff55'})"></div>
          <div class="bar-label">${esc(g.name)} <span class="dim">${esc(g.rise)}▲ ${esc(g.fall)}▼</span></div>
        </div>
        <div class="bar-val ${dirCls(g.changeRate)}">${pct(g.changeRate)}</div>`);
      box.appendChild(row);
      growIn(() => { $('.bar-fill', row).style.width = `${Math.abs(g.changeRate) / max * 100}%`; }, 50 + i * 45);
    });
  };
  paint($('#ind-up'), up);
  paint($('#ind-down'), down);
}

/* ── 글로벌 ───────────────────────────────────────────── */

/** 지표 설명 — 무엇인지까지만. '오르면 좋다/나쁘다'는 적지 않는다 */
const GLOBAL_HINT = {
  '^SOX':  '미국 반도체 지수. 국내 반도체 대형주와 자주 함께 비교됩니다',
  '^GSPC': '미국 대형주 500종목 지수',
  'EWY':   '미국에 상장된 한국 주식 ETF. 한국 장이 닫힌 사이의 움직임',
  'KRW=X': '1달러에 몇 원인지. 외국인 자금 흐름과 함께 자주 언급됩니다',
  '^VIX':  'S&P500 옵션으로 본 미국 증시의 예상 변동성. 흔히 20 이상을 경계, 30 이상을 공포 구간이라 부릅니다',
  '^TNX':  '미국 국채 10년물 금리(%)',
  'ES=F':  '한국 장중에도 거래되는 미국 S&P500 지수 선물',
  'NQ=F':  '한국 장중에도 거래되는 미국 나스닥100 지수 선물',
};

function gitemHTML(g, expect) {
  const old = g.asOf && expect && g.asOf < expect;
  const hint = GLOBAL_HINT[g.symbol];
  return `<div class="gitem" title="${esc(g.name)} (${esc(g.symbol)}) · ${esc(g.asOf)} 기준">
    <div class="gitem-top">
      <div class="gitem-l">
        <div class="gitem-name">${esc(g.name)}</div>
        <div class="gitem-price">${nfmt(g.price, 2)}</div>
        <div class="gitem-chg ${dirCls(g.changeRate)}">${pct(g.changeRate)}</div>
      </div>
      ${sparkSVG(g.spark, 'gitem-spark', g.changeRate)}
    </div>
    <div class="gitem-sub">${moveTxt(g.movePctl)}${old ? `${g.movePctl != null ? ' · ' : ''}${mdy(g.asOf)} 값` : ''}</div>
    ${hint ? `<div class="gitem-hint">${esc(hint)}</div>` : ''}
  </div>`;
}

function renderGlobals() {
  const items = D.global.items || [];
  const main = [...(D.global.preopen || PRE_DEFAULT), '^VIX'];
  const expect = lastUSSession();
  const first = main.map(s => items.find(g => g.symbol === s)).filter(Boolean);
  const rest = items.filter(g => !main.includes(g.symbol));
  $('#globals').innerHTML = first.length ? first.map(g => gitemHTML(g, expect)).join('')
    : '<p class="dim">바깥 지표를 받지 못했습니다.</p>';
  $('#globals-rest').innerHTML = rest.map(g => gitemHTML(g, expect)).join('');

  const mac = $('#macro');
  const macro = D.global.macro || [];
  mac.innerHTML = macro.length
    ? macro.map(m => `<div class="mitem">${esc(m.name)}<b>${esc(m.value)}${esc(m.unit)}</b><small>${esc(m.asOf)}</small></div>`).join('')
    : '';
}

/* ── 이벤트 ───────────────────────────────────────────── */

function renderEvents() {
  const box = $('#events');
  const today = new Date(Date.now() + 9 * 3600e3).toISOString().slice(0, 10);   // KST
  const dday = e => Math.round((Date.parse(e.date) - Date.parse(today)) / 864e5);
  const list = (D.events.upcoming || []).map(e => ({ ...e, dday: e.date ? dday(e) : e.dday }))
                                        .filter(e => e.dday == null || e.dday >= 0);
  box.innerHTML = '';
  if (!list.length) { box.innerHTML = '<p class="dim">예정된 이벤트가 없습니다.</p>'; return; }

  list.forEach(e => {
    const cls = (e.dday === 0 ? 'today' : e.dday <= 3 ? 'soon' : '') + (e.type === '휴장' ? ' holiday' : '');
    box.appendChild(el('div', `event ${cls}`, `
      <div class="event-dday">${e.dday == null ? '—' : e.dday === 0 ? 'D-DAY' : `D-${e.dday}`}</div>
      <div class="event-body">
        <div class="event-title">${esc(e.title)}${e.type ? ` <span class="event-type">${esc(e.type)}</span>` : ''}</div>
        <div class="event-date">${e.date ? dayLabel(e.date) : ''}</div>
        ${e.note ? `<div class="event-note">${esc(e.note)}</div>` : ''}
      </div>`));
  });
}

/* ── 평소와 다른 것 (today.json) ──────────────────────── */

const UNUSUAL_KIND = { streak: '연속 매매', concentration: '종목 쏠림', flow: '수급 크기', program: '프로그램', short: '공매도', credit: '빚투' };

function renderUnusual() {
  const t = D.today || {};
  const items = (t.items || []).slice(0, 3);
  setText('#unusual-date', t.date ? `${dayLabel(t.date)} 기준` : '');
  const box = $('#unusual');
  if (items.length) {
    box.innerHTML = items.map(x => `
      <div class="unusual-item">
        ${UNUSUAL_KIND[x.kind] ? `<span class="ui-kind">${UNUSUAL_KIND[x.kind]}</span>` : ''}
        <div class="ui-text">${esc(x.text)}</div>
        ${x.detail ? `<div class="ui-detail">${esc(x.detail)}</div>` : ''}
      </div>`).join('');
  } else {
    box.innerHTML = `<p class="unusual-none">${esc(t.none || (t.date
      ? '최근 이력과 견줘 특별히 드문 숫자가 없습니다.'
      : '평소와 견준 계산 결과를 아직 받지 못했습니다. 아래 사실 목록을 보세요.'))}</p>`;
  }
  setText('#unusual-footer', t.footer || '');
}

/* ── 관심 종목 (이 브라우저의 localStorage 에만) ─────────── */

const WATCH_KEY = 'ant.watch.v1', WATCH_MAX = 20;
function watchList() {
  const v = store.get(WATCH_KEY, []);
  return Array.isArray(v) ? v.filter(c => typeof c === 'string' && /^[0-9A-Za-z]{6}$/.test(c)).slice(0, WATCH_MAX) : [];
}

/** ☆/★ 토글. 저장소가 막혀 있으면 아예 그리지 않는다 */
function starBtn(code, name) {
  if (!store.ok) return '';
  const on = watchList().includes(code);
  return `<button type="button" class="star" data-star="${esc(code)}" data-name="${esc(name)}" aria-pressed="${on}"
    aria-label="${esc(name)} 관심 종목${on ? '에서 빼기' : '에 넣기'}" title="관심 종목">${on ? '★' : '☆'}</button>`;
}
function syncStars() {
  const w = watchList();
  $$('.star[data-star]').forEach(b => {
    const on = w.includes(b.dataset.star);
    b.setAttribute('aria-pressed', on);
    b.setAttribute('aria-label', `${b.dataset.name || ''} 관심 종목${on ? '에서 빼기' : '에 넣기'}`);
    b.textContent = on ? '★' : '☆';
  });
}
function toggleWatch(code) {
  let w = watchList();
  if (w.includes(code)) w = w.filter(c => c !== code);
  else {
    if (w.length >= WATCH_MAX) { toast(`관심 종목은 ${WATCH_MAX}개까지 담을 수 있습니다.`); return; }
    w.push(code);
  }
  if (!store.set(WATCH_KEY, w)) { toast('이 브라우저에서는 관심 종목을 저장할 수 없습니다.'); return; }
  syncStars();
  safe(renderWatch, '관심 종목');
  safe(renderJump, '바로가기');
}

/** 순위 행 하나(이름·등락 정보를 순위 목록에서라도 찾는다) */
function rankRowOf(code) {
  for (const byActor of Object.values(D.ranks?.markets || {}))
    for (const a of ['foreign', 'institution'])
      for (const p of ['day', 'week'])
        for (const side of ['buy', 'sell']) {
          const r = (byActor?.[a]?.[p]?.[side] || []).find(x => x.code === code);
          if (r) return r;
        }
  return null;
}

/** 오늘(하루) 순위에 든 사실: '외국인 순매도 3위 −2,119억' */
function rankFacts(code) {
  const out = [];
  Object.values(D.ranks?.markets || {}).forEach(byActor => ['foreign', 'institution'].forEach(a => {
    const d = byActor?.[a]?.day;
    if (!d) return;
    [['buy', '순매수'], ['sell', '순매도']].forEach(([side, word]) => {
      const i = (d[side] || []).findIndex(x => x.code === code);
      if (i >= 0) out.push(`${ACTORS[a].name} ${word} ${i + 1}위 ${eok(d[side][i].amt)}`);
    });
  }));
  return out;
}

function renderWatch() {
  const card = $('#watch-card');
  const list = store.ok ? watchList() : [];
  card.hidden = !list.length;
  if (card.hidden) return;
  const R = D.ranks;
  setText('#watch-sub', `${list.length}종목${R?.asOf ? ` · 순위는 ${dayLabel(R.asOf)}${R.final === false ? ' 잠정' : ''}` : ''}`);
  $('#watch').innerHTML = list.map(code => {
    const info = stockInfo(code), rk = rankRowOf(code);
    const name = info?.name ?? rk?.name ?? code;
    const chg = info?.changeRate ?? rk?.chg;
    const sf = info ? stockStreak(info, 'foreign') : null;
    const si = info ? stockStreak(info, 'institution') : null;
    const stTxt = (label, s) =>
      `${label} <b class="${s ? (s.side === 'buy' ? 'up' : 'down') : 'dim'}">${s ? streakTxt(s) : '—'}</b>`;
    const facts = rankFacts(code);
    return `<div class="watch-row">
      <button type="button" class="watch-name" data-open="${esc(code)}" data-name="${esc(name)}">${esc(name)}${
        info ? `<small>${mkName(info.market)}</small>` : '<small>수집 범위 밖</small>'}</button>
      <span class="watch-chg ${dirCls(chg)}">${chg != null ? pct(chg) : '—'}</span>
      ${starBtn(code, name)}
      <div class="watch-facts">
        ${info ? `<span>${stTxt('외국인', sf)}</span><span>${stTxt('기관', si)}</span>` : ''}
        ${facts.map(f => `<span class="fact-chip">${esc(f)}</span>`).join('')}
      </div>
    </div>`;
  }).join('');
}

/* ── 외국인·기관 종목 순위 (ranks.json) ──────────────────── */

function renderRanks() {
  const card = $('#ranks-card');
  const R = D.ranks;
  card.hidden = !(R && R.markets && Object.keys(R.markets).length);
  if (card.hidden) return;
  [['#ranks-actor-seg', 'actor', rankActor], ['#ranks-market-seg', 'market', rankMarket], ['#ranks-period-seg', 'period', rankPeriod]]
    .forEach(([sel, key, v]) => $$(`${sel} button`).forEach(b => b.classList.toggle('on', b.dataset[key] === v)));

  // 장중이나 아직 확정 전이면 지난 거래일 순위라는 걸 먼저 말한다
  const stale = R.final === false || (isProvisionalPhase(D.meta?.phase) && R.asOf !== todayKST());
  setText('#ranks-cap', stale
    ? `전 거래일(${dayLabel(R.asOf)}) 확정 · 오늘 값은 장 마감 뒤${R.scope ? ` · ${R.scope}` : ''}`
    : `${dayLabel(R.asOf)} 확정${R.scope ? ` · ${R.scope}` : ''}`);

  const sel = R.markets[rankMarket]?.[rankActor]?.[rankPeriod];
  const more = $('#ranks-more');
  if (!sel) {
    $('#ranks').innerHTML = '<p class="dim">이 조합의 순위를 받지 못했습니다.</p>';
    setText('#ranks-range', '');
    more.hidden = true;
    return;
  }
  const to = dayLabel(sel.to || R.asOf);
  setText('#ranks-range', (rankPeriod === 'week'
    ? `${sel.from && sel.from !== sel.to ? `${dayLabel(sel.from)} ~ ${to}` : `${to}까지`} 1주 합계`
    : `${to} 하루`) + ` · ${ACTORS[rankActor].name} · ${mkName(rankMarket)}`);

  const n = rankOpen ? 20 : 5;
  const row = (r, i) => `
    <li class="rank-row">
      <span class="rank-no">${i + 1}</span>
      <button type="button" class="rank-name" data-open="${esc(r.code)}" data-name="${esc(r.name)}">${esc(r.name)}</button>
      ${starBtn(r.code, r.name)}
      <span class="rank-amt ${dirCls(r.amt)}">${eok(r.amt)}</span>
      <span class="rank-chg ${dirCls(r.chg)}">${pct(r.chg)}</span>
      <span class="rank-sub">${r.volPct != null ? `거래량의 ${(+r.volPct).toFixed(1)}%` : ''}${
        r.streak?.days >= 2 ? `${r.volPct != null ? ' · ' : ''}${r.streak.days}일 연속 ${r.streak.side === 'buy' ? '매수' : '매도'}` : ''}</span>
    </li>`;
  const col = (side, rows) => `
    <div class="rank-col">
      <h3>${ACTORS[rankActor].name} ${side === 'buy' ? '순매수' : '순매도'}</h3>
      <ol class="rank-list">${rows.slice(0, n).map(row).join('') || '<li class="dim small">없음</li>'}</ol>
    </div>`;
  $('#ranks').innerHTML = col('buy', sel.buy || []) + col('sell', sel.sell || []);
  const longest = Math.max((sel.buy || []).length, (sel.sell || []).length);
  more.hidden = longest <= 5;
  more.textContent = rankOpen ? '접기' : `펼쳐 보기 (${Math.min(20, longest)}위까지)`;
  more.setAttribute('aria-expanded', rankOpen ? 'true' : 'false');
}

/* ── 속설 검증표 (docs/evidence.json) ─────────────────────── */

function renderEvidence() {
  const ev = D.evidence;
  const box = $('#evidence');
  if (!ev || !ev.rows?.length) {
    box.innerHTML = '<p class="dim">검증표를 불러오지 못했습니다.</p>';
    ['#evidence-method', '#evidence-multiple', '#evidence-asof'].forEach(s => setText(s, ''));
    return;
  }
  setText('#evidence-title', ev.title || '수급 속설 검증표');
  setText('#evidence-asof', ev.asOf ? `${ev.asOf} 기준` : '');
  setText('#evidence-method', ev.method || '');
  setText('#evidence-multiple', ev.multiple || '');
  const vcls = v => v === '통과' ? 'pass' : v === '참고만' ? 'ref' : 'none';
  box.innerHTML = ev.rows.map(r => `
    <article class="ev-row">
      <div class="ev-top">
        <div class="ev-claim"><span class="ev-id">${esc(r.id)}</span>${esc(r.claim)}</div>
        <span class="ev-verdict v-${vcls(r.verdict)}">${esc(r.verdict)}</span>
      </div>
      <dl class="ev-res">
        <div><dt>2009~2023 결과</dt><dd>${esc(r.oos)}</dd></div>
        <div><dt>최근 3년</dt><dd>${esc(r.site)}</dd></div>
      </dl>
      <p class="ev-plain">${esc(r.plain)}</p>
    </article>`).join('');
}

/* ── 장중 잠정치 기록 ─────────────────────────────────── */

function renderIntraHist() {
  const h = D.intraday_hist || {};
  const n = h.count ?? h.days?.length ?? 0;
  const p = $('#intra-hist');
  p.hidden = !n;
  if (n) p.textContent = `장중 잠정치 기록 ${n}일째 쌓는 중`;
}

/* ── 바로가기: 숨은 섹션으로 가는 칩은 숨긴다 ───────────── */

function renderJump() {
  $$('#jump a').forEach(a => {
    const t = document.getElementById(a.getAttribute('href').slice(1));
    a.hidden = !t || t.hidden;
  });
}

/* ── 용어 풀이 ───────────────────────────────────────── */

const TERMS = {
  pctl: ['백분위', '여러 날의 값을 작은 것부터 줄 세웠을 때의 위치(0~100)입니다. 90번째 백분위면 100일 중 90일보다 컸다는 뜻이고, 높거나 낮다고 좋은 것도 나쁜 것도 아닙니다.'],
  pp: ['%p (퍼센트포인트)', '퍼센트끼리의 차이입니다. 3%와 5%의 차이는 2%p입니다(‘2% 차이’라고 하면 3%의 2%로 오해될 수 있습니다).'],
  corp: ['기타법인', '개인·외국인·기관에 들지 않는 일반 법인입니다(상장사의 자기주식 매매, 비금융 법인 등). 개인·외국인·기관·기타법인의 순매수를 모두 더하면 0입니다.'],
  contract: ['계약 (선물)', '선물 거래의 단위입니다. 코스피200 선물 1계약은 지수 × 25만 원어치라, 계약 수와 현물 금액(억원)은 단위가 다릅니다.'],
  liq: ['반대매매', '빚(신용융자·미수)으로 산 주식의 담보가 모자라거나 기한 안에 갚지 못하면 증권사가 그 주식을 강제로 파는 것입니다.'],
  prov: ['잠정 / 확정', '장중과 마감 직후의 투자자별 수급은 추정치(잠정)입니다. 저녁 20시 무렵 거래소 집계가 나오면 값이 바뀌고(확정), 잠정치와 꽤 다를 수 있습니다.'],
  arb: ['비차익 / 차익', '프로그램 매매 가운데 차익은 선물과 현물의 가격 차를 노린 매매, 비차익은 여러 종목을 바스켓으로 묶어 한꺼번에 사고파는 매매(주로 외국인·기관의 지수형 자금)입니다.'],
  shortbal: ['순보유잔고', '공매도한 뒤 아직 갚지 않은 물량 가운데 보고 의무(상장주식의 0.01% 이상 등)가 있는 것만 합친 값입니다. 실제 잔고보다 작습니다.'],
  intensity: ['거래대금 대비 순매수 (강도)', '순매수 금액을 그날 시장 전체 거래대금으로 나눈 값입니다. 거래가 많은 날과 적은 날을 같은 잣대로 견주려고 씁니다.'],
};

function wireTerms() {
  const pop = $('#term-pop');
  let cur = null;
  const close = () => {
    if (!cur) return;
    cur.setAttribute('aria-expanded', 'false');
    cur = null;
    pop.hidden = true;
  };
  document.addEventListener('click', e => {
    const b = e.target.closest('button.term');
    if (b) {
      e.preventDefault();
      if (cur === b) { close(); return; }
      close();
      const t = TERMS[b.dataset.term];
      if (!t) return;
      pop.innerHTML = `<b>${esc(t[0])}</b><p>${esc(t[1])}</p>`;
      pop.hidden = false;
      cur = b;
      b.setAttribute('aria-expanded', 'true');
      b.setAttribute('aria-controls', 'term-pop');
      const r = b.getBoundingClientRect();
      const w = Math.min(300, window.innerWidth - 24);
      pop.style.width = `${w}px`;
      pop.style.left = `${Math.max(12, Math.min(r.left, window.innerWidth - w - 12))}px`;
      const below = r.bottom + 6, h = pop.offsetHeight;
      pop.style.top = `${below + h > window.innerHeight - 8 ? Math.max(8, r.top - h - 6) : below}px`;
      return;
    }
    if (!e.target.closest('#term-pop')) close();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
  window.addEventListener('scroll', close, { passive: true });
  window.addEventListener('resize', close);
}

/** 짧은 알림(관심 종목 개수 초과, 수집 범위 밖 등) */
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.hidden = true; }, 2800);
}

/* ── 접어 둔 칸(<details>)의 열림 상태를 이 브라우저에 기억 ── */

const DETAILS_KEY = 'ant.details.v1';
function initDetails() {
  const saved = store.get(DETAILS_KEY, {}) || {};
  $$('details[id]').forEach(d => {
    if (typeof saved[d.id] === 'boolean') d.open = saved[d.id];
    d.addEventListener('toggle', () => {
      const s = store.get(DETAILS_KEY, {}) || {};
      s[d.id] = d.open;
      store.set(DETAILS_KEY, s);
      // 접혀 있던 차트는 폭을 몰라 어림으로 그렸다 — 펼치면 실제 폭으로 다시
      if (d.open && d.querySelector('svg.chart')) rerenderCharts();
    });
  });
}

/* ── 컨트롤 ───────────────────────────────────────────── */

function wireControls() {
  if (wireControls.done) return;
  wireControls.done = true;
  wireTerms();

  // 순위 탭
  [['#ranks-actor-seg', 'actor', v => { rankActor = v; }],
   ['#ranks-market-seg', 'market', v => { rankMarket = v; }],
   ['#ranks-period-seg', 'period', v => { rankPeriod = v; }]].forEach(([sel, key, set]) =>
    $$(`${sel} button`).forEach(b => b.addEventListener('click', () => {
      set(b.dataset[key]);
      setText('#ranks-note', '');
      safe(renderRanks, '종목 순위');
    })));
  $('#ranks-more').addEventListener('click', () => {
    rankOpen = !rankOpen;
    safe(renderRanks, '종목 순위');
    if (!rankOpen) $('#ranks-card').scrollIntoView({ block: 'nearest' });
  });

  // 별(관심 종목)과 종목 이름 버튼은 여러 곳에 있어 한 군데서 받는다
  document.addEventListener('click', e => {
    const s = e.target.closest('button.star[data-star]');
    if (s) { toggleWatch(s.dataset.star); return; }
    const o = e.target.closest('[data-open]');
    if (o) {
      if (openStock(o.dataset.open)) { setText('#ranks-note', ''); return; }
      const msg = `‘${o.dataset.name || o.dataset.open}’ — 종목 상세 수집 범위(시총 상위 종목) 밖이라 상세를 보여 드릴 수 없습니다.`;
      if (o.closest('#ranks-card')) setText('#ranks-note', msg);
      else toast(msg);
    }
  });

  // 바로가기나 본문 링크가 접힌 칸을 가리키면 펼친 뒤 이동한다
  document.addEventListener('click', e => {
    const a = e.target.closest('a[href^="#"]');
    if (!a) return;
    const t = document.getElementById(a.getAttribute('href').slice(1));
    if (t && t.tagName === 'DETAILS') t.open = true;
  });

  $$('#hero-market-seg button').forEach(b => b.addEventListener('click', () => {
    $$('#hero-market-seg button').forEach(x => x.classList.toggle('on', x === b));
    heroMarket = b.dataset.market;
    [[renderHero, '수급 보드'], [renderIntraday, '장중 흐름'], [renderFlowChart, '수급 차트'],
     [renderStreaks, '연속 매매'], [renderInstBreakdown, '기관 분해'], [renderJump, '바로가기']]
      .forEach(([fn, label]) => safe(fn, label));
  }));

  $$('#report-market-seg button').forEach(b => b.addEventListener('click', () => {
    reportMarket = b.dataset.market;
    safe(renderReportCard, '성적표');
  }));

  $$('#program-market-seg button').forEach(b => b.addEventListener('click', () => {
    programMarket = b.dataset.market;
    safe(renderProgram, '프로그램 매매');
  }));

  $$('#short-market-seg button').forEach(b => b.addEventListener('click', () => {
    shortMarket = b.dataset.market;
    safe(renderShort, '공매도');
  }));

  $$('#flow-mode-seg button').forEach(b => b.addEventListener('click', () => {
    $$('#flow-mode-seg button').forEach(x => x.classList.toggle('on', x === b));
    flowMode = b.dataset.mode;
    renderFlowChart();
  }));

  $$('#flow-range-seg button').forEach(b => b.addEventListener('click', () => {
    $$('#flow-range-seg button').forEach(x => x.classList.toggle('on', x === b));
    flowRange = +b.dataset.range;
    renderFlowChart();
  }));

  $$('#tree-market-seg button').forEach(b => b.addEventListener('click', () => {
    $$('#tree-market-seg button').forEach(x => x.classList.toggle('on', x === b));
    treeMarket = b.dataset.tmarket;
    selectedStock = null;
    $('#stock-detail').hidden = true;
    renderTreemap();
  }));

  wireSearch();
}

/* ── 종목 검색 ────────────────────────────────────────── */

function wireSearch() {
  const input = $('#stock-search'), list = $('#search-results');
  if (!input) return;
  let activeIdx = -1;

  const close = () => { list.hidden = true; list.innerHTML = ''; activeIdx = -1; };

  // 검색 대상: 수집 범위(universe, 약 350종목) + 시총 상위(top). 같은 종목은 top 쪽 값을 쓴다
  const pool = () => {
    const m = new Map();
    (D.stocks.universe || []).forEach(u => m.set(u.code,
      { code: u.code, name: u.name, market: u.market, price: u.price, changeRate: u.chg }));
    (D.stocks.top || []).forEach(t => m.set(t.code, t));
    return [...m.values()].filter(s => s.name);
  };

  const search = q => {
    q = q.trim().toLowerCase();
    if (!q) { close(); return; }
    const all = pool();
    const hits = all.filter(s => s.name.toLowerCase().includes(q) || s.code === q).slice(0, 8);
    if (!hits.length) {
      list.hidden = false;
      list.innerHTML = `<div class="sr-item" style="cursor:default;color:var(--dimmer)">
        "${esc(q)}" — 수집된 ${all.length}종목 안에 없습니다 (시총 상위 위주로 수집합니다)</div>`;
      return;
    }
    list.hidden = false;
    list.innerHTML = '';
    hits.forEach(s => {
      const item = el('button', 'sr-item', `
        <span class="sr-name">${highlight(s.name, q)}
          <span class="sr-meta">${mkName(s.market)} · ${esc(s.code)}</span></span>
        <span class="sr-chg ${dirCls(s.changeRate)}">${s.price != null ? `${nfmt(s.price, 0)} · ` : ''}${pct(s.changeRate)}</span>`);
      item.addEventListener('click', () => {
        close();
        input.value = s.name;
        openStock(s.code);
      });
      list.appendChild(item);
    });
  };

  const highlight = (name, q) => {
    const i = name.toLowerCase().indexOf(q);
    if (i < 0) return esc(name);
    return `${esc(name.slice(0, i))}<b>${esc(name.slice(i, i + q.length))}</b>${esc(name.slice(i + q.length))}`;
  };

  input.addEventListener('input', () => search(input.value));
  input.addEventListener('focus', () => search(input.value));
  input.addEventListener('keydown', e => {
    const items = $$('.sr-item', list).filter(x => x.tagName === 'BUTTON');
    if (e.key === 'Escape') { close(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      activeIdx = e.key === 'ArrowDown'
        ? Math.min(activeIdx + 1, items.length - 1)
        : Math.max(activeIdx - 1, 0);
      items.forEach((x, i) => x.classList.toggle('active', i === activeIdx));
    }
    if (e.key === 'Enter' && items.length) {
      e.preventDefault();
      (items[Math.max(activeIdx, 0)]).click();
    }
  });
  document.addEventListener('click', e => {
    if (!e.target.closest('.stock-search')) close();
  });
}

boot();

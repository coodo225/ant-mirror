/* ══════════════════════════════════════════════════════════
   개인 · 외국인 · 기관 — 한국 증시 수급 대시보드
   의존성 없음. docs/data/*.json 만 읽는다.
   ══════════════════════════════════════════════════════════ */

const ACTORS = {
  individual:  { name: '개인',   sub: '개미',   face: '🐜', color: 'var(--individual)', raw: '#ffb02e' },
  foreign:     { name: '외국인', sub: '외인',   face: '🦅', color: 'var(--foreign)',    raw: '#4fc3f7' },
  institution: { name: '기관',   sub: '기관계', face: '🐋', color: 'var(--institution)', raw: '#b388ff' },
};
const ACTOR_KEYS = ['individual', 'foreign', 'institution'];

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
let selectedStock = null;

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

/** 종목 수급은 '주' 단위 */
function shares(v) {
  if (v == null || isNaN(v)) return '—';
  const s = v < 0 ? '-' : '+';
  const a = Math.abs(v);
  if (a >= 10000) return `${s}${(a / 10000).toFixed(1)}만주`;
  return `${s}${Math.round(a).toLocaleString()}주`;
}

/* ── 부트 ─────────────────────────────────────────────── */

const FILES = ['meta', 'flows', 'ant', 'futures', 'credit', 'market', 'stocks', 'global', 'events', 'insights'];

/** 응답이 없으면 무한정 기다리지 않는다. 실패하면 왜 실패했는지 남긴다. */
async function fetchJSON(name, { timeout = 8000, tries = 2 } = {}) {
  let last = '알 수 없는 오류';
  for (let i = 0; i < tries; i++) {
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), timeout);
    try {
      const r = await fetch(`data/${name}.json?t=${Date.now()}`,
                            { cache: 'no-store', signal: ac.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) {
      last = e.name === 'AbortError' ? `${timeout / 1000}초 안에 응답 없음` : e.message;
    } finally {
      clearTimeout(timer);
    }
  }
  throw new Error(`data/${name}.json — ${last}`);
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

async function boot() {
  RENDER_ERRORS.length = 0;
  const slow = setTimeout(() => {
    $('#loading-detail').textContent = '예상보다 오래 걸립니다. 서버 응답을 기다리는 중…';
  }, 6000);

  try {
    const results = await Promise.all(FILES.map(async f => [f, await fetchJSON(f)]));
    results.forEach(([k, v]) => { D[k] = v; });
  } catch (e) {
    showFatal(e);
    return;
  } finally {
    clearTimeout(slow);
  }

  $('#loading').hidden = true;
  $('#app').hidden = false;

  safe(renderMeta, '헤더');
  safe(renderMirror, '거울 한 줄');
  safe(renderHero, '줄다리기');
  safe(renderThermo, '개미 온도계');
  safe(renderFutures, '선물');
  safe(renderCredit, '빚투 체온계');
  safe(renderReportCard, '성적표');
  safe(renderOppose, '반대로 가는 본능');
  safe(renderHall, '흑역사');
  safe(renderCaveats, '한계 고백');
  safe(renderInsights, '브리핑');
  safe(renderIndices, '지수');
  safe(renderFlowChart, '수급 차트');
  safe(renderStreaks, '연속 매매');
  safe(renderInstBreakdown, '기관 분해');
  safe(renderTreemap, '트리맵');
  safe(renderIndustries, '업종');
  safe(renderGlobals, '글로벌');
  safe(renderEvents, '이벤트');
  safe(wireControls, '컨트롤');
  reportRenderErrors();

  // 장중이면 60초마다 조용히 갱신
  if ((D.meta.phase || '').includes('장중')) setInterval(refresh, 60000);
}

async function refresh() {
  try {
    const results = await Promise.all(
      FILES.map(async f => [f, await fetchJSON(f, { timeout: 8000, tries: 1 })])
    );
    results.forEach(([k, v]) => { D[k] = v; });
  } catch (e) {
    console.warn('자동 갱신 실패, 다음 주기에 재시도:', e.message);
    return;                       // 이미 그려진 화면은 그대로 둔다
  }
  RENDER_ERRORS.length = 0;
  [[renderMeta, '헤더'], [renderMirror, '거울 한 줄'], [renderHero, '줄다리기'],
   [renderThermo, '개미 온도계'], [renderFutures, '선물'], [renderCredit, '빚투 체온계'],
   [renderInsights, '브리핑'], [renderIndices, '지수'],
   [renderFlowChart, '수급 차트'], [renderStreaks, '연속 매매'],
   [renderInstBreakdown, '기관 분해']].forEach(([fn, label]) => safe(fn, label));
  reportRenderErrors();
}

/* ── 헤더 ─────────────────────────────────────────────── */

function renderMeta() {
  const m = D.meta;
  const badge = $('#phase-badge');
  badge.textContent = m.phase;
  badge.classList.toggle('live', (m.phase || '').includes('장중'));
  $('#updated').textContent = m.generatedAtText;
  $('#provisional').hidden = !((m.phase || '').includes('장중') || (m.phase || '').includes('동시호가'));

  $('#sources').textContent = (m.sources || []).map(s => s.name).join(' · ');
  $('#caveat').textContent = m.caveat || '';

  const w = $('#warnings');
  const lines = [...(m.warnings || [])];
  if (lines.length) {
    w.hidden = false;
    w.innerHTML = '<b>수집 경고</b>' + lines.map(x => `<div>· ${x}</div>`).join('');
  } else w.hidden = true;
}

/** 렌더 중 터진 섹션이 있으면 조용히 넘어가지 않고 화면에 적는다 */
function reportRenderErrors() {
  if (!RENDER_ERRORS.length) return;
  const w = $('#warnings');
  w.hidden = false;
  w.innerHTML += '<b style="margin-top:8px">화면 오류</b>' +
    RENDER_ERRORS.map(x => `<div>· ${x}</div>`).join('');
}

/* ── 히어로: 줄다리기 ─────────────────────────────────── */

function renderHero() {
  const mk = D.flows.markets[heroMarket];
  const tug = $('#tug');
  if (!mk) { tug.innerHTML = '<p class="dim">데이터가 없습니다.</p>'; return; }

  const last = mk.latest;
  $('#hero-date').textContent =
    `${last.date} · ${heroMarket === 'KOSPI' ? '코스피' : '코스닥'} · 단위 억원`;

  const vals = ACTOR_KEYS.map(k => ({ key: k, v: last[k] ?? 0 }));
  const max = Math.max(...vals.map(x => Math.abs(x.v)), 1);
  const lead = vals.reduce((a, b) => Math.abs(b.v) > Math.abs(a.v) ? b : a);

  // 3주체 다이버징 바
  tug.innerHTML = '';
  vals.forEach(({ key, v }, i) => {
    const a = ACTORS[key];
    const lane = el('div', 'lane' + (key === lead.key ? ' grow' : ''));
    const half = Math.abs(v) / max * 50;   // 트랙 절반 기준 %

    lane.innerHTML = `
      <div class="lane-who">
        <div class="lane-face">${a.face}</div>
        <div class="lane-name">${a.name}<small>${key === lead.key ? '오늘의 주역' : a.sub}</small></div>
      </div>
      <div class="lane-track">
        <div class="lane-bar ${v >= 0 ? 'buy' : 'sell'}"
             style="background:linear-gradient(${v >= 0 ? '90deg' : '270deg'}, ${a.raw}dd, ${a.raw}77);
                    ${v >= 0 ? '' : `left:${50 - half}%;`}">
          <span class="lane-val" style="color:${a.raw}">${eok(v)}</span>
        </div>
      </div>`;
    tug.appendChild(lane);
    growIn(() => { $('.lane-bar', lane).style.width = `${Math.max(half, 0.6)}%`; }, 60 + i * 90);
  });

  renderBalance(vals);
}

/** 파는 진영 vs 사는 진영 구성 막대 */
function renderBalance(vals) {
  const sellers = vals.filter(x => x.v < 0).sort((a, b) => a.v - b.v);
  const buyers  = vals.filter(x => x.v > 0).sort((a, b) => b.v - a.v);
  const sellSum = sellers.reduce((s, x) => s + Math.abs(x.v), 0);
  const buySum  = buyers.reduce((s, x) => s + x.v, 0);
  const total   = sellSum + buySum || 1;

  const wrap = $('.balance');
  const track = $('.balance-track', wrap);
  track.innerHTML = '';
  $$('.balance-knot', wrap).forEach(n => n.remove());
  let cursor = 0;
  const put = (arr, isSell) => arr.forEach(({ key, v }) => {
    const wpc = Math.abs(v) / total * 100;
    const seg = el('div', 'bseg');
    Object.assign(seg.style, {
      position: 'absolute', top: '0', bottom: '0',
      left: `${cursor}%`, width: '0%',
      background: ACTORS[key].raw,
      opacity: isSell ? '.85' : '1',
      transition: 'width .9s cubic-bezier(.22,1,.36,1)',
    });
    seg.title = `${ACTORS[key].name} ${eok(v)}`;
    track.appendChild(seg);
    growIn(() => { seg.style.width = `${wpc}%`; }, 80);
    cursor += wpc;
  });
  put(sellers, true);
  put(buyers, false);

  const knot = el('div', 'balance-knot', '🪢');
  knot.style.left = `${sellSum / total * 100}%`;
  wrap.appendChild(knot);

  const names = a => a.map(x => ACTORS[x.key].name).join(' · ') || '없음';
  $('#balance-caption').innerHTML =
    `파는 쪽 <b>${names(sellers)}</b> ${eok(-sellSum)} &nbsp;·&nbsp; ` +
    `사는 쪽 <b>${names(buyers)}</b> ${eok(buySum)}` +
    `<br><span class="dim small">순매수와 순매도는 서로의 거울입니다. 누군가 판 물량은 반드시 누군가 받습니다.</span>`;
}

/* ══════════════════════════════════════════════════════
   개미 파트 — 풍자는 톤에만, 숫자는 있는 그대로
   ══════════════════════════════════════════════════════ */

const hasAnt = () => D.ant && D.ant.actors;

/** 헤더 아래 한 줄. 무작위 농담이 아니라 오늘 데이터에서 나온 문장이어야 한다. */
function renderMirror() {
  const line = $('#mirror-line'), sub = $('#mirror-sub');
  if (!hasAnt()) {
    $('#mirror').hidden = true;
    return;
  }
  const a = D.ant.actors.individual;
  const p = a.todayPercentile;
  const opp = D.ant.oppositeRate.vsForeign;

  let head, tail;
  if (p >= 80) {
    head = '오늘도 개미는 담고 있습니다.';
    tail = `최근 ${D.ant.sample.days}거래일 중 <b>상위 ${(100 - p).toFixed(0)}%</b>의 매수 강도. ` +
           '지난 1년, 이런 날의 뒤끝이 문제였습니다.';
  } else if (p <= 20) {
    head = '오늘 개미는 던지고 있습니다.';
    tail = `최근 ${D.ant.sample.days}거래일 중 <b>하위 ${p.toFixed(0)}%</b>. ` +
           '그런데 지난 1년, 개미가 던진 뒤는 나쁘지 않았습니다. 아래에서 확인하세요.';
  } else {
    head = '오늘 개미는 어중간합니다.';
    tail = `최근 ${D.ant.sample.days}거래일 중 <b>${p.toFixed(0)}번째 백분위</b>. ` +
           '방향이 뚜렷하지 않은 날입니다.';
  }
  line.textContent = head;
  sub.innerHTML = `${tail} 참고로 개미는 이 기간 <b>${opp}%</b>의 날에 외국인과 정반대로 움직였습니다.`;
}

/** 오늘의 매수 강도를 1년 분포 안에 놓아 본다 */
function renderThermo() {
  if (!hasAnt()) { $('#thermo-card').hidden = true; return; }
  const a = D.ant.actors.individual;
  const p = a.todayPercentile;

  $('#thermo-sample').textContent = D.ant.sample.days;
  growIn(() => { $('#thermo-ant').style.left = `${p}%`; }, 120);

  const mood = p >= 80 ? { t: '영끌 매수 구간', c: 'up' }
             : p >= 60 ? { t: '사는 쪽', c: 'up' }
             : p > 40  ? { t: '관망', c: 'flat' }
             : p > 20  ? { t: '파는 쪽', c: 'down' }
             :           { t: '패닉 매도 구간', c: 'down' };

  $('#thermo-nums').innerHTML = `
    <div>
      <span class="thermo-big ${mood.c}">${p.toFixed(0)}<small style="font-size:14px">번째 백분위</small></span>
      <span class="thermo-desc" style="margin-left:10px">${mood.t}</span>
    </div>
    <div class="thermo-desc">오늘 개인 순매수 <b class="${dirCls(a.todayValue)}">${eok(a.todayValue)}</b></div>`;

  const cr = D.ant.contrarianRead;
  const box = $('#contrarian');
  if (!cr) {
    box.innerHTML = `<div class="c-head">오늘은 참고할 만한 극단이 아닙니다</div>
      개미의 매수 강도가 평범한 구간이라 과거 비교 표본을 뽑지 않았습니다.
      <div class="c-note">상·하위 20% 구간에 들어오면 과거 같은 국면의 20거래일 성적을 보여줍니다.</div>`;
    return;
  }
  const better = cr.excess >= 0;
  box.innerHTML = `
    <div class="c-head">🐜 ${cr.label} — 과거 ${cr.n}번</div>
    그 <b>20거래일 뒤</b> 지수는 평균 <b class="${better ? 'up' : 'down'}">${cr.r20 >= 0 ? '+' : ''}${cr.r20}%</b>,
    같은 기간 시장 평균은 <b>${cr.baseline20 >= 0 ? '+' : ''}${cr.baseline20}%</b>였습니다.
    시장 평균 대비 <b class="${better ? 'up' : 'down'}">${cr.excess >= 0 ? '+' : ''}${cr.excess}%p</b>.
    <div class="c-note">과거 기록이지 예측이 아닙니다. 표본 ${cr.n}개는 결론을 내리기엔 적은 수입니다.</div>`;
}

/* ── 선물: 외국인의 본심 ─────────────────────────────── */

function renderFutures() {
  const card = $('#futures-card');
  const f = D.futures;
  if (!f || !f.daily?.length) { card.hidden = true; return; }

  const div = f.divergence;
  const box = $('#fut-divergence');
  if (div) {
    const spotDir = div.spotForeign >= 0 ? '사고' : '팔고';
    const futDir = div.futuresForeign >= 0 ? '사는' : '파는';
    if (!div.aligned) {
      box.className = 'divergence split';
      box.innerHTML = `
        <div class="div-head">⚠️ 현물과 선물이 갈립니다</div>
        최근 ${div.window}거래일, 외국인이 현물은 <b>${eok(div.spotForeign)}</b> ${spotDir}
        선물은 <b>${div.futuresForeign >= 0 ? '+' : ''}${div.futuresForeign.toLocaleString()}계약</b> ${futDir} 중.
        선물이 먼저 도는 경우가 많았습니다 — 보이는 매도가 전부가 아닐 수 있습니다.`;
    } else {
      box.className = 'divergence';
      box.innerHTML = `
        <div class="div-head">현물과 선물이 같은 방향입니다</div>
        최근 ${div.window}거래일, 외국인은 현물 <b>${eok(div.spotForeign)}</b>,
        선물 <b>${div.futuresForeign >= 0 ? '+' : ''}${div.futuresForeign.toLocaleString()}계약</b>.
        엇갈린 신호는 없습니다.`;
    }
  }

  // 최근 20일 외국인 선물 순매수 막대 차트
  const rows = f.daily.slice(-20);
  const W = 640, H = 190, M = { t: 12, r: 10, b: 24, l: 46 };
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
    g += `<text x="${M.l - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#5d6780"
           font-size="10.5" font-family="ui-monospace,monospace">${Math.round(v).toLocaleString()}</text>`;
  }
  g += `<line x1="${M.l}" y1="${Y(0).toFixed(1)}" x2="${W - M.r}" y2="${Y(0).toFixed(1)}" stroke="#4a5570" stroke-width="1.3"/>`;
  rows.forEach((r, i) => {
    const v = r.foreign ?? 0;
    const x = M.l + i * bw + bw * 0.18;
    const y0 = Y(0), y1 = Y(v);
    g += `<rect x="${x.toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" width="${(bw * 0.64).toFixed(1)}"
           height="${Math.max(Math.abs(y1 - y0), 0.8).toFixed(1)}" rx="2"
           fill="${v >= 0 ? '#ff4d4d' : '#4d94ff'}" opacity=".88"><title>${r.date} ${v >= 0 ? '+' : ''}${v.toLocaleString()}계약</title></rect>`;
    if (i % 4 === 0 || i === rows.length - 1)
      g += `<text x="${(x + bw * 0.32).toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="#5d6780"
             font-size="9.5" font-family="ui-monospace,monospace">${mdy(r.date)}</text>`;
  });
  const svg = $('#fut-chart');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;

  const st = f.streaks?.foreign;
  const last = f.latest;
  $('#fut-note').textContent =
    `외국인 선물 순매수 최근 20거래일. 오늘 ${(last.foreign ?? 0) >= 0 ? '+' : ''}${(last.foreign ?? 0).toLocaleString()}계약` +
    (st && st.days >= 2 ? ` · ${st.days}일 연속 ${st.side === 'buy' ? '매수' : '매도'}` : '') + '.';
}

/* ── 빚투 체온계 ─────────────────────────────────────── */

function renderCredit() {
  const card = $('#credit-card');
  const c = D.credit;
  if (!c || !c.loans?.length) { card.hidden = true; return; }

  const ln = c.latest.loans || {};
  const mo = c.latest.money || {};
  const liqHot = mo.liquidation != null && mo.liqAvg20 && mo.liquidation >= mo.liqAvg20 * 2;

  $('#credit-stats').innerHTML = `
    <div class="cstat">
      <div class="cstat-label">신용융자 잔고 (빚투)</div>
      <div class="cstat-val">${eok(ln.total, { sign: false })}</div>
      <div class="cstat-sub">5일 ${ln.d5 != null ? eok(ln.d5) : '—'} · 20일 ${ln.d20 != null ? eok(ln.d20) : '—'}</div>
    </div>
    <div class="cstat ${liqHot ? 'alert' : ''}">
      <div class="cstat-label">반대매매 ${liqHot ? '🔥' : ''}</div>
      <div class="cstat-val ${liqHot ? 'up' : ''}">${eok(mo.liquidation, { sign: false })}</div>
      <div class="cstat-sub">20일 평균 ${eok(mo.liqAvg20, { sign: false })} · 미수금 대비 ${mo.liqRatio ?? '—'}%</div>
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
  (D.market.indices.KOSPI.history || []).forEach(h => { closeBy[h.date] = h.close; });

  const W = 640, H = 190, M = { t: 12, r: 46, b: 24, l: 56 };
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
    g += `<text x="${M.l - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#5d6780"
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
  const step = Math.max(1, Math.round(rows.length / 5));
  rows.forEach((r, i) => {
    if (i % step && i !== rows.length - 1) return;
    g += `<text x="${X(i).toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="#5d6780"
           font-size="9.5" font-family="ui-monospace,monospace">${mdy(r.date)}</text>`;
  });
  const svg = $('#credit-chart');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;

  const trend = ln.d20 == null ? '' : ln.d20 > 0
    ? `최근 20거래일 새 ${eok(ln.d20)} 늘었습니다. 빚으로 산 물량은 하락장에서 반대매매로 되돌아옵니다.`
    : `최근 20거래일 새 ${eok(ln.d20)} — 레버리지가 정리되는 중입니다.`;
  $('#credit-note').innerHTML =
    `<span style="color:#ffb02e">━</span> 신용융자 잔고 · <span style="opacity:.5">┄</span> 코스피. ${trend}`;
}

function quip(k, grade) {
  const good = grade === 'A' || grade === 'B';
  if (k === 'individual') {
    return good
      ? '이 표본에서는 개미의 타이밍이 나쁘지 않았습니다. 흔한 일은 아니니 기록해 둡시다.'
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

function renderReportCard() {
  if (!hasAnt()) { $('#report-card').hidden = true; return; }
  const ant = D.ant, base = ant.baseline;
  $('#report-sample').textContent =
    `${ant.sample.from} ~ ${ant.sample.to} · ${ant.sample.days}거래일 · 코스피 기준`;

  const excesses = ACTOR_KEYS.map(k => ant.actors[k].excess20 ?? 0);
  const worst = Math.min(...excesses), best = Math.max(...excesses);

  const box = $('#grades');
  box.innerHTML = '';
  ACTOR_KEYS.forEach(k => {
    const a = ant.actors[k], A = ACTORS[k];
    const ex = a.excess20;
    const cls = ex === worst ? ' worst' : ex === best ? ' best' : '';
    box.appendChild(el('div', `grade-card${cls}`, `
      <div class="grade-top">
        <span class="grade-face">${A.face}</span>
        <span class="grade-who">${A.name}<small>크게 산 날 ${a.heavyBuy.n}일 기준</small></span>
        <span class="grade-letter g-${a.grade}">${a.grade}</span>
      </div>
      <div class="grade-rows">
        ${[1, 5, 20].map(h => `
          <div class="grade-row">
            <span>${h}거래일 뒤</span>
            <b class="${dirCls(a.heavyBuy['r' + h])}">${a.heavyBuy['r' + h] >= 0 ? '+' : ''}${a.heavyBuy['r' + h]}%</b>
          </div>`).join('')}
        <div class="grade-row">
          <span>시장 평균(20일)</span><b class="dim">${base.r20 >= 0 ? '+' : ''}${base.r20}%</b>
        </div>
      </div>
      <div class="grade-excess">
        <span>시장 대비</span>
        <b class="${dirCls(ex)}">${ex >= 0 ? '+' : ''}${ex}%p</b>
      </div>
      <p class="grade-quip">${quip(k, a.grade)}</p>`));
  });

  renderTimingChart();
}

/** 주체별 · 기간별 '크게 산 날 이후 수익률' 을 시장 평균선과 함께 */
function renderTimingChart() {
  const svg = $('#timing-chart'), ant = D.ant;
  const W = 900, H = 230, M = { t: 16, r: 16, b: 34, l: 56 };
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
    g += `<text x="${M.l - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="#5d6780"
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
             text-anchor="middle" fill="${ACTORS[k].raw}" font-size="10.5"
             font-family="ui-monospace,monospace">${v >= 0 ? '+' : ''}${v}</text>`;
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

/** 연도별 분해 — F학점이 장세 탓인지 실력 탓인지 보여준다 */
function renderYearly() {
  const box = $('#yearly');
  const rows = D.ant.yearly || [];
  if (rows.length < 2) { box.innerHTML = ''; return; }

  const gc = { A: '#4dd4ac', B: '#4dd4ac', C: '#7a869e', D: '#ffb02e', F: '#ff4d4d' };
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
          <td class="${dirCls(y.indivHeavyR20)}">${y.indivHeavyR20 >= 0 ? '+' : ''}${y.indivHeavyR20}%</td>
          <td class="dim">${y.baseline20 >= 0 ? '+' : ''}${y.baseline20}%</td>
          <td class="${dirCls(y.excess)}">${y.excess >= 0 ? '+' : ''}${y.excess}%p</td>
          <td class="yg" style="color:${gc[y.grade] || '#7a869e'}">${y.grade}</td>
        </tr>`).join('')}
      </tbody>
    </table>
    <p class="yearly-note">
      연도별로 쪼개 보면 개미의 성적은 해마다 다릅니다.
      "개미는 늘 틀린다"보다 정확한 문장은 "개미는 <b>급락하는 해에</b> 유독 크게 틀린다"입니다.
      반대로 가는 습관(상관계수)은 매년 그대로인데, 그 습관의 값이 해마다 다르게 청구되는 셈입니다.
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
      <div class="hall-amt">개미 ${eok(r.amount)} 매수</div>
      <div class="hall-ret ${dirCls(r.return20)}">${r.return20 >= 0 ? '+' : ''}${r.return20}%</div>`);
    box.appendChild(node);
    growIn(() => { $('.hall-bar', node).style.width = `${Math.max(half, 0.6)}%`; }, 70 + i * 65);
  });

  const losses = rows.filter(r => r.return20 < 0).length;
  const wins = rows.length - losses;
  const avg = rows.reduce((s, r) => s + r.return20, 0) / rows.length;
  $('#hall-verdict').hidden = false;
  $('#hall-verdict').innerHTML = losses > wins
    ? `개미가 가장 크게 질렀던 ${rows.length}번 중 <b>${losses}번이 손실</b>로 끝났습니다.
       평균 ${avg >= 0 ? '+' : ''}${avg.toFixed(2)}%.
       다만 ${wins}번은 잘 맞았다는 뜻이기도 합니다. 늘 틀리는 건 아닙니다, 자주 틀릴 뿐입니다.`
    : `의외로 ${rows.length}번 중 <b>${wins}번은 맞았습니다.</b>
       평균 ${avg >= 0 ? '+' : ''}${avg.toFixed(2)}%.
       "개미는 항상 틀린다"는 말이 항상 맞지는 않습니다.
       크게 지른 날 ${losses}번은 아팠지만, 나머지는 버텨냈습니다.`;
}

function renderCaveats() {
  const box = $('#caveats');
  const list = (D.ant && D.ant.caveats) || [];
  box.innerHTML = list.length
    ? list.map(c => `<li>${c}</li>`).join('')
    : '<li class="dim">표시할 내용이 없습니다.</li>';
}

/* ── 인사이트 ─────────────────────────────────────────── */

function renderInsights() {
  const box = $('#insights');
  const icons = { buy: '📈', sell: '📉', neutral: '🔎' };
  box.innerHTML = '';
  (D.insights.items || []).forEach((t, i) => {
    const n = el('div', `tip ${t.tone}`,
      `<span class="tip-icon">${icons[t.tone] || '🔎'}</span><span>${t.text}</span>`);
    n.style.animationDelay = `${i * 55}ms`;
    box.appendChild(n);
  });
  if (!box.children.length) box.innerHTML = '<p class="dim">표시할 브리핑이 없습니다.</p>';
}

/* ── 지수 카드 ────────────────────────────────────────── */

function renderIndices() {
  const box = $('#indices');
  box.innerHTML = '';
  Object.values(D.market.indices).forEach(ix => {
    const card = el('div', 'card');
    const hist = (ix.history || []).slice(-60).map(h => h.close);
    card.innerHTML = `
      <div class="idx">
        <div>
          <div class="idx-name">${ix.name}</div>
          <div class="idx-price">${nfmt(ix.price)}</div>
          <div class="idx-chg ${dirCls(ix.change)}">
            ${ix.change > 0 ? '▲' : ix.change < 0 ? '▼' : '─'}
            ${nfmt(Math.abs(ix.change || 0))} (${pct(ix.changeRate)})
          </div>
        </div>
        ${sparkSVG(hist, 'idx-spark', ix.changeRate)}
      </div>`;
    box.appendChild(card);
  });
}

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
  if (!mk) { svg.innerHTML = ''; return; }

  const rows = mk.daily.slice(-flowRange);
  const W = 900, H = 320, M = { t: 14, r: 58, b: 26, l: 62 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;

  // 지수 종가를 날짜로 맞춰 붙인다
  const closeBy = {};
  (D.market.indices[heroMarket].history || []).forEach(h => { closeBy[h.date] = h.close; });
  const closes = rows.map(r => closeBy[r.date] ?? null);

  // 누적은 보이는 구간의 시작점을 0 으로 다시 잡는다
  const series = ACTOR_KEYS.map(k => {
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
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = lo + (hi - lo) * i / ticks, y = Y(v);
    g += `<line x1="${M.l}" y1="${y.toFixed(1)}" x2="${W - M.r}" y2="${y.toFixed(1)}"
           stroke="#232b40" stroke-width="1"/>`;
    g += `<text x="${M.l - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end"
           fill="#5d6780" font-size="11" font-family="ui-monospace,monospace">${eok(v, { sign: false })}</text>`;
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
      g += `<text x="${W - M.r + 8}" y="${(CY(v) + 4).toFixed(1)}" fill="#6f7a93" font-size="10.5"
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
    const sub = Math.max(1.6, bw / 3 - 1.4);
    rows.forEach((r, i) => {
      ACTOR_KEYS.forEach((k, j) => {
        const v = r[k] ?? 0;
        const x = X(i) - bw / 2 + j * (bw / 3) + (bw / 3 - sub) / 2;
        const y0 = Y(0), y1 = Y(v);
        g += `<rect x="${x.toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}"
               width="${sub.toFixed(1)}" height="${Math.max(Math.abs(y1 - y0), 0.8).toFixed(1)}"
               fill="${ACTORS[k].raw}" opacity=".88" rx="1"/>`;
      });
    });
  }

  // 날짜 축
  const step = Math.max(1, Math.round(rows.length / 8));
  rows.forEach((r, i) => {
    if (i % step && i !== rows.length - 1) return;
    g += `<text x="${X(i).toFixed(1)}" y="${H - 8}" text-anchor="middle"
           fill="#5d6780" font-size="10.5" font-family="ui-monospace,monospace">${mdy(r.date)}</text>`;
  });

  // 호버 레이어
  g += `<rect id="flow-hit" x="${M.l}" y="${M.t}" width="${pw}" height="${ph}" fill="transparent"/>`;
  g += `<line id="flow-cursor" y1="${M.t}" y2="${M.t + ph}" stroke="#ffffff" stroke-opacity=".22"
         stroke-width="1" visibility="hidden"/>`;

  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.innerHTML = g;

  $('#flow-legend').innerHTML =
    ACTOR_KEYS.map(k => `<span><i style="background:${ACTORS[k].raw}"></i>${ACTORS[k].name}</span>`).join('') +
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

  hit.addEventListener('mousemove', move);
  hit.addEventListener('touchmove', e => { move(e.touches[0]); e.preventDefault(); }, { passive: false });
  const leave = () => { tip.hidden = true; cursor.setAttribute('visibility', 'hidden'); };
  hit.addEventListener('mouseleave', leave);
  hit.addEventListener('touchend', leave);
}

/* ── 연속 매매 ────────────────────────────────────────── */

function renderStreaks() {
  const mk = D.flows.markets[heroMarket];
  const box = $('#streaks');
  box.innerHTML = '';
  if (!mk) return;

  const recent = mk.daily.slice(-10);
  ACTOR_KEYS.forEach(k => {
    const s = mk.streaks[k], a = ACTORS[k];
    const buying = s.side === 'buy';
    const label = s.days === 0 ? '보합' : `${s.days}일 연속 ${buying ? '순매수' : '순매도'}`;
    const dots = recent.map(r => {
      const v = r[k] ?? 0;
      const c = v > 0 ? '#ff4d4d' : v < 0 ? '#4d94ff' : '#252d42';
      return `<i style="background:${c}"></i>`;
    }).join('');

    box.appendChild(el('div', 'streak', `
      <div class="streak-face">${a.face}</div>
      <div class="streak-body">
        <div class="streak-title">${a.name} · <span class="${buying ? 'up' : 'down'}">${label}</span></div>
        <div class="streak-sub">기간 누적 ${eok(s.total)}</div>
        <div class="dots" title="최근 10거래일 (빨강=순매수, 파랑=순매도)">${dots}</div>
      </div>
      <div class="streak-count ${buying ? 'up' : 'down'}">${s.days}<small>일</small></div>`));
  });
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

    const totalInMarket = (D.stocks.top || []).filter(s => (s.market || 'KOSPI') === treeMarket).length;
    const cap = $('#treemap-caption');
    if (cap) cap.textContent =
      `${treeMarket === 'KOSPI' ? '코스피' : '코스닥'} 시총 상위 ${Math.min(limit, items.length)}종목 표시` +
      ` · 검색하면 수집된 ${totalInMarket}종목 전부에서 찾습니다`;

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
      tile.innerHTML = `<div class="tile-name">${t.name}</div>
                        <div class="tile-chg">${pct(t.changeRate)}</div>`;
      tile.title = `${t.name}  ${nfmt(t.price, 0)}원  ${pct(t.changeRate)}\n시총 ${t.marketCapText || ''}`;
      tile.addEventListener('click', () => showStock(t.code));
      box.appendChild(tile);
    });
  };

  draw();
  clearTimeout(renderTreemap._t);
  if (!renderTreemap._bound) {
    renderTreemap._bound = true;
    window.addEventListener('resize', () => {
      clearTimeout(renderTreemap._t);
      renderTreemap._t = setTimeout(draw, 180);
    });
  }
}

function showStock(code) {
  const s = (D.stocks.top || []).find(x => x.code === code);
  const box = $('#stock-detail');
  if (!s) { box.hidden = true; return; }
  selectedStock = code;
  $$('.tile').forEach(t => t.classList.remove('sel'));

  const flow = s.flow || [];
  const max = Math.max(...flow.flatMap(d => ACTOR_KEYS.map(k => Math.abs(d[k] ?? 0))), 1);

  box.hidden = false;
  box.innerHTML = `
    <div class="sd-head">
      <span class="sd-name">${s.name}</span>
      <span class="sd-price ${dirCls(s.changeRate)}">${nfmt(s.price, 0)}원 ${pct(s.changeRate)}</span>
      <span class="sd-meta">시총 ${s.marketCapText || '—'}${
        s.foreignHoldRatio != null ? ` · 외국인 보유 ${s.foreignHoldRatio}%` : ''}</span>
    </div>
    <div class="sd-flow">${
      flow.length ? flow.map(d => `
        <div class="sd-day">
          <span class="sd-date">${d.date ? `${d.date.slice(4, 6)}/${d.date.slice(6, 8)}` : ''}</span>
          <div class="sd-bars">${ACTOR_KEYS.map(k => {
            const v = d[k] ?? 0;
            return `<div class="sd-seg" title="${ACTORS[k].name} ${shares(v)}"
                      style="background:${ACTORS[k].raw};opacity:${v >= 0 ? 1 : .42};
                             width:${(Math.abs(v) / max * 100).toFixed(1)}%"></div>`;
          }).join('')}</div>
        </div>`).join('')
      : '<p class="dim">이 종목의 수급 데이터가 없습니다.</p>'}
    </div>
    <div class="sd-legend">${
      ACTOR_KEYS.map(k => `<span><i style="background:${ACTORS[k].raw}"></i>${ACTORS[k].name}</span>`).join('')
    }<span class="dim">진한 색 = 순매수, 흐린 색 = 순매도 · 막대 길이 = 수량</span></div>`;

  $$('.tile').forEach(t => {
    if (t.title.startsWith(s.name)) t.classList.add('sel');
  });
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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
          <div class="bar-label">${g.name} <span class="dim">${g.rise}▲ ${g.fall}▼</span></div>
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

const GLOBAL_HINT = {
  '필라델피아 반도체': '한국 반도체 대형주와 가장 밀접한 지수',
  '원/달러 환율': '높을수록 외국인 순매도 압력',
  'VIX 공포지수': '20 넘으면 경계, 30 넘으면 공포',
  '미국 10년물 금리': '오르면 성장주에 부담',
  'S&P500 선물': '한국 장중 미국 분위기 미리보기',
  '나스닥100 선물': '한국 장중 기술주 분위기 미리보기',
};

function renderGlobals() {
  const box = $('#globals');
  box.innerHTML = '';
  (D.global.items || []).forEach(g => {
    const digits = Math.abs(g.price) >= 1000 ? 2 : (Math.abs(g.price) < 10 ? 2 : 2);
    const n = el('div', 'gitem', `
      <div class="gitem-l">
        <div class="gitem-name">${g.name}</div>
        <div class="gitem-price">${nfmt(g.price, digits)}</div>
        <div class="gitem-chg ${dirCls(g.changeRate)}">${pct(g.changeRate)}</div>
      </div>
      ${sparkSVG(g.spark, 'gitem-spark', g.changeRate)}`);
    n.title = `${g.name} (${g.symbol}) · ${g.asOf} 기준` +
              (GLOBAL_HINT[g.name] ? `\n${GLOBAL_HINT[g.name]}` : '');
    box.appendChild(n);
  });

  const mac = $('#macro');
  const items = D.global.macro || [];
  mac.innerHTML = items.length
    ? items.map(m => `<div class="mitem">${m.name}<b>${m.value}${m.unit}</b><small>${m.asOf}</small></div>`).join('')
    : '';
}

/* ── 이벤트 ───────────────────────────────────────────── */

function renderEvents() {
  const box = $('#events');
  const list = D.events.upcoming || [];
  box.innerHTML = '';
  if (!list.length) { box.innerHTML = '<p class="dim">예정된 이벤트가 없습니다.</p>'; return; }

  list.forEach(e => {
    const cls = e.dday === 0 ? 'today' : e.dday <= 7 ? 'soon' : '';
    box.appendChild(el('div', `event ${cls}`, `
      <div class="event-dday">${e.dday === 0 ? 'D-DAY' : `D-${e.dday}`}</div>
      <div class="event-body">
        <div class="event-title">${e.title}</div>
        <div class="event-date">${e.date}</div>
        ${e.note ? `<div class="event-note">${e.note}</div>` : ''}
      </div>`));
  });
}

/* ── 컨트롤 ───────────────────────────────────────────── */

function wireControls() {
  $$('#hero-market-seg button').forEach(b => b.addEventListener('click', () => {
    $$('#hero-market-seg button').forEach(x => x.classList.toggle('on', x === b));
    heroMarket = b.dataset.market;
    renderHero(); renderFlowChart(); renderStreaks(); renderInstBreakdown();
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

  const search = q => {
    q = q.trim().toLowerCase();
    if (!q) { close(); return; }
    const hits = (D.stocks.top || []).filter(s =>
      s.name.toLowerCase().includes(q) || s.code === q
    ).slice(0, 8);
    if (!hits.length) {
      list.hidden = false;
      list.innerHTML = `<div class="sr-item" style="cursor:default;color:var(--dimmer)">
        "${q}" — 수집된 ${(D.stocks.top || []).length}종목 안에 없습니다 (시총 상위만 수집합니다)</div>`;
      return;
    }
    list.hidden = false;
    list.innerHTML = '';
    hits.forEach(s => {
      const item = el('button', 'sr-item', `
        <span class="sr-name">${highlight(s.name, q)}
          <span class="sr-meta">${s.market === 'KOSDAQ' ? '코스닥' : '코스피'} · ${s.code}</span></span>
        <span class="sr-chg ${dirCls(s.changeRate)}">${nfmt(s.price, 0)} · ${pct(s.changeRate)}</span>`);
      item.addEventListener('click', () => {
        close();
        input.value = s.name;
        if ((s.market || 'KOSPI') !== treeMarket) {
          treeMarket = s.market || 'KOSPI';
          $$('#tree-market-seg button').forEach(x =>
            x.classList.toggle('on', x.dataset.tmarket === treeMarket));
          renderTreemap();
        }
        showStock(s.code);
      });
      list.appendChild(item);
    });
  };

  const highlight = (name, q) => {
    const i = name.toLowerCase().indexOf(q);
    if (i < 0) return name;
    return `${name.slice(0, i)}<b>${name.slice(i, i + q.length)}</b>${name.slice(i + q.length)}`;
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

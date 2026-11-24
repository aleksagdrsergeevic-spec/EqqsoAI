/* EqqsoAI front-end: one document, two views (landing and cabinet). */

const SESSION_KEY = 'wan-session';

const state = {
  session: null,
  models: [],
  account: null,
  family: 'Все',
  search: '',
  showAll: false,
  cabFamily: 'Все',
  cabSearch: '',
  opsFilter: '',
  authMode: 'login',
  codeTab: 'curl',
};

const $ = (id) => document.getElementById(id);

/* The cookie is the primary session. Telegram's web view and the desktop web
   client both open the site in an iframe where cookies can be dropped, so the
   token is mirrored to localStorage and sent back as a header. */
function sessionToken() {
  try { return localStorage.getItem(SESSION_KEY) || ''; } catch (e) { return ''; }
}

function storeSession(token) {
  try {
    if (token) localStorage.setItem(SESSION_KEY, token);
    else localStorage.removeItem(SESSION_KEY);
  } catch (e) { /* private mode */ }
}

function telegram() {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (!tg || !tg.platform || tg.platform === 'unknown') return null;
  return tg;
}

function initTelegram() {
  const tg = telegram();
  if (!tg) return;
  try {
    tg.ready();
    tg.expand();
    if (tg.setHeaderColor) tg.setHeaderColor('#08060f');
    if (tg.setBackgroundColor) tg.setBackgroundColor('#08060f');
  } catch (e) { /* older clients */ }

  if (tg.BackButton) {
    tg.BackButton.onClick(() => {
      if (!$('cabinet').classList.contains('hidden')) showCabinet(false);
      else tg.close();
    });
  }
}

function toast(message, bad = false) {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('bad', bad);
  el.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add('hidden'), 2400);
}

async function api(path, body) {
  const options = { headers: {} };
  const token = sessionToken();
  if (token) options.headers['X-Session-Token'] = token;
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try {
    data = await response.json();
  } catch (e) {
    data = null;
  }
  if (!response.ok) {
    const message = (data && data.error && data.error.message) || ('Ошибка ' + response.status);
    throw new Error(message);
  }
  return data || {};
}

function copyText(text) {
  if (!text) return;
  const done = () => toast('Скопировано');
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, () => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}

function fallbackCopy(text, done) {
  const area = document.createElement('textarea');
  area.value = text;
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.appendChild(area);
  area.select();
  try { document.execCommand('copy'); done(); } catch (e) { toast('Не удалось скопировать', true); }
  document.body.removeChild(area);
}

/* ------------------------------ formatting ------------------------------ */

function num(value, digits = 2) {
  const n = Number(value) || 0;
  const fixed = n.toFixed(digits);
  return fixed.replace(/\.?0+$/, '') || '0';
}

function money(value) {
  const n = Number(value) || 0;
  if (n >= 1000) return Math.round(n).toLocaleString('ru-RU');
  if (n >= 10) return num(n, 1);
  return num(n, 3);
}

function when(ts) {
  const date = new Date(ts * 1000);
  const pad = (v) => String(v).padStart(2, '0');
  return pad(date.getDate()) + '.' + pad(date.getMonth() + 1) + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

/* ------------------------------ models ------------------------------ */

function families() {
  const seen = [];
  state.models.forEach((m) => { if (!seen.includes(m.family)) seen.push(m.family); });
  return seen;
}

function renderPills(target, active, onPick, allLabel = 'Все') {
  target.innerHTML = '';
  [allLabel].concat(families()).forEach((name) => {
    const button = document.createElement('button');
    button.className = 'pill' + (name === active ? ' active' : '');
    button.textContent = name;
    button.addEventListener('click', () => onPick(name));
    target.appendChild(button);
  });
}

function modelCard(model) {
  const card = document.createElement('article');
  card.className = 'model-card' + (model.available ? '' : ' dim');

  const top = document.createElement('div');
  top.className = 'mc-top';
  const family = document.createElement('span');
  family.className = 'mc-family' + (model.family === 'Claude' ? ' serif' : '');
  family.textContent = model.family;
  const mult = document.createElement('span');
  mult.className = 'mc-mult';
  mult.textContent = '×' + num(model.multiplier, 2);
  top.append(family, mult);

  const id = document.createElement('div');
  id.className = 'mc-id';
  id.textContent = model.id;

  const label = document.createElement('div');
  label.className = 'mc-label';
  label.textContent = model.available ? model.label : model.label + ' · маршрут не подключён';

  const foot = document.createElement('div');
  foot.className = 'mc-foot';
  const copy = document.createElement('button');
  copy.className = 'link-gold';
  copy.textContent = 'Копировать ID';
  copy.addEventListener('click', () => copyText(model.id));
  foot.appendChild(copy);

  card.append(top, id, label, foot);
  return card;
}

function renderModels() {
  const grid = $('modelGrid');
  grid.innerHTML = '';

  const filtered = state.models.filter((m) => {
    if (state.family !== 'Все' && m.family !== state.family) return false;
    if (state.search && !m.id.toLowerCase().includes(state.search.toLowerCase())) return false;
    return true;
  });

  const visible = state.showAll ? filtered : filtered.slice(0, 8);
  visible.forEach((m) => grid.appendChild(modelCard(m)));

  if (!visible.length) {
    grid.innerHTML = '<p class="muted">Ничего не нашлось. Измените фильтр или поиск.</p>';
  }

  $('modelCount').textContent = 'Показано ' + visible.length + ' из ' + filtered.length;
  const toggle = $('toggleAll');
  toggle.textContent = state.showAll ? 'Свернуть список ↑' : 'Показать все модели ↓';
  toggle.classList.toggle('hidden', filtered.length <= 8);
}

function renderCabinetModels() {
  const grid = $('cabModelGrid');
  grid.innerHTML = '';
  const filtered = state.models.filter((m) => {
    if (state.cabFamily !== 'Все' && m.family !== state.cabFamily) return false;
    if (state.cabSearch && !m.id.toLowerCase().includes(state.cabSearch.toLowerCase())) return false;
    return true;
  });
  filtered.forEach((m) => grid.appendChild(modelCard(m)));
  if (!filtered.length) grid.innerHTML = '<p class="muted">Ничего не нашлось.</p>';
}

/* ------------------------------ code samples ------------------------------ */

function samples() {
  const base = (state.session && state.session.base_url) || 'http://localhost:8000/v1';
  const model = (state.session && state.session.default_model) || 'gpt-oss-120b';
  return {
    curl: 'curl ' + base + '/chat/completions \\\n'
      + '  -H "Authorization: Bearer $FG_API_KEY" \\\n'
      + '  -H "Content-Type: application/json" \\\n'
      + '  -d \'{"model":"' + model + '","messages":[{"role":"user","content":"Привет"}]}\'',
    cursor: 'Settings → Models → OpenAI API Key\n'
      + 'Base URL: ' + base + '\n'
      + 'API Key:  sk-…\n'
      + 'Model:    ' + model,
    cline: 'Provider:  OpenAI Compatible\n'
      + 'Base URL:  ' + base + '\n'
      + 'API Key:   ${FG_API_KEY}\n'
      + 'Model ID:  ' + model,
  };
}

function renderCode() {
  $('codeBody').textContent = samples()[state.codeTab];
}

/* ------------------------------ cabinet ------------------------------ */

function renderAccount() {
  const account = state.account;
  if (!account) return;

  $('avatar').textContent = (account.login || '?').slice(0, 1).toUpperCase();
  $('whoLogin').textContent = account.login;
  $('balanceValue').textContent = money(account.balance);
  $('cabinetBase').textContent = account.base_url;

  const keys = $('keyList');
  keys.innerHTML = '';
  account.keys.forEach((key) => {
    const row = document.createElement('div');
    row.className = 'key-row';
    const code = document.createElement('code');
    code.textContent = key.key;
    const copy = document.createElement('button');
    copy.className = 'link-gold';
    copy.textContent = 'Копировать';
    copy.addEventListener('click', () => copyText(key.key));
    const revoke = document.createElement('button');
    revoke.className = 'link-gold';
    revoke.textContent = 'Отозвать';
    revoke.addEventListener('click', () => revokeKey(key.id));
    row.append(code, copy, revoke);
    keys.appendChild(row);
  });
  if (!account.keys.length) {
    keys.innerHTML = '<p class="muted small">Ключей нет. Создайте первый.</p>';
  }

  const stats = account.stats;
  $('spendTotal').textContent = money(stats.total);
  $('spend24').textContent = money(stats.day_total);
  $('spendReqs').textContent = stats.requests;
  $('spendAvg').textContent = stats.requests ? money(stats.total / stats.requests) : '0';
  renderChart($('overviewChart'), stats.daily);

  $('hTotal').textContent = money(stats.total);
  $('hDay').textContent = money(stats.day_total);
  $('hDayReqs').textContent = stats.day_requests;
  $('hReqs').textContent = stats.requests;
  $('hAvg').textContent = stats.requests ? money(stats.total / stats.requests) : '0';
  $('hMax').textContent = money(stats.max_request);
  renderChart($('historyChart'), stats.daily);

  const byModel = $('byModel');
  byModel.innerHTML = '';
  if (!stats.by_model.length) {
    byModel.innerHTML = '<p class="muted small">Пока нет данных</p>';
  } else {
    stats.by_model.forEach((row) => {
      const el = document.createElement('div');
      el.className = 'bm-row';
      const name = document.createElement('b');
      name.textContent = row.model;
      const right = document.createElement('span');
      right.textContent = money(row.cost) + ' TKN · ' + row.requests + ' запр.';
      el.append(name, right);
      byModel.appendChild(el);
    });
  }

  renderOps();
}

function renderChart(el, daily) {
  el.innerHTML = '';
  const max = Math.max.apply(null, daily.map((d) => d.cost).concat([0]));
  if (max <= 0) {
    const empty = document.createElement('span');
    empty.className = 'empty';
    empty.textContent = 'За этот период списаний нет';
    el.appendChild(empty);
    return;
  }
  daily.forEach((day) => {
    const bar = document.createElement('div');
    bar.className = 'bar';
    bar.style.height = Math.max(4, Math.round((day.cost / max) * 100)) + '%';
    bar.title = money(day.cost) + ' TKN';
    el.appendChild(bar);
  });
}

function renderOps() {
  const body = $('opsBody');
  body.innerHTML = '';
  const recent = (state.account ? state.account.stats.recent : []).filter((row) => {
    return !state.opsFilter || row.model.toLowerCase().includes(state.opsFilter.toLowerCase());
  });

  if (!recent.length) {
    const tr = document.createElement('tr');
    const td = document.createElement('td');
    td.colSpan = 4;
    td.className = 'empty';
    td.textContent = 'Запросов пока нет';
    tr.appendChild(td);
    body.appendChild(tr);
  } else {
    const labels = ['Модель', 'Вход / выход', 'Время', 'Списано'];
    recent.forEach((row) => {
      const tr = document.createElement('tr');
      const cells = [
        row.model,
        Math.round(row.prompt_tokens) + ' / ' + Math.round(row.completion_tokens),
        when(row.at),
        money(row.cost) + ' TKN',
      ];
      cells.forEach((text, index) => {
        const td = document.createElement('td');
        td.textContent = text;
        td.dataset.label = labels[index];
        if (index === 3) td.className = 'right';
        tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  }
  $('opsCount').textContent = recent.length + ' запросов';
}

async function loadAccount() {
  state.account = await api('/api/account');
  renderAccount();
}

async function refreshKey() {
  try {
    const data = await api('/api/keys', { label: 'новый' });
    state.account = data.account;
    renderAccount();
    toast('Ключ создан');
  } catch (error) {
    toast(error.message, true);
  }
}

async function revokeKey(id) {
  try {
    const data = await api('/api/keys/' + id + '/revoke', {});
    state.account = data.account;
    renderAccount();
    toast('Ключ отозван');
  } catch (error) {
    toast(error.message, true);
  }
}

async function redeem() {
  const input = $('codeInput');
  const error = $('redeemError');
  error.textContent = '';
  try {
    const data = await api('/api/redeem', { code: input.value });
    state.account = data.account;
    renderAccount();
    input.value = '';
    toast('Начислено ' + money(data.amount) + ' TKN');
  } catch (e) {
    error.textContent = e.message;
  }
}

/* ------------------------------ views ------------------------------ */

function showCabinet(active) {
  $('cabinet').classList.toggle('hidden', !active);
  document.querySelector('main').classList.toggle('hidden', active);
  document.querySelector('.ftr').classList.toggle('hidden', active);
  $('loginBtn').classList.toggle('hidden', Boolean(state.session && state.session.authenticated));
  $('userActions').classList.toggle('hidden', !(state.session && state.session.authenticated));

  const tg = telegram();
  if (tg && tg.BackButton) {
    if (active) tg.BackButton.show();
    else tg.BackButton.hide();
  }
  window.scrollTo({ top: 0 });
}

function switchTab(name) {
  document.querySelectorAll('#cabinetTabs .pill').forEach((button) => {
    button.classList.toggle('active', button.dataset.tab === name);
  });
  ['overview', 'models', 'history'].forEach((tab) => {
    $('tab-' + tab).classList.toggle('hidden', tab !== name);
  });
  if (name === 'models') renderCabinetModels();
}

/* ------------------------------ auth ------------------------------ */

function openAuth(mode) {
  state.authMode = mode || 'login';
  $('authError').textContent = '';
  $('authLogin').value = '';
  $('authPassword').value = '';
  setAuthMode(state.authMode);
  $('authModal').classList.remove('hidden');
  setTimeout(() => $('authLogin').focus(), 40);
}

function setAuthMode(mode) {
  state.authMode = mode;
  $('authTitle').textContent = mode === 'register' ? 'Создать аккаунт' : 'Войти в кабинет';
  $('authSubmit').textContent = mode === 'register' ? 'Создать аккаунт' : 'Войти';
  document.querySelectorAll('#authSeg .seg-btn').forEach((button) => {
    button.classList.toggle('active', button.dataset.mode === mode);
  });
  $('authPassword').autocomplete = mode === 'register' ? 'new-password' : 'current-password';
}

async function submitAuth() {
  const error = $('authError');
  error.textContent = '';
  const login = $('authLogin').value.trim();
  const password = $('authPassword').value;
  if (login.length < 3 || login.length > 64) {
    error.textContent = 'Логин должен содержать от 3 до 64 символов';
    return;
  }
  if (password.length < 8 || password.length > 256) {
    error.textContent = 'Пароль должен содержать от 8 до 256 символов';
    return;
  }
  try {
    const data = await api('/api/' + (state.authMode === 'register' ? 'register' : 'login'), { login, password });
    if (data.token) storeSession(data.token);
    $('authModal').classList.add('hidden');
    await boot();
    showCabinet(true);
    toast(state.authMode === 'register' ? 'Аккаунт создан' : 'С возвращением');
  } catch (e) {
    error.textContent = e.message;
  }
}

function openLogout() {
  const button = $('logoutConfirmBtn');
  const hint = $('logoutHint');
  const check = $('logoutConfirm');
  check.checked = false;
  button.disabled = true;
  hint.textContent = 'Подтверждение станет доступно через 5 секунд.';
  $('logoutModal').classList.remove('hidden');

  let left = 5;
  clearInterval(openLogout._timer);
  openLogout._timer = setInterval(() => {
    left -= 1;
    if (left > 0) {
      hint.textContent = 'Подтверждение станет доступно через ' + left + ' с.';
    } else {
      hint.textContent = 'Теперь можно выйти.';
      button.disabled = false;
      clearInterval(openLogout._timer);
    }
  }, 1000);
}

async function doLogout() {
  if (!$('logoutConfirm').checked) {
    toast('Отметьте, что помните логин и пароль', true);
    return;
  }
  try {
    await api('/api/logout', {});
  } catch (e) {
    /* the cookie is cleared either way */
  }
  $('logoutModal').classList.add('hidden');
  storeSession('');
  state.account = null;
  await boot();
  showCabinet(false);
  toast('Вы вышли');
}

/* ------------------------------ boot ------------------------------ */

function renderFamilyPills() {
  renderPills($('familyFilters'), state.family, (name) => {
    state.family = name;
    renderFamilyPills();
    renderModels();
  });
}

async function boot() {
  const [session, models] = await Promise.all([api('/api/session'), api('/api/models')]);
  state.session = session;
  state.models = models.models || [];

  $('baseUrl').textContent = session.base_url;
  $('heroCta').textContent = session.authenticated ? 'Открыть кабинет ↗' : 'Создать аккаунт ↗';
  $('heroCta2').textContent = session.authenticated ? 'Открыть кабинет ↗' : 'Перейти в кабинет ↗';

  renderFamilyPills();
  renderCabinetPills();
  renderModels();
  renderCabinetModels();
  renderCode();

  if (session.authenticated) {
    try {
      await loadAccount();
    } catch (e) {
      state.account = null;
    }
  }
}

function renderCabinetPills() {
  renderPills($('cabFamilyFilters'), state.cabFamily, (name) => {
    state.cabFamily = name;
    renderCabinetPills();
    renderCabinetModels();
  });
}

function bind() {
  document.querySelectorAll('[data-copy]').forEach((button) => {
    button.addEventListener('click', () => {
      const target = $(button.dataset.copy);
      copyText(target ? target.textContent : '');
    });
  });

  document.querySelectorAll('[data-close]').forEach((button) => {
    button.addEventListener('click', () => $(button.dataset.close).classList.add('hidden'));
  });

  document.querySelectorAll('[data-goto]').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.goto));
  });

  $('loginBtn').addEventListener('click', () => openAuth('login'));
  $('heroCta').addEventListener('click', () => {
    if (state.session && state.session.authenticated) showCabinet(true);
    else openAuth('register');
  });
  $('heroCta2').addEventListener('click', () => {
    if (state.session && state.session.authenticated) showCabinet(true);
    else openAuth('register');
  });
  $('cabinetBtn').addEventListener('click', () => showCabinet(true));
  $('logoutBtn').addEventListener('click', openLogout);

  document.querySelectorAll('#authSeg .seg-btn').forEach((button) => {
    button.addEventListener('click', () => setAuthMode(button.dataset.mode));
  });
  $('authSubmit').addEventListener('click', submitAuth);
  $('authPassword').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') submitAuth();
  });

  $('logoutConfirmBtn').addEventListener('click', doLogout);

  $('toggleAll').addEventListener('click', () => {
    state.showAll = !state.showAll;
    renderModels();
  });

  $('modelSearch').addEventListener('input', (event) => {
    state.search = event.target.value;
    renderModels();
  });

  $('cabModelSearch').addEventListener('input', (event) => {
    state.cabSearch = event.target.value;
    renderCabinetModels();
  });

  document.querySelectorAll('#cabinetTabs .pill').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });

  document.querySelectorAll('#codeTabs .tab').forEach((button) => {
    button.addEventListener('click', () => {
      state.codeTab = button.dataset.tab;
      document.querySelectorAll('#codeTabs .tab').forEach((other) => {
        other.classList.toggle('active', other === button);
      });
      renderCode();
    });
  });

  $('copyCode').addEventListener('click', () => copyText($('codeBody').textContent));
  $('newKeyBtn').addEventListener('click', refreshKey);
  $('redeemBtn').addEventListener('click', redeem);
  $('topUpBtn').addEventListener('click', () => {
    $('codeInput').focus();
    $('codeInput').scrollIntoView({ block: 'center', behavior: 'smooth' });
  });
  $('opsRefresh').addEventListener('click', () => {
    loadAccount().then(() => toast('Обновлено')).catch((e) => toast(e.message, true));
  });
  $('opsFilter').addEventListener('input', (event) => {
    state.opsFilter = event.target.value;
    renderOps();
  });
}

(function start() {
  initTelegram();
  bind();
  boot().catch((error) => toast(error.message, true));
})();

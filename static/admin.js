const ADMIN_SESSION_KEY = 'eqqsoai-admin-session';
const $ = (id) => document.getElementById(id);

function adminSessionToken(token) {
  try {
    if (token) sessionStorage.setItem(ADMIN_SESSION_KEY, token);
    else sessionStorage.removeItem(ADMIN_SESSION_KEY);
  } catch (e) { /* private mode */ }
}

function showAdmin(authenticated) {
  $('loginPanel').classList.toggle('hidden', authenticated);
  $('adminPanel').classList.toggle('hidden', !authenticated);
  $('logoutButton').classList.toggle('hidden', !authenticated);
}

function message(target, text, bad = false) {
  target.textContent = text || '';
  target.classList.toggle('bad', bad);
}

function toast(text, bad = false) {
  const target = $('toast');
  target.textContent = text;
  target.classList.toggle('bad', bad);
  target.classList.remove('hidden');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => target.classList.add('hidden'), 2500);
}

async function api(path, body) {
  const options = { credentials: 'same-origin', headers: {} };
  try {
    const token = sessionStorage.getItem(ADMIN_SESSION_KEY);
    if (token) options.headers['X-Admin-Session'] = token;
  } catch (e) { /* cookie auth remains available */ }
  if (body !== undefined) {
    options.method = 'POST';
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch (e) { /* non-JSON error */ }
  if (!response.ok) {
    throw new Error((data.error && data.error.message) || ('Ошибка ' + response.status));
  }
  return data;
}

function formatAmount(value) {
  return Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 });
}

function formatDate(value) {
  if (!value) return '—';
  return new Date(Number(value) * 1000).toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const input = document.createElement('textarea');
  input.value = text;
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  const copied = document.execCommand('copy');
  input.remove();
  if (!copied) throw new Error('Не удалось скопировать код');
}

function addCell(row, text, className) {
  const cell = document.createElement('td');
  if (className) cell.className = className;
  cell.textContent = text;
  row.appendChild(cell);
  return cell;
}

function renderCodes(codes) {
  const body = $('codesBody');
  body.replaceChildren();
  $('emptyCodes').classList.toggle('hidden', codes.length > 0);

  codes.forEach((item) => {
    const row = document.createElement('tr');
    addCell(row, item.code, 'code-cell');
    addCell(row, formatAmount(item.amount) + ' TKN');
    addCell(row, formatDate(item.created_at));
    addCell(row, item.used_by ? 'Активирован · ' + item.used_by : 'Не использован', item.used_by ? 'status-used' : 'status-ready');
    const actions = document.createElement('td');
    if (!item.used_by) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copy-button';
      button.textContent = 'Копировать';
      button.addEventListener('click', () => copyText(item.code).then(() => toast('Код скопирован')).catch((error) => toast(error.message, true)));
      actions.appendChild(button);
    }
    row.appendChild(actions);
    body.appendChild(row);
  });
}

async function loadCodes() {
  const data = await api('/api/admin/codes?limit=100');
  renderCodes(data.codes || []);
}

function renderIssued(codes) {
  const list = $('issuedList');
  list.replaceChildren();
  codes.forEach((item) => {
    const row = document.createElement('li');
    const code = document.createElement('code');
    code.textContent = item.code;
    const amount = document.createElement('span');
    amount.className = 'muted';
    amount.textContent = formatAmount(item.amount) + ' TKN';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'copy-button';
    copy.textContent = 'Копировать';
    copy.addEventListener('click', () => copyText(item.code).then(() => toast('Код скопирован')).catch((error) => toast(error.message, true)));
    row.append(code, amount, copy);
    list.appendChild(row);
  });
  $('issuedPanel').classList.remove('hidden');
}

$('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('loginButton');
  button.disabled = true;
  message($('loginMessage'), '');
  try {
    const data = await api('/api/admin/login', { password: $('adminPassword').value });
    adminSessionToken(data.token);
    $('adminPassword').value = '';
    await loadCodes();
    showAdmin(true);
  } catch (error) {
    message($('loginMessage'), error.message, true);
  } finally {
    button.disabled = false;
  }
});

$('codeForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('createButton');
  button.disabled = true;
  message($('createMessage'), '');
  try {
    const data = await api('/api/admin/codes', {
      amount: Number($('amount').value),
      count: Number($('count').value),
    });
    renderIssued(data.codes || []);
    await loadCodes();
    message($('createMessage'), 'Создано кодов: ' + (data.codes || []).length);
  } catch (error) {
    message($('createMessage'), error.message, true);
  } finally {
    button.disabled = false;
  }
});

$('copyAllButton').addEventListener('click', async () => {
  const codes = Array.from($('issuedList').querySelectorAll('li code')).map((node) => node.textContent);
  if (!codes.length) return;
  try {
    await copyText(codes.join('\n'));
    toast('Все коды скопированы');
  } catch (error) {
    toast(error.message, true);
  }
});

$('refreshButton').addEventListener('click', () => loadCodes().then(() => toast('Список обновлён')).catch((error) => toast(error.message, true)));

$('logoutButton').addEventListener('click', async () => {
  try { await api('/api/admin/logout', {}); } catch (e) { /* clear local view regardless */ }
  adminSessionToken('');
  showAdmin(false);
  $('loginMessage').textContent = '';
});

(async function start() {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg && tg.platform && tg.platform !== 'unknown') {
    try { tg.ready(); tg.expand(); } catch (e) { /* older clients */ }
  }
  try {
    const data = await api('/api/admin/session');
    showAdmin(Boolean(data.authenticated));
    if (data.authenticated) await loadCodes();
    else adminSessionToken('');
  } catch (error) {
    showAdmin(false);
    message($('loginMessage'), error.message, true);
  }
})();

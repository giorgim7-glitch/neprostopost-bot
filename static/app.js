'use strict';
// Empty setting uses this website; standalone HTML sets the backend HTTPS origin.
const API_ORIGIN = new URL(document.querySelector('meta[name="neprostopost-api"]')?.content || location.origin).origin;
let sessionToken = sessionStorage.getItem('neprostopost.session') || '';
let authenticated = false;
let catalogItems = [];
let imageObjects = [];
let discoveryAttempted = false;
let discoveryMarker = null;
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dateLabel = value => new Date(value).toLocaleString('ru-RU', {dateStyle:'short',timeStyle:'short'});
function notice(message, error = false) {
    $('appNotice').hidden = !message;
    $('appNotice').textContent = message;
    $('appNotice').style.background = error ? '#FEE2E2' : '#CCFBF1';
}
async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (sessionToken) headers.set('Authorization', 'Bearer ' + sessionToken);
    const response = await fetch(API_ORIGIN + '/api' + path, {...options, headers});
    const data = await response.json().catch(() => ({message:'Некорректный ответ сервера'}));
    if (!response.ok) {
        if (response.status === 401) {
            authenticated = false;
            sessionToken = '';
            sessionStorage.removeItem('neprostopost.session');
            notice(data.message || 'Откройте приложение заново через MAX.', true);
        }
        throw new Error(data.message || `Ошибка ${response.status}`);
    }
    return data;
}
function requireLogin() {
    if (!authenticated) { notice('Откройте Mini App из бота MAX, чтобы войти.', true); return false; }
    return true;
}
async function action(fn) {
    if (!requireLogin()) return;
    const buttons = [...document.querySelectorAll('button')];
    buttons.forEach(b => b.disabled = true);
    try { await fn(); } catch (error) { notice(error.message || 'Не удалось связаться с сервером', true); }
    finally { buttons.forEach(b => b.disabled = false); }
}
function switchTab(tabName, element) {
    document.querySelectorAll('.screen').forEach(el => el.classList.remove('active'));
    $('screen-' + tabName).classList.add('active');
    document.querySelectorAll('.tab-item').forEach(el => el.classList.remove('active'));
    element?.classList.add('active');
    if (!authenticated) return;
    const loads = {broadcast:loadChats, catalog:loadCatalogList, dashboard:loadDashboard, clients:loadClients, profile:loadProfile};
    loads[tabName]?.().catch(error => notice(error.message, true));
    if (tabName === 'broadcast' && !discoveryAttempted) { discoveryAttempted = true; findGroups(); }
}
const statuses = {scheduled:'Запланировано', processing:'Отправляется', sent:'Отправлено', failed:'Ошибка отправки', uncertain:'Проверьте доставку в MAX', cancelled:'Отменено'};
function taskMarkup(job) {
    const id = escapeHTML(job.id);
    const type = job.type === 'group' ? 'group' : 'client';
    return `<div class="task-card"><div class="task-content"><div class="task-title">${escapeHTML(job.text)}</div><div class="task-sub">${escapeHTML(dateLabel(job.schedule))}</div><div>${escapeHTML(statuses[job.status] || job.status)}</div>${job.error ? `<div>${escapeHTML(job.error)}</div>` : ''}</div>${job.status !== 'processing' ? `<button aria-label="Удалить задачу" data-delete-job="${id}" data-job-type="${type}">Удалить</button>` : ''}</div>`;
}
async function loadDashboard() {
    const [data, stats] = await Promise.all([api('/get_jobs'), api('/get_stats')]);
    const pending = data.jobs.filter(j => !['sent','cancelled'].includes(j.status));
    $('dashTasksCount').textContent = pending.length;
    $('statGroups').textContent = stats.groups;
    $('statClients').textContent = stats.clients;
    $('statOverdue').textContent = pending.filter(j => new Date(j.schedule) < new Date()).length;
    $('dashboardTasks').innerHTML = pending.slice(0, 5).map(taskMarkup).join('') || '<p>Нет активных задач</p>';
}
async function loadChats() {
    const [data, tasks] = await Promise.all([api('/get_chats'), api('/get_jobs')]);
    $('chatSelect').replaceChildren(...data.chats.map(chat => new Option(chat.title, chat.id)));
    $('groupsList').innerHTML = data.chats.map(chat => `<div class="group-card"><div class="group-info"><div class="group-name">${escapeHTML(chat.title)}</div><div class="group-members">ID: ${escapeHTML(chat.id)}</div></div><button data-disconnect="${escapeHTML(chat.id)}">Отключить</button></div>`).join('') || '<p>Подключённых групп пока нет. Найдите группу в MAX и нажмите «Подключить».</p>';
    $('broadcastJobs').innerHTML = tasks.jobs.filter(j => j.type === 'group').map(taskMarkup).join('') || '<p>Рассылок пока нет</p>';
}
async function findGroups(nextPage = false) {
    await action(async () => {
        discoveryAttempted = true;
        const suffix = nextPage && discoveryMarker != null ? '?marker=' + encodeURIComponent(discoveryMarker) : '';
        const data = await api('/discover_chats' + suffix);
        discoveryMarker = data.marker;
        const cards = data.chats.map(chat => `<div class="group-card"><div class="group-info"><div class="group-name">${escapeHTML(chat.title)}</div></div>${chat.connected ? '<span>Подключена</span>' : `<button data-connect="${escapeHTML(chat.id)}">Подключить</button>`}</div>`).join('');
        $('availableGroups').innerHTML = cards || '<p>На этой странице подходящих групп нет. Ваш аккаунт и бот должны быть администраторами группы.</p>';
        if (data.hint) { const hint = document.createElement('p'); hint.textContent = data.hint; $('availableGroups').append(hint); }
        if (data.unavailable_count) $('availableGroups').insertAdjacentHTML('beforeend', '<p>MAX не подтвердил права в части групп. Попробуйте поиск ещё раз.</p>');
        if (discoveryMarker != null) {
            const more = document.createElement('button'); more.textContent = 'Следующие группы';
            more.addEventListener('click', () => findGroups(true)); $('availableGroups').append(more);
        }
    });
}
async function connectChat(selectedId) {
    await action(async () => {
        const chatId = selectedId || $('groupId').value.trim();
        if (!/^-?\d+$/.test(chatId)) throw new Error('Введите числовой ID группы');
        await api('/connect_chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({chat_id:chatId})});
        $('groupId').value = '';
        await loadChats(); $('availableGroups').replaceChildren(); notice('Группа подключена.');
    });
}
async function loadClients() {
    const data = await api('/get_clients');
    $('clientsList').innerHTML = data.clients.map(c => `<div class="task-card"><div class="task-content"><div class="task-title">${escapeHTML(c.type)}: ${escapeHTML(c.name)}</div><div class="task-sub">${escapeHTML(c.text)}</div><div>${escapeHTML(dateLabel(c.schedule_time))}</div><div class="task-phone">${escapeHTML(c.phone)}</div></div><button data-delete-job="${escapeHTML(c.id)}" data-job-type="client">Удалить</button></div>`).join('') || '<p>Пока нет запланированных касаний</p>';
}
function renderCatalog() {
    imageObjects.forEach(URL.revokeObjectURL); imageObjects = [];
    const query = $('catalogSearch').value.toLocaleLowerCase();
    const items = catalogItems.filter(item => `${item.title} ${item.address} ${item.description}`.toLocaleLowerCase().includes(query));
    $('catalogList').innerHTML = items.map(item => `<div class="obj-card"><div class="catalog-images" data-images="${escapeHTML(item.id)}"></div><div class="obj-info"><div class="obj-tag">${escapeHTML(item.tag)}</div><h3 class="obj-title">${escapeHTML(item.title)}</h3><div class="obj-addr">${escapeHTML(item.address)}</div><div class="obj-price">${escapeHTML(item.price)}</div><div class="obj-desc">${escapeHTML(item.description)}</div><button data-delete-catalog="${escapeHTML(item.id)}">Удалить объект</button></div></div>`).join('') || '<p>Объектов не найдено</p>';
    for (const item of items) {
        const container = [...document.querySelectorAll('[data-images]')].find(el => el.dataset.images === item.id);
        for (const url of item.image_urls || []) {
            // Bearer auth cannot be attached by <img>; fetch the backend image as a blob.
            const parsed = new URL(url, API_ORIGIN);
            if (parsed.origin !== API_ORIGIN) continue;
            fetch(parsed.href, {headers:{Authorization:'Bearer ' + sessionToken}}).then(async response => {
                if (!response.ok) throw new Error('Не удалось загрузить фото');
                const blob = await response.blob();
                if (!container.isConnected) return;
                const objectURL = URL.createObjectURL(blob); imageObjects.push(objectURL);
                const img = document.createElement('img'); img.className = 'obj-img'; img.alt = item.title; img.src = objectURL;
                container.append(img);
            }).catch(error => notice(error.message, true));
        }
    }
}
async function loadCatalogList() { catalogItems = (await api('/get_catalog')).catalog; renderCatalog(); }
async function loadProfile() {
    const [stats, profile] = await Promise.all([api('/get_stats'), api('/me')]);
    $('profGroups').textContent = stats.groups; $('profClients').textContent = stats.clients;
    $('profSubs').textContent = stats.subscribers ?? '—';
    $('profName').textContent = profile.user.first_name || 'Пользователь MAX';
    $('userAvatar').textContent = (profile.user.first_name || 'М')[0];
    $('subscriptionLabel').textContent = profile.subscription === 'trial' ? `Пробный период до ${dateLabel(profile.trial_end)}` : 'Пробный период завершён';
}
function futureISO(value) {
    const date = new Date(value);
    if (!value || Number.isNaN(date.getTime()) || date <= new Date()) throw new Error('Выберите дату и время в будущем');
    return date.toISOString();
}
function appendImages(form, input) {
    for (const file of input.files) form.append('images', file);
}
async function sendBroadcast() {
    await action(async () => {
        if (!$('chatSelect').value || !$('adText').value.trim()) throw new Error('Выберите группу и напишите текст');
        const form = new FormData(); form.append('chat_id', $('chatSelect').value); form.append('text', $('adText').value.trim());
        form.append('run_at', futureISO($('adRunAt').value)); appendImages(form, $('adImages'));
        const result = await api('/submit_ad', {method:'POST', body:form});
        $('adText').value = ''; $('adImages').value = ''; $('broadcastForm').style.display = 'none';
        await loadChats(); notice(result.message);
    });
}
async function addClient() {
    await action(async () => {
        if (!$('clientName').value.trim() || !$('clientDate').value || !$('clientTime').value) throw new Error('Укажите имя, дату и время');
        const form = new FormData();
        for (const [field,id] of Object.entries({name:'clientName',phone:'clientPhone',text:'clientText',type:'clientType',date:'clientDate',time:'clientTime'})) form.append(field, $(id).value);
        form.append('run_at', futureISO(`${$('clientDate').value}T${$('clientTime').value}`));
        await api('/add_client', {method:'POST',body:form});
        for (const id of ['clientName','clientPhone','clientText']) $(id).value = '';
        $('addClientForm').style.display = 'none'; await loadClients(); notice('Касание сохранено в планере.');
    });
}
async function saveCatalogItem() {
    await action(async () => {
        if (!$('catTitle').value.trim()) throw new Error('Введите название объекта');
        const form = new FormData();
        for (const [field,id] of Object.entries({title:'catTitle',address:'catAddress',price:'catPrice',tag:'catTag',description:'catDescription'})) form.append(field, $(id).value);
        appendImages(form, $('catImages')); await api('/add_catalog', {method:'POST',body:form});
        for (const id of ['catTitle','catAddress','catPrice','catDescription','catImages']) $(id).value = '';
        $('addCatalogForm').style.display = 'none'; await loadCatalogList(); notice('Объект сохранён.');
    });
}
async function logout() {
    try { if (authenticated) await api('/auth/logout', {method:'POST'}); }
    catch (error) { notice(error.message,true); return; }
    sessionStorage.removeItem('neprostopost.session'); sessionStorage.setItem('neprostopost.signedout', '1');
    sessionToken = ''; authenticated = false;
    location.reload();
}
document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.connect) connectChat(button.dataset.connect);
    if (button.dataset.deleteJob) action(async () => {
        await api(`/delete_job/${encodeURIComponent(button.dataset.deleteJob)}/${button.dataset.jobType}`, {method:'DELETE'});
        await Promise.all([loadDashboard(),loadClients(),loadChats()]); notice('Задача удалена.');
    });
    if (button.dataset.deleteCatalog) action(async () => {
        await api(`/catalog/${encodeURIComponent(button.dataset.deleteCatalog)}`, {method:'DELETE'}); await loadCatalogList(); notice('Объект удалён.');
    });
    if (button.dataset.disconnect) action(async () => {
        await api(`/groups/${encodeURIComponent(button.dataset.disconnect)}`, {method:'DELETE'}); await loadChats(); notice('Группа отключена, её ожидающие рассылки отменены.');
    });
});
async function initialize() {
    const webapp = window.WebApp;
    webapp?.ready?.();
    const date = new Date(Date.now() + 3600000);
    const local = new Date(date.getTime() - date.getTimezoneOffset()*60000).toISOString();
    $('clientDate').value = local.slice(0,10); $('clientTime').value = local.slice(11,16); $('adRunAt').value = local.slice(0,16);
    if (sessionStorage.getItem('neprostopost.signedout')) {
        notice('Вы вышли из аккаунта.');
        const loginButton = document.createElement('button'); loginButton.textContent = 'Войти через MAX';
        loginButton.addEventListener('click', () => { sessionStorage.removeItem('neprostopost.signedout'); initialize(); });
        $('appNotice').append(' ', loginButton);
        return;
    }
    try {
        if (sessionToken) {
            try { await api('/me'); authenticated = true; }
            catch (error) { if (sessionToken) throw error; }
        }
        if (!authenticated) {
            if (!webapp?.initData) { notice('Для входа откройте приложение из бота MAX.'); return; }
            const data = await api('/auth/max', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({init_data:webapp.initData})});
            sessionToken = data.token; sessionStorage.setItem('neprostopost.session', sessionToken); authenticated = true;
        }
        notice(''); await Promise.all([loadDashboard(),loadProfile()]);
    } catch (error) { notice(error.message || 'Не удалось подключиться к серверу', true); }
}
initialize();

setInterval(() => {
    if (!authenticated || document.hidden) return;
    const active = document.querySelector('.screen.active')?.id;
    const refresh = {'screen-dashboard':loadDashboard,'screen-broadcast':loadChats};
    refresh[active]?.().catch(error => notice(error.message, true));
}, 15000);

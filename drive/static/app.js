'use strict';

const VIEW_TITLES = {
  recent: 'Recent', starred: 'Starred', links: 'Share links', bin: 'Bin', search: 'Search', sync: 'Cloud sync',
};
const EMPTY_TEXT = {
  drive: 'This folder is empty. Drop files here or use Upload.',
  recent: 'Files you add or open appear here.',
  starred: 'Star files and folders to find them here.',
  links: 'Links you share appear here.',
  bin: 'The bin is empty.',
  sync: 'No cloud connections yet. Add one to keep a Drive folder in step with Google Drive, Nextcloud or WebDAV.',
  search: 'Nothing matches that name.',
};
const SEARCH_DELAY_MS = 250;
const TOAST_MS = 4000;
const PARALLEL_UPLOADS = 2;
const RETRY_LIMIT = 8;
const RETRY_BASE_MS = 1000;
const RETRY_MAX_MS = 30000;
const LOW_ROOM_BYTES = 1024 ** 3;
const DISK_FULL = 507;
const RESUME_PREFIX = 'drive-upload:';
const LAYOUT_KEY = 'drive-layout';
const SIGNED_OUT = 401;
const NOT_READY = 503;
const NOT_FOUND = 404;
const THUMB_RETRY_BASE_MS = 800;
const THUMB_RETRY_MAX_MS = 8000;
const SIGNED_OUT_TEXT = 'You are signed out. Sign in again, then pick the same files to carry on where the uploads stopped.';
const BATCH_BYTES = 16;
const SORT_KEY = 'drive-sort';
const ICONS = {
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  file: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5',
  image: 'M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9h.01',
  pdf: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h4',
  text: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 12l-2 2 2 2M15 12l2 2-2 2',
  audio: 'M9 18V6l10-2v12M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM19 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  video: 'M4 6h12v12H4zM16 10l5-3v10l-5-3',
  more: 'M12 5h.01M12 12h.01M12 19h.01',
  star: 'M12 3l2.8 5.7 6.2.9-4.5 4.4 1 6.2L12 17.3 6.5 20.2l1-6.2L3 9.6l6.2-.9z',
  close: 'M6 6l12 12M18 6 6 18',
};

const state = {
  view: 'drive', folderId: 0, query: '', items: [], path: [], selected: new Set(), csrf: '', session: null,
  sort: 'name', order: 'asc', layout: 'list', storage: null, shares: [],
};
const uploadQueue = [];
let runningUploads = 0;
const byId = (id) => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
const timeFormat = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

function readSetting(key, fallback) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function writeSetting(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    return;
  }
}

function isJsonResponse(response) {
  return (response.headers.get('Content-Type') || '').startsWith('application/json');
}

async function api(method, path, body) {
  const headers = { Accept: 'application/json' };
  if (method !== 'GET') headers['X-CSRF-Token'] = state.csrf;
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: 'same-origin' });
  if (response.redirected) throw new ApiError(SIGNED_OUT_TEXT, SIGNED_OUT);
  if (response.status === 204) return null;
  const data = isJsonResponse(response) ? await response.json().catch(() => null) : null;
  if (!response.ok) {
    const message = data && data.error ? data.error : `the server answered ${response.status}`;
    throw new ApiError(message, response.status);
  }
  if (data === null) throw new ApiError(SIGNED_OUT_TEXT, SIGNED_OUT);
  return data;
}

function newBatchId() {
  const bytes = crypto.getRandomValues(new Uint8Array(BATCH_BYTES));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, '0')).join('');
}

let toastTimer = 0;
function showToast(message, isError = false) {
  const toast = byId('toast');
  toast.textContent = message;
  toast.classList.toggle('error', isError);
  toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.hidden = true; }, TOAST_MS);
}

function reportError(error) {
  if (error instanceof ApiError && error.status === SIGNED_OUT) {
    showToast('You are signed out. Reload the page to sign in again.', true);
    return;
  }
  showToast(error.message || String(error), true);
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name]);
  svg.append(path);
  return svg;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = -1;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

function iconFor(item) {
  if (item.kind === 'folder') return 'folder';
  return ICONS[item.preview] ? item.preview : 'file';
}

function contentUrl(item, inline) {
  return `/api/files/${item.id}/content${inline ? '?inline=1' : ''}`;
}

function downloadUrl(item) {
  return item.kind === 'folder' ? `/api/folders/${item.id}/zip` : contentUrl(item, false);
}

function routeFromHash() {
  const hash = location.hash.replace(/^#\/?/, '');
  const [view, id] = hash.split('/');
  if (view === 'folder' && /^\d+$/.test(id || '')) return { view: 'drive', folderId: Number(id) };
  if (['recent', 'starred', 'links', 'bin', 'sync'].includes(view)) return { view, folderId: 0 };
  return { view: 'drive', folderId: 0 };
}

function goTo(view, folderId = 0) {
  const hash = view === 'drive' ? (folderId ? `#/folder/${folderId}` : '#/') : `#/${view}`;
  if (location.hash === hash || (hash === '#/' && location.hash === '')) {
    applyRoute();
    return;
  }
  location.hash = hash;
}

function applyRoute() {
  const route = routeFromHash();
  state.view = route.view;
  state.folderId = route.folderId;
  state.query = '';
  byId('search').value = '';
  state.selected.clear();
  load();
}

async function load() {
  try {
    if (state.view === 'drive') {
      const listing = await api('GET', `/api/folders/${state.folderId}?sort=${state.sort}&order=${state.order}`);
      state.items = listing.items;
      state.path = listing.path;
    } else if (state.view === 'sync') {
      await loadSync();
      state.items = [];
    } else if (state.view === 'links') {
      const answer = await api('GET', '/api/shares');
      state.shares = answer.shares;
      state.items = [];
    } else {
      const path = state.view === 'search' ? `/api/search?q=${encodeURIComponent(state.query)}` : `/api/${state.view}`;
      state.items = (await api('GET', path)).items;
    }
  } catch (error) {
    if (error.status === 404 && state.view === 'drive' && state.folderId) {
      showToast('That folder is not here any more.', true);
      goTo('drive');
      return;
    }
    reportError(error);
  }
  render();
}

function renderCrumbs() {
  const crumbs = byId('crumbs');
  crumbs.replaceChildren();
  if (state.view !== 'drive') {
    const title = state.view === 'search' ? `Search for "${state.query}"` : VIEW_TITLES[state.view];
    crumbs.append(element('span', 'crumb', title));
    return;
  }
  const steps = [{ id: 0, name: 'My Drive' }, ...state.path];
  steps.forEach((step, index) => {
    if (index > 0) crumbs.append(element('span', 'crumb-sep', '/'));
    const button = element('button', 'crumb', step.name);
    button.type = 'button';
    button.addEventListener('click', () => goTo('drive', step.id));
    attachDropTarget(button, step.id);
    crumbs.append(button);
  });
}

function renderNotices() {
  byId('bin-notice').hidden = state.view !== 'bin';
  byId('bin-empty').hidden = !state.items.length;
  byId('bin-text').textContent = `Items in the bin are deleted for good after ${state.session ? state.session.binDays : 30} days.`;
  const links = byId('links-notice');
  links.hidden = state.view !== 'links';
  links.textContent = state.session && state.session.sharesEnabled
    ? 'Anyone with one of these links can view and download what it points to, without signing in. Revoke a link to stop it at once.'
    : 'Share links are switched off on this server, so none of these links work right now. Turn them on with SHARES_ENABLED=true.';
  byId('sync-notice').hidden = state.view !== 'sync';
  byId('zip-folder').hidden = state.view !== 'drive' || !state.folderId;
  byId('sort').disabled = state.view !== 'drive';
}

function renderSelection() {
  const bar = byId('selection-bar');
  const count = state.selected.size;
  bar.hidden = count === 0;
  byId('selection-count').textContent = `${count} selected`;
  const inBin = state.view === 'bin';
  bar.querySelectorAll('[data-bulk]').forEach((button) => {
    const action = button.dataset.bulk;
    const binOnly = action === 'restore' || action === 'forever';
    button.hidden = action !== 'clear' && (inBin ? !binOnly : binOnly);
  });
}

function render() {
  renderCrumbs();
  renderNotices();
  renderStorage();
  document.querySelectorAll('.view-btn').forEach((button) => {
    button.setAttribute('aria-current', String(button.dataset.view === state.view));
  });
  const list = byId('items');
  list.className = `items ${state.layout}`;
  list.replaceChildren();
  if (state.view === 'links') {
    renderLinks(list);
  } else if (state.view === 'sync') {
    renderSync(list);
  } else {
    state.items.forEach((item) => list.append(renderItem(item)));
  }
  const empty = byId('empty-state');
  const count = { links: state.shares.length, sync: syncState.connections.length }[state.view] ?? state.items.length;
  empty.hidden = count > 0;
  empty.textContent = EMPTY_TEXT[state.view];
  renderSelection();
}

function renderItem(item) {
  const row = element('div', 'item');
  row.setAttribute('role', 'listitem');
  row.dataset.id = item.id;
  row.classList.toggle('selected', state.selected.has(item.id));
  const check = element('input', 'item-check');
  check.type = 'checkbox';
  check.checked = state.selected.has(item.id);
  check.setAttribute('aria-label', `Select ${item.name}`);
  check.addEventListener('click', (event) => event.stopPropagation());
  check.addEventListener('change', () => toggleSelected(item.id, check.checked));
  row.append(check);
  if (state.layout === 'grid') {
    row.append(renderThumb(item));
    const label = element('div', 'grid-label');
    label.append(element('span', 'item-name', item.name));
    if (item.starred) label.append(starMark());
    row.append(label);
  } else {
    const glyph = element('span', `item-icon ${item.kind === 'folder' ? 'folder' : ''}`);
    glyph.append(icon(iconFor(item)));
    row.append(glyph, element('span', 'item-name', item.name));
    if (item.starred) row.append(starMark());
    row.append(element('span', 'item-meta', item.kind === 'folder' ? '' : formatSize(item.size)));
    const when = state.view === 'bin' ? item.trashedAt : item.modifiedAt;
    row.append(element('span', 'item-meta item-date', when ? dateFormat.format(new Date(when * 1000)) : ''));
  }
  const more = element('button', 'icon-btn item-more');
  more.type = 'button';
  more.setAttribute('aria-label', `More for ${item.name}`);
  more.append(icon('more'));
  more.addEventListener('click', (event) => {
    event.stopPropagation();
    openMenu(item, more);
  });
  row.append(more);
  row.addEventListener('click', () => openItem(item));
  row.addEventListener('contextmenu', (event) => {
    event.preventDefault();
    openMenu(item, more);
  });
  if (item.kind === 'folder' && state.view === 'drive') attachDropTarget(row, item.id);
  if (state.view === 'drive') attachDragSource(row, item);
  return row;
}

function starMark() {
  const mark = element('span', 'item-star');
  mark.setAttribute('aria-label', 'Starred');
  mark.append(icon('star'));
  return mark;
}

const missingThumbs = new Set();
const thumbItems = new WeakMap();
let thumbWatcher = null;

function thumbKey(item) {
  return `${item.id}:${item.size}:${item.modifiedAt}`;
}

function renderThumb(item) {
  const thumb = element('div', `thumb ${item.kind === 'folder' ? 'folder' : ''}`);
  thumb.append(icon(iconFor(item)));
  if (item.preview === 'image' && state.view !== 'bin' && !missingThumbs.has(thumbKey(item))) watchThumb(thumb, item);
  return thumb;
}

function watchThumb(thumb, item) {
  if (!('IntersectionObserver' in window)) {
    loadThumb(thumb, item);
    return;
  }
  thumbItems.set(thumb, item);
  thumbWatcher = thumbWatcher || new IntersectionObserver((entries) => {
    entries.filter((entry) => entry.isIntersecting).forEach((entry) => {
      thumbWatcher.unobserve(entry.target);
      loadThumb(entry.target, thumbItems.get(entry.target));
    });
  }, { rootMargin: '200px' });
  thumbWatcher.observe(thumb);
}

function thumbUrl(item) {
  return `/api/files/${item.id}/thumb`;
}

function thumbRetryDelay(attempt) {
  const grown = Math.min(THUMB_RETRY_MAX_MS, THUMB_RETRY_BASE_MS * 1.5 ** attempt);
  return grown + Math.random() * THUMB_RETRY_BASE_MS;
}

async function loadThumb(thumb, item) {
  for (let attempt = 0; thumb.isConnected; attempt += 1) {
    const response = await fetch(thumbUrl(item), { credentials: 'same-origin' }).catch(() => null);
    if (response && response.redirected) return;
    if (response && response.ok) {
      await swapInThumb(thumb, response);
      return;
    }
    if (response && response.status === NOT_FOUND) missingThumbs.add(thumbKey(item));
    if (response && response.status !== NOT_READY) return;
    await sleep(thumbRetryDelay(attempt));
  }
}

async function swapInThumb(thumb, response) {
  const url = URL.createObjectURL(await response.blob());
  const image = element('img');
  image.alt = '';
  image.addEventListener('load', () => {
    URL.revokeObjectURL(url);
    if (thumb.isConnected) thumb.replaceChildren(image);
  }, { once: true });
  image.addEventListener('error', () => URL.revokeObjectURL(url), { once: true });
  image.src = url;
}

function renderLinks(list) {
  state.shares.forEach((share) => {
    const row = element('div', 'item link-item');
    row.setAttribute('role', 'listitem');
    const glyph = element('span', `item-icon ${share.nodeKind === 'folder' ? 'folder' : ''}`);
    glyph.append(icon(share.nodeKind === 'folder' ? 'folder' : 'file'));
    row.append(glyph, element('span', 'item-name', share.nodeName));
    row.append(element('span', 'link-meta', describeShare(share)));
    const copy = element('button', 'btn ghost', 'Copy link');
    copy.type = 'button';
    copy.addEventListener('click', () => copyLink(share));
    const revoke = element('button', 'btn', 'Revoke');
    revoke.type = 'button';
    revoke.addEventListener('click', () => revokeShare(share));
    row.append(copy, revoke);
    list.append(row);
  });
}

function describeShare(share) {
  const parts = [];
  if (share.inBin) parts.push('in the bin, works again if restored');
  if (share.expired) parts.push('expired');
  else if (share.expiresAt) parts.push(`until ${timeFormat.format(new Date(share.expiresAt * 1000))}`);
  if (share.hasPassword) parts.push('password');
  return parts.join(', ') || 'no expiry';
}

function shareUrl(share) {
  return `${location.origin}/s/${share.token}`;
}

async function copyLink(share) {
  try {
    await navigator.clipboard.writeText(shareUrl(share));
    showToast('Link copied.');
  } catch {
    window.prompt('Copy this link:', shareUrl(share));
  }
}

async function revokeShare(share) {
  if (!window.confirm(`Stop the link to "${share.nodeName}"? Anyone using it loses access at once.`)) return;
  try {
    await api('DELETE', `/api/shares/${share.id}`);
    showToast('Link revoked.');
    if (state.view === 'links') load();
  } catch (error) {
    reportError(error);
  }
}

function toggleSelected(id, isOn) {
  if (isOn) state.selected.add(id);
  else state.selected.delete(id);
  const row = byId('items').querySelector(`[data-id="${id}"]`);
  if (row) row.classList.toggle('selected', isOn);
  renderSelection();
}

function openItem(item) {
  if (state.view === 'bin') {
    showToast('Restore this item to open it.');
    return;
  }
  if (item.kind === 'folder') {
    goTo('drive', item.id);
    return;
  }
  openViewer(item);
}

function closeMenu() {
  const open = document.querySelector('.menu');
  if (open) open.remove();
}

function menuEntries(item) {
  if (state.view === 'bin') {
    return [
      ['Restore', () => bulk('restore', [item.id])],
      ['Delete forever', () => bulk('forever', [item.id]), true],
    ];
  }
  const entries = [];
  if (item.kind === 'file') entries.push(['Preview', () => openViewer(item)]);
  else entries.push(['Open', () => goTo('drive', item.id)]);
  entries.push([item.kind === 'folder' ? 'Download as zip' : 'Download', () => downloadItem(item)]);
  entries.push(['Rename', () => renameItem(item)]);
  entries.push(['Move to', () => bulk('move', [item.id])]);
  entries.push(['Make a copy', () => bulk('copy', [item.id])]);
  if (item.kind === 'file') entries.push(['Convert to...', () => openConvertDialog(item)]);
  entries.push([item.starred ? 'Remove star' : 'Star', () => setStar(item, !item.starred)]);
  entries.push(['Share link', () => openShareDialog(item)]);
  if (state.view !== 'drive') entries.push(['Show in folder', () => goTo('drive', item.parentId)]);
  entries.push(['Delete', () => bulk('trash', [item.id]), true]);
  return entries;
}

function openMenu(item, anchor) {
  closeMenu();
  const menu = element('div', 'menu');
  menu.setAttribute('role', 'menu');
  menuEntries(item).forEach(([label, action, isDanger]) => {
    const button = element('button', isDanger ? 'danger' : '', label);
    button.type = 'button';
    button.setAttribute('role', 'menuitem');
    button.addEventListener('click', () => {
      closeMenu();
      action();
    });
    menu.append(button);
  });
  document.body.append(menu);
  const box = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  menu.style.left = `${Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8))}px`;
  menu.style.top = `${box.bottom + height + 8 > window.innerHeight ? Math.max(8, box.top - height) : box.bottom}px`;
  menu.querySelector('button').focus();
}

function startDownload(url) {
  const link = element('a');
  link.href = url;
  link.download = '';
  document.body.append(link);
  link.click();
  link.remove();
}

async function downloadZip(folderId) {
  try {
    await api('GET', `/api/folders/${folderId}/zip/check`);
  } catch (error) {
    reportError(error);
    return;
  }
  startDownload(`/api/folders/${folderId}/zip`);
}

function downloadItem(item) {
  if (item.kind === 'folder') downloadZip(item.id);
  else startDownload(downloadUrl(item));
}

function closeOnCancel(dialog, resolve) {
  dialog.querySelectorAll('[data-close]').forEach((button) => {
    button.onclick = () => dialog.close();
  });
  dialog.onclose = () => resolve(null);
}

function askName(title, initial, saveLabel) {
  return new Promise((resolve) => {
    const dialog = byId('name-dialog');
    byId('name-title').textContent = title;
    byId('name-save').textContent = saveLabel;
    const input = byId('name-input');
    input.value = initial;
    closeOnCancel(dialog, resolve);
    byId('name-form').onsubmit = (event) => {
      event.preventDefault();
      const value = input.value.trim();
      dialog.onclose = null;
      dialog.close();
      resolve(value || null);
    };
    dialog.showModal();
    const dot = initial.lastIndexOf('.');
    input.setSelectionRange(0, dot > 0 ? dot : initial.length);
  });
}

async function createFolder() {
  const name = await askName('New folder', 'Untitled folder', 'Create');
  if (!name) return;
  try {
    await api('POST', '/api/folders', { parentId: uploadTarget(), name });
    if (state.view !== 'drive') goTo('drive');
    else load();
  } catch (error) {
    reportError(error);
  }
}

async function renameItem(item) {
  const name = await askName('Rename', item.name, 'Rename');
  if (!name || name === item.name) return;
  try {
    await api('PATCH', `/api/nodes/${item.id}`, { name });
    load();
  } catch (error) {
    reportError(error);
  }
}

async function setStar(item, starred) {
  try {
    await api('PATCH', `/api/nodes/${item.id}`, { starred });
    load();
  } catch (error) {
    reportError(error);
  }
}

function drawPickerCrumbs(path, show) {
  const crumbs = byId('picker-crumbs');
  crumbs.replaceChildren();
  [{ id: 0, name: 'My Drive' }, ...path].forEach((step, index) => {
    if (index > 0) crumbs.append(element('span', 'crumb-sep', '/'));
    const button = element('button', 'crumb', step.name);
    button.type = 'button';
    button.onclick = () => show(step.id);
    crumbs.append(button);
  });
}

function pickFolder(title, okLabel, movingIds) {
  return new Promise((resolve) => {
    const dialog = byId('picker-dialog');
    byId('picker-title').textContent = title;
    byId('picker-ok').textContent = okLabel;
    byId('picker-ok').disabled = true;
    let current = null;
    const show = async (folderId) => {
      let listing;
      try {
        listing = await api('GET', `/api/folders/${folderId}`);
      } catch (error) {
        reportError(error);
        return;
      }
      current = folderId;
      byId('picker-ok').disabled = false;
      drawPickerCrumbs(listing.path, show);
      const list = byId('picker-list');
      list.replaceChildren();
      listing.items.filter((item) => item.kind === 'folder' && !movingIds.includes(item.id)).forEach((item) => {
        const button = element('button');
        button.type = 'button';
        button.append(icon('folder'), element('span', '', item.name));
        button.onclick = () => show(item.id);
        list.append(button);
      });
      if (!list.children.length) list.append(element('p', 'empty-state', 'No folders inside.'));
    };
    closeOnCancel(dialog, resolve);
    byId('picker-ok').onclick = () => {
      if (current === null) return;
      dialog.onclose = null;
      dialog.close();
      resolve(current);
    };
    show(uploadTarget());
    dialog.showModal();
  });
}

async function moveOrCopy(action, ids) {
  const isMove = action === 'move';
  const target = await pickFolder(isMove ? 'Move to' : 'Copy to', isMove ? 'Move here' : 'Copy here', ids);
  if (target === null) return false;
  await api('POST', `/api/nodes/${action}`, { ids, targetId: target });
  showToast(isMove ? 'Moved.' : 'Copied.');
  return true;
}

async function bulk(action, ids) {
  try {
    if (action === 'move' || action === 'copy') {
      if (!await moveOrCopy(action, ids)) return;
    } else if (action === 'trash') {
      await api('POST', '/api/nodes/trash', { ids });
      showToast(`Moved to the bin. It is deleted for good after ${state.session.binDays} days.`);
    } else if (action === 'restore') {
      await api('POST', '/api/nodes/restore', { ids });
      showToast('Restored.');
    } else if (action === 'forever') {
      if (!window.confirm(`Delete ${ids.length === 1 ? 'this item' : `${ids.length} items`} for good? This cannot be undone.`)) return;
      await api('POST', '/api/nodes/delete', { ids });
      showToast('Deleted for good.');
    }
  } catch (error) {
    reportError(error);
  }
  state.selected.clear();
  load();
  loadStorage();
}

async function emptyBin() {
  if (!window.confirm('Delete everything in the bin for good? This cannot be undone.')) return;
  try {
    await api('POST', '/api/bin/empty');
    showToast('The bin is empty.');
  } catch (error) {
    reportError(error);
  }
  load();
  loadStorage();
}

function drawShareRow(share, item, refresh) {
  const row = element('li', 'share-row');
  row.append(element('span', 'share-url', shareUrl(share)));
  row.append(element('span', share.expired ? 'expired' : '', describeShare(share)));
  const copy = element('button', 'btn ghost', 'Copy');
  copy.type = 'button';
  copy.onclick = () => copyLink(share);
  const revoke = element('button', 'btn', 'Revoke');
  revoke.type = 'button';
  revoke.onclick = async () => {
    await revokeShare({ ...share, nodeName: item.name });
    refresh();
  };
  row.append(copy, revoke);
  return row;
}

async function openShareDialog(item) {
  const dialog = byId('share-dialog');
  byId('share-title').textContent = `Share "${item.name}"`;
  const enabled = state.session.sharesEnabled;
  byId('share-off').hidden = enabled;
  byId('share-create').disabled = !enabled;
  byId('share-password').value = '';
  const refresh = async () => {
    const list = byId('share-list');
    list.replaceChildren();
    try {
      const answer = await api('GET', `/api/shares?nodeId=${item.id}`);
      answer.shares.forEach((share) => list.append(drawShareRow(share, item, refresh)));
    } catch (error) {
      reportError(error);
    }
  };
  byId('share-form').onsubmit = async (event) => {
    event.preventDefault();
    const seconds = byId('share-expiry').value;
    const body = { nodeId: item.id };
    if (seconds) body.expiresAt = Date.now() / 1000 + Number(seconds);
    const password = byId('share-password').value;
    if (password) body.password = password;
    try {
      const share = await api('POST', '/api/shares', body);
      byId('share-password').value = '';
      await copyLink(share);
      refresh();
    } catch (error) {
      reportError(error);
    }
  };
  dialog.querySelectorAll('[data-close]').forEach((button) => {
    button.onclick = () => dialog.close();
  });
  dialog.onclose = () => {
    if (state.view === 'links') load();
  };
  await refresh();
  dialog.showModal();
}

function fillViewer(stage, item, source, textUrl) {
  if (item.preview === 'image') {
    const image = element('img');
    image.alt = item.name;
    image.src = source;
    stage.append(image);
  } else if (item.preview === 'pdf') {
    const frame = element('iframe');
    frame.title = item.name;
    frame.src = source;
    stage.append(frame);
  } else if (item.preview === 'audio' || item.preview === 'video') {
    const player = element(item.preview);
    player.controls = true;
    player.preload = 'metadata';
    player.src = source;
    stage.append(player);
  } else if (item.preview === 'text') {
    const pre = element('pre', '', 'Loading...');
    stage.append(pre);
    api('GET', textUrl).then((answer) => {
      pre.textContent = answer.text + (answer.truncated ? '\n\n[Only the start is shown. Download the file to see all of it.]' : '');
    }).catch((error) => {
      pre.textContent = error.message;
    });
  } else {
    stage.append(element('p', 'empty-state', `No preview for this kind of file (${formatSize(item.size)}). Download it to open it.`));
  }
}

function openViewer(item) {
  const dialog = byId('viewer');
  const stage = byId('viewer-stage');
  stage.replaceChildren();
  byId('viewer-name').textContent = item.name;
  byId('viewer-download').href = contentUrl(item, false);
  fillViewer(stage, item, contentUrl(item, true), `/api/files/${item.id}/text`);
  dialog.onclose = () => {
    if (!dialog.open) stage.replaceChildren();
  };
  dialog.showModal();
}

async function loadStorage() {
  try {
    state.storage = await api('GET', '/api/storage');
  } catch (error) {
    reportError(error);
  }
  renderStorage();
}

function renderStorage() {
  const storage = state.storage;
  if (!storage) return;
  const box = byId('storage');
  const shown = Math.max(storage.diskBytes, 1);
  byId('storage-fill').style.width = `${Math.min(100, ((storage.diskBytes - storage.freeBytes) / shown) * 100).toFixed(1)}%`;
  byId('storage-used').textContent = `${formatSize(storage.usedBytes)} in ${storage.fileCount} files`
    + (storage.binBytes ? `, ${formatSize(storage.binBytes)} in the bin` : '');
  byId('storage-free').textContent = `${formatSize(storage.freeBytes)} free on the disk. Uploads stop when under ${formatSize(storage.minFreeBytes)} is free, so ${formatSize(storage.uploadRoomBytes)} can still be uploaded.`;
  box.classList.toggle('low', storage.uploadRoomBytes < LOW_ROOM_BYTES && storage.uploadRoomBytes > 0);
  box.classList.toggle('full', storage.uploadRoomBytes === 0);
  const notice = byId('disk-notice');
  notice.hidden = storage.uploadRoomBytes > 0 || state.view !== 'drive';
  notice.textContent = `Uploads are paused: the disk has ${formatSize(storage.freeBytes)} free and must keep ${formatSize(storage.minFreeBytes)} free. Empty the bin or delete files to make room.`;
}

function resumeKey(task) {
  return RESUME_PREFIX + [task.parentId, task.folders.join('/'), task.file.name, task.file.size, task.file.lastModified].join('|');
}

function queueFiles(files, parentId) {
  const batch = newBatchId();
  files.forEach(({ file, folders }) => {
    const task = { file, folders, parentId, batch, received: 0, state: 'waiting', uploadId: null, row: null, retries: 0, cancelled: false, xhr: null };
    uploadQueue.push(task);
    drawUploadRow(task);
  });
  byId('uploads').hidden = false;
  pumpUploads();
}

function drawUploadRow(task) {
  const row = element('li', 'upload-row');
  const name = element('span', 'upload-name', [...task.folders, task.file.name].join('/'));
  name.title = name.textContent;
  const meter = element('div', 'meter');
  const fill = element('div', 'meter-fill');
  meter.append(fill);
  const status = element('span', 'upload-state', 'Waiting');
  const cancel = element('button', 'icon-btn');
  cancel.type = 'button';
  cancel.setAttribute('aria-label', `Cancel ${task.file.name}`);
  cancel.append(icon('close'));
  cancel.onclick = () => cancelUpload(task);
  row.append(name, cancel, meter, status);
  byId('upload-list').append(row);
  task.row = { row, fill, status, cancel };
}

function showUploadState(task, text) {
  const fraction = task.file.size ? task.received / task.file.size : 1;
  task.row.fill.style.width = `${(fraction * 100).toFixed(1)}%`;
  task.row.status.textContent = text;
  task.row.row.classList.toggle('failed', task.state === 'failed');
  task.row.row.classList.toggle('done', task.state === 'done');
  task.row.cancel.hidden = ['done', 'failed', 'cancelled'].includes(task.state);
}

function updateUploadTitle() {
  const active = uploadQueue.filter((task) => task.state === 'waiting' || task.state === 'sending').length;
  byId('uploads-title').textContent = active ? `Uploading ${active} ${active === 1 ? 'file' : 'files'}` : 'Uploads finished';
}

function pumpUploads() {
  while (runningUploads < PARALLEL_UPLOADS) {
    const next = uploadQueue.find((task) => task.state === 'waiting');
    if (!next) break;
    runningUploads += 1;
    next.state = 'sending';
    runUpload(next).finally(() => {
      runningUploads -= 1;
      pumpUploads();
    });
  }
  updateUploadTitle();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function sendChunk(task, offset, blob) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    task.xhr = xhr;
    xhr.open('PUT', `/api/uploads/${task.uploadId}?offset=${offset}`);
    xhr.setRequestHeader('X-CSRF-Token', state.csrf);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = (event) => {
      task.received = offset + event.loaded;
      showUploadState(task, `${formatSize(task.received)} of ${formatSize(task.file.size)}`);
    };
    xhr.onload = () => {
      if (xhr.responseURL && !new URL(xhr.responseURL).pathname.startsWith('/api/uploads/')) {
        reject(new ApiError(SIGNED_OUT_TEXT, SIGNED_OUT));
        return;
      }
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        data = null;
      }
      if (xhr.status === 200) resolve(data);
      else reject(new ApiError(data && data.error ? data.error : `the server answered ${xhr.status}`, xhr.status));
    };
    xhr.onerror = () => reject(new ApiError('the connection dropped', 0));
    xhr.onabort = () => reject(new ApiError('cancelled', -1));
    xhr.send(blob);
  });
}

async function beginOrResume(task) {
  const saved = readSetting(resumeKey(task), '');
  if (saved) {
    try {
      const status = await api('GET', `/api/uploads/${saved}`);
      task.uploadId = saved;
      task.received = status.received;
      return status.chunkBytes;
    } catch {
      writeSetting(resumeKey(task), null);
    }
  }
  const body = {
    name: task.file.name, size: task.file.size, parentId: task.parentId, folders: task.folders, lastModified: task.file.lastModified,
  };
  if (task.folders.length) body.batch = task.batch;
  const started = await api('POST', '/api/uploads', body);
  task.uploadId = started.id;
  task.received = 0;
  writeSetting(resumeKey(task), started.id);
  return started.chunkBytes;
}

function isWorthRetrying(error) {
  const isServerHiccup = error.status >= 500 && error.status !== DISK_FULL;
  return error.status === 0 || error.status === 409 || isServerHiccup;
}

async function sendAllChunks(task, chunkBytes) {
  while (task.received < task.file.size) {
    if (task.cancelled) return;
    const end = Math.min(task.file.size, task.received + chunkBytes);
    try {
      const answer = await sendChunk(task, task.received, task.file.slice(task.received, end));
      task.received = answer.received;
      task.retries = 0;
    } catch (error) {
      if (task.cancelled) return;
      if (!isWorthRetrying(error) || task.retries >= RETRY_LIMIT) throw error;
      task.retries += 1;
      const wait = Math.min(RETRY_MAX_MS, RETRY_BASE_MS * 2 ** task.retries);
      showUploadState(task, `Connection lost, trying again in ${Math.round(wait / 1000)} s`);
      await sleep(wait);
      const status = await api('GET', `/api/uploads/${task.uploadId}`).catch(() => null);
      if (status) task.received = status.received;
    }
  }
}

function stopUploadsSignedOut() {
  uploadQueue.forEach((task) => {
    if (task.state !== 'waiting' && task.state !== 'sending') return;
    task.cancelled = true;
    task.state = 'failed';
    if (task.xhr) task.xhr.abort();
    showUploadState(task, SIGNED_OUT_TEXT);
  });
  updateUploadTitle();
  reportError(new ApiError(SIGNED_OUT_TEXT, SIGNED_OUT));
}

async function dropServerUpload(task) {
  writeSetting(resumeKey(task), null);
  if (task.uploadId) await api('DELETE', `/api/uploads/${task.uploadId}`).catch(() => null);
}

async function runUpload(task) {
  try {
    const chunkBytes = await beginOrResume(task);
    if (task.cancelled) {
      if (task.state === 'cancelled') await dropServerUpload(task);
      return;
    }
    await sendAllChunks(task, chunkBytes);
    if (task.cancelled) return;
    await api('POST', `/api/uploads/${task.uploadId}/finish`);
    writeSetting(resumeKey(task), null);
    task.state = 'done';
    showUploadState(task, 'Done');
    refreshAfterUpload();
  } catch (error) {
    if (task.cancelled) return;
    if (error.status === SIGNED_OUT) {
      stopUploadsSignedOut();
      return;
    }
    task.state = 'failed';
    showUploadState(task, `Stopped: ${error.message}. Pick the same file again to carry on where it stopped.`);
    if (error.status === DISK_FULL) loadStorage();
  }
}

let refreshTimer = 0;
function refreshAfterUpload() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    load();
    loadStorage();
  }, 400);
}

async function cancelUpload(task) {
  task.cancelled = true;
  task.state = 'cancelled';
  if (task.xhr) task.xhr.abort();
  showUploadState(task, 'Cancelled');
  await dropServerUpload(task);
  updateUploadTitle();
}

function isInThisPage(upload) {
  return uploadQueue.some((task) => task.uploadId === upload.id && (task.state === 'waiting' || task.state === 'sending'));
}

async function loadUnfinished() {
  let answer;
  try {
    answer = await api('GET', '/api/uploads');
  } catch (error) {
    reportError(error);
    return [];
  }
  return answer.uploads.filter((upload) => !isInThisPage(upload));
}

async function refreshUnfinishedNotice() {
  const unfinished = await loadUnfinished();
  const notice = byId('unfinished-notice');
  notice.hidden = unfinished.length === 0;
  byId('unfinished-text').textContent = `${unfinished.length} unfinished ${unfinished.length === 1 ? 'upload is' : 'uploads are'} waiting on the server and holding space.`;
}

function drawUnfinishedRow(upload, refresh) {
  const row = element('li', 'share-row');
  const name = element('span', 'share-url', [...upload.folders, upload.name].join('/'));
  name.title = name.textContent;
  row.append(name, element('span', '', `${formatSize(upload.received)} of ${formatSize(upload.size)}, last sent ${timeFormat.format(new Date(upload.updatedAt * 1000))}`));
  const cancel = element('button', 'btn', 'Cancel upload');
  cancel.type = 'button';
  cancel.onclick = async () => {
    try {
      await api('DELETE', `/api/uploads/${upload.id}`);
    } catch (error) {
      reportError(error);
    }
    refresh();
  };
  row.append(cancel);
  return row;
}

async function openUnfinishedDialog() {
  const dialog = byId('unfinished-dialog');
  const refresh = async () => {
    const list = byId('unfinished-list');
    list.replaceChildren();
    const unfinished = await loadUnfinished();
    unfinished.forEach((upload) => list.append(drawUnfinishedRow(upload, refresh)));
    if (!unfinished.length) list.append(element('p', 'empty-state', 'No unfinished uploads.'));
    refreshUnfinishedNotice();
    loadStorage();
  };
  dialog.querySelectorAll('[data-close]').forEach((button) => {
    button.onclick = () => dialog.close();
  });
  await refresh();
  dialog.showModal();
}

function clearFinishedUploads() {
  for (let index = uploadQueue.length - 1; index >= 0; index -= 1) {
    const task = uploadQueue[index];
    if (['done', 'failed', 'cancelled'].includes(task.state)) {
      task.row.row.remove();
      uploadQueue.splice(index, 1);
    }
  }
  byId('uploads').hidden = uploadQueue.length === 0;
}

function uploadTarget() {
  return state.view === 'drive' ? state.folderId : 0;
}

function filesFromInput(input) {
  return [...input.files].map((file) => {
    const parts = (file.webkitRelativePath || file.name).split('/');
    return { file, folders: parts.slice(0, -1) };
  });
}

function readEntries(reader) {
  return new Promise((resolve, reject) => reader.readEntries(resolve, reject));
}

async function walkEntry(entry, folders, found) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    found.push({ file, folders });
    return;
  }
  if (!entry.isDirectory) return;
  const reader = entry.createReader();
  let batch = await readEntries(reader);
  while (batch.length) {
    for (const child of batch) await walkEntry(child, [...folders, entry.name], found);
    batch = await readEntries(reader);
  }
}

async function filesFromDrop(dataTransfer) {
  const entries = [...dataTransfer.items]
    .filter((item) => item.kind === 'file')
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null));
  if (!entries.length || entries.some((entry) => !entry)) {
    return [...dataTransfer.files].map((file) => ({ file, folders: [] }));
  }
  const found = [];
  for (const entry of entries) await walkEntry(entry, [], found);
  return found;
}

function hasFiles(event) {
  return [...event.dataTransfer.types].includes('Files');
}

function attachDropTarget(target, folderId) {
  target.addEventListener('dragover', (event) => {
    event.preventDefault();
    event.stopPropagation();
    target.classList.add('drop-over');
    event.dataTransfer.dropEffect = hasFiles(event) ? 'copy' : 'move';
  });
  target.addEventListener('dragleave', () => target.classList.remove('drop-over'));
  target.addEventListener('drop', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    target.classList.remove('drop-over');
    byId('content').classList.remove('drop-target');
    await handleDrop(event, folderId);
  });
}

function attachDragSource(row, item) {
  row.draggable = true;
  row.addEventListener('dragstart', (event) => {
    const ids = state.selected.has(item.id) ? [...state.selected] : [item.id];
    event.dataTransfer.setData('application/x-drive-ids', JSON.stringify(ids));
    event.dataTransfer.effectAllowed = 'move';
  });
}

async function handleDrop(event, folderId) {
  const moving = event.dataTransfer.getData('application/x-drive-ids');
  if (moving) {
    const ids = JSON.parse(moving).filter((id) => id !== folderId);
    if (!ids.length) return;
    try {
      await api('POST', '/api/nodes/move', { ids, targetId: folderId });
      showToast('Moved.');
    } catch (error) {
      reportError(error);
    }
    state.selected.clear();
    load();
    return;
  }
  if (!hasFiles(event)) return;
  try {
    const files = await filesFromDrop(event.dataTransfer);
    if (files.length) queueFiles(files, folderId);
  } catch (error) {
    reportError(error);
  }
}

function setUpDropZone() {
  const content = byId('content');
  content.addEventListener('dragover', (event) => {
    if (state.view !== 'drive' || !hasFiles(event)) return;
    event.preventDefault();
    content.classList.add('drop-target');
  });
  content.addEventListener('dragleave', (event) => {
    if (!content.contains(event.relatedTarget)) content.classList.remove('drop-target');
  });
  content.addEventListener('drop', async (event) => {
    content.classList.remove('drop-target');
    if (state.view !== 'drive') return;
    event.preventDefault();
    await handleDrop(event, state.folderId);
  });
}

function applySortChoice(value) {
  const [sort, order] = value.split(':');
  state.sort = sort;
  state.order = order;
}

function runSearch() {
  const query = byId('search').value.trim();
  if (!query) {
    applyRoute();
    return;
  }
  state.view = 'search';
  state.query = query;
  state.selected.clear();
  load();
}

function syncLayoutButton() {
  const button = byId('layout-toggle');
  button.textContent = state.layout === 'list' ? 'Grid' : 'List';
  button.setAttribute('aria-label', state.layout === 'list' ? 'Show as grid' : 'Show as list');
}

function wireUploadButtons() {
  byId('upload-files').addEventListener('click', () => byId('file-input').click());
  byId('upload-folder').addEventListener('click', () => byId('folder-input').click());
  ['file-input', 'folder-input'].forEach((id) => {
    byId(id).addEventListener('change', () => {
      const files = filesFromInput(byId(id));
      byId(id).value = '';
      if (files.length) queueFiles(files, uploadTarget());
    });
  });
  byId('uploads-clear').addEventListener('click', clearFinishedUploads);
  byId('conversions-clear').addEventListener('click', clearFinishedConversions);
}

let searchTimer = 0;
function wireEvents() {
  document.querySelectorAll('.view-btn').forEach((button) => {
    button.addEventListener('click', () => goTo(button.dataset.view));
  });
  byId('search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, SEARCH_DELAY_MS);
  });
  wireUploadButtons();
  byId('new-folder').addEventListener('click', createFolder);
  byId('sort').addEventListener('change', () => {
    applySortChoice(byId('sort').value);
    writeSetting(SORT_KEY, byId('sort').value);
    load();
  });
  byId('layout-toggle').addEventListener('click', () => {
    state.layout = state.layout === 'list' ? 'grid' : 'list';
    writeSetting(LAYOUT_KEY, state.layout);
    syncLayoutButton();
    render();
  });
  byId('zip-folder').addEventListener('click', () => downloadZip(state.folderId));
  byId('unfinished-open').addEventListener('click', openUnfinishedDialog);
  byId('bin-empty').addEventListener('click', emptyBin);
  byId('sync-add').addEventListener('click', openAddConnection);
  byId('viewer-close').append(icon('close'));
  byId('viewer-close').addEventListener('click', () => byId('viewer').close());
  byId('selection-bar').addEventListener('click', (event) => {
    const button = event.target.closest('[data-bulk]');
    if (!button) return;
    if (button.dataset.bulk === 'clear') {
      state.selected.clear();
      render();
      return;
    }
    bulk(button.dataset.bulk, [...state.selected]);
  });
  document.addEventListener('click', (event) => {
    if (!event.target.closest('.menu')) closeMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeMenu();
  });
  window.addEventListener('hashchange', applyRoute);
  window.addEventListener('beforeunload', (event) => {
    if (uploadQueue.some((task) => task.state === 'sending' || task.state === 'waiting')) event.preventDefault();
  });
  setUpDropZone();
}

async function start() {
  state.layout = readSetting(LAYOUT_KEY, 'list') === 'grid' ? 'grid' : 'list';
  const sortChoice = readSetting(SORT_KEY, 'name:asc');
  if ([...byId('sort').options].some((option) => option.value === sortChoice)) byId('sort').value = sortChoice;
  applySortChoice(byId('sort').value);
  syncLayoutButton();
  wireEvents();
  try {
    state.session = await api('GET', '/api/session');
    state.csrf = state.session.csrfToken;
  } catch (error) {
    reportError(error);
    return;
  }
  loadStorage();
  applyRoute();
  refreshUnfinishedNotice();
  loadConversions();
}

document.addEventListener('DOMContentLoaded', start);

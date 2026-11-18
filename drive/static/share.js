'use strict';

const ICONS = {
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  file: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5',
  image: 'M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M15 9h.01',
  pdf: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h4',
  text: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 12l-2 2 2 2M15 12l2 2-2 2',
  audio: 'M9 18V6l10-2v12M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM19 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  video: 'M4 6h12v12H4zM16 10l5-3v10l-5-3',
};
const TOKEN = location.pathname.split('/')[2] || '';
const BASE = `/s/${TOKEN}`;
const byId = (id) => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });

class ShareError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function call(method, path, body) {
  const options = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(BASE + path, options);
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ShareError(data && data.error ? data.error : `the server answered ${response.status}`, response.status);
  return data;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('class', 'icon');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', ICONS[name] || ICONS.file);
  svg.append(path);
  return svg;
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

function showMessage(text, isError) {
  const message = byId('share-message');
  message.textContent = text;
  message.className = `notice${isError ? ' error' : ''}`;
  message.hidden = false;
}

function fileUrl(item, inline) {
  return `${BASE}/file/${item.id}${inline ? '?inline=1' : ''}`;
}

function openViewer(item) {
  const stage = byId('viewer-stage');
  stage.replaceChildren();
  byId('viewer-name').textContent = item.name;
  byId('viewer-download').href = fileUrl(item, false);
  const source = fileUrl(item, true);
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
    call('GET', `/text/${item.id}`).then((answer) => {
      pre.textContent = answer.text + (answer.truncated ? '\n\n[Only the start is shown. Download the file to see all of it.]' : '');
    }).catch((error) => {
      pre.textContent = error.message;
    });
  } else {
    stage.append(element('p', 'empty-state', 'No preview for this kind of file. Download it to open it.'));
  }
  byId('viewer').showModal();
}

async function downloadZip(folderId) {
  try {
    await call('GET', `/zip/${folderId}/check`);
  } catch (error) {
    showMessage(error.message, true);
    return;
  }
  const link = element('a');
  link.href = `${BASE}/zip/${folderId}`;
  link.download = '';
  document.body.append(link);
  link.click();
  link.remove();
}

function drawItem(item) {
  const row = element('div', 'item');
  row.setAttribute('role', 'listitem');
  const glyph = element('span', `item-icon ${item.kind === 'folder' ? 'folder' : ''}`);
  glyph.append(icon(item.kind === 'folder' ? 'folder' : item.preview));
  row.append(glyph, element('span', 'item-name', item.name));
  row.append(element('span', 'item-meta', item.kind === 'folder' ? '' : formatSize(item.size)));
  const download = element('a', 'btn ghost', 'Download');
  download.href = item.kind === 'folder' ? `${BASE}/zip/${item.id}` : fileUrl(item, false);
  download.addEventListener('click', (event) => {
    event.stopPropagation();
    if (item.kind !== 'folder') return;
    event.preventDefault();
    downloadZip(item.id);
  });
  row.append(download);
  row.addEventListener('click', () => {
    if (item.kind === 'folder') show(item.id);
    else openViewer(item);
  });
  return row;
}

function drawCrumbs(path) {
  const crumbs = byId('crumbs');
  crumbs.replaceChildren();
  path.forEach((step, index) => {
    if (index > 0) crumbs.append(element('span', 'crumb-sep', '/'));
    const button = element('button', 'crumb', step.name);
    button.type = 'button';
    button.addEventListener('click', () => show(step.id));
    crumbs.append(button);
  });
}

function drawExpiry(expiresAt) {
  byId('expiry').textContent = expiresAt ? `This link stops working on ${dateFormat.format(new Date(expiresAt * 1000))}.` : '';
}

async function show(folderId) {
  let info;
  try {
    info = await call('GET', `/info${folderId ? `?folder=${folderId}` : ''}`);
  } catch (error) {
    byId('shared').hidden = true;
    showMessage(error.message, true);
    return;
  }
  if (info.locked) {
    byId('lock').hidden = false;
    byId('shared').hidden = true;
    byId('lock-password').focus();
    return;
  }
  byId('lock').hidden = true;
  byId('shared').hidden = false;
  drawExpiry(info.expiresAt);
  document.title = `${info.root.name} - shared with you`;
  const list = byId('items');
  list.replaceChildren();
  if (info.root.kind === 'file') {
    drawCrumbs([]);
    byId('zip').hidden = true;
    list.append(drawItem(info.root));
    byId('empty-state').hidden = true;
    return;
  }
  drawCrumbs(info.path);
  const zip = byId('zip');
  zip.hidden = false;
  zip.href = `${BASE}/zip/${info.folder.id}`;
  zip.onclick = (event) => {
    event.preventDefault();
    downloadZip(info.folder.id);
  };
  info.items.forEach((item) => list.append(drawItem(item)));
  byId('empty-state').hidden = info.items.length > 0;
}

function wire() {
  byId('lock').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
      await call('POST', '/unlock', { password: byId('lock-password').value });
      byId('lock-password').value = '';
      byId('share-message').hidden = true;
      show(0);
    } catch (error) {
      showMessage(error.message, true);
    }
  });
  byId('viewer-close').addEventListener('click', () => byId('viewer').close());
  byId('viewer').addEventListener('close', () => {
    if (!byId('viewer').open) byId('viewer-stage').replaceChildren();
  });
}

wire();
show(0);

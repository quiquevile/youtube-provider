const $ = (selector, root = document) => root.querySelector(selector);
const el = (name, attrs = {}, text = '') => {
  const node = document.createElement(name);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  node.textContent = text;
  return node;
};
const heights = [144, 240, 360, 480, 720, 1080, 1440, 2160, 4320];
let state;
let selected;
let folderTarget;
let folderCurrent;
let folderParent;
let folderRequest = 0;

function notice(message, error = false) {
  $('#notice').textContent = message;
  $('#notice').classList.toggle('error', error);
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': state?.csrf || '' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (response.status === 401) { window.location.href = '/login'; throw new Error('Session expired'); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function reload() {
  state = await api('GET', '/admin/api/state');
  if (!selected || !state.profiles.some(profile => profile.slug === selected)) selected = state.profiles[0]?.slug;
  render();
}

function current() { return state.profiles.find(profile => profile.slug === selected); }

function render() {
  const select = $('#profile-select');
  select.replaceChildren(...state.profiles.map(profile => {
    const option = el('option', { value: profile.slug }, profile.name);
    option.selected = profile.slug === selected;
    return option;
  }));
  const profile = current();
  if (!profile) return;
  $('#base-url').textContent = `${state.api_base_url}/profiles/${profile.slug}`;
  $('#profile-name').value = profile.name;
  $('#output-dir').value = profile.output_dir;
  $('#search-terms').value = profile.search_terms.join(', ');
  $('#search-count').value = profile.search_count;
  for (const [id, value] of [['min-height', profile.min_height], ['max-height', profile.max_height]]) {
    const select = $(`#${id}`);
    select.replaceChildren(el('option', { value: '' }, 'No limit'), ...heights.map(height => el('option', { value: height }, `${height}p`)));
    select.value = value === null ? '' : String(value);
  }
  renderQuality(profile.quality_order);
  $('#channels').replaceChildren(...profile.channels.map(channelCard));
  renderPreviewChannels();
  renderKeys();
  $('#update-channel').value = state.update.channel;
  $('#update-interval').value = state.update.interval_hours;
  $('#update-status').textContent = `${state.update.status}: ${state.update.message || 'No update running'} · Active version: ${state.update.version || 'bundled'}`;
}

function renderQuality(order) {
  const list = $('#quality-order');
  list.replaceChildren(...order.map(height => {
    const item = el('li', { draggable: 'true', 'data-height': height }, `${height}p`);
    item.addEventListener('dragstart', () => item.classList.add('dragging'));
    item.addEventListener('dragend', () => item.classList.remove('dragging'));
    item.addEventListener('dragover', event => {
      event.preventDefault();
      const dragging = $('.dragging', list);
      if (dragging && dragging !== item) list.insertBefore(dragging, item);
    });
    return item;
  }));
}

async function loadFolder(path) {
  const request = ++folderRequest;
  $('#folder-select').disabled = true;
  $('#folder-list').replaceChildren();
  $('#folder-error').textContent = '';
  $('#folder-note').textContent = 'Loading folders…';
  const data = await api('GET', `/admin/api/directories?path=${encodeURIComponent(path)}`);
  if (request !== folderRequest) return;
  folderCurrent = data.path;
  folderParent = data.parent;
  $('#folder-current').textContent = data.path;
  $('#folder-up').disabled = !data.parent;
  $('#folder-select').disabled = !data.writable;
  $('#folder-note').textContent = data.truncated ? 'Showing the first 500 folders. You can type a path manually.' :
    (data.writable ? 'Select this folder or open a subdirectory.' : 'This folder is not writable by the provider.');
  $('#folder-list').replaceChildren(...data.directories.map(directory => {
    const row = el('li');
    const button = el('button', { type: 'button' }, directory.name);
    button.addEventListener('click', () => loadFolder(directory.path).catch(showFolderError));
    row.append(button);
    return row;
  }));
}

function showFolderError(error) {
  $('#folder-error').textContent = error.message;
  $('#folder-note').textContent = '';
}

async function openFolderPicker(target, fallback) {
  folderTarget = target;
  const dialog = $('#folder-dialog');
  if (!dialog.open) dialog.showModal();
  const requested = target.value.trim() || fallback || '/';
  let path = requested;
  if (!path.startsWith('/')) path = fallback || '/';
  while (dialog.open) {
    try {
      await loadFolder(path);
      if (path !== requested && dialog.open) {
        $('#folder-error').textContent = 'The requested folder is unavailable. Showing its nearest accessible parent; you can still type the original path manually.';
      }
      return;
    } catch (error) {
      if (path === '/') { showFolderError(error); return; }
      path = path.slice(0, path.lastIndexOf('/')) || '/';
    }
  }
}

function field(labelText, value, className) {
  const label = el('label', {}, labelText);
  const input = el('input', { class: className });
  input.value = value || '';
  label.append(input);
  return label;
}

function ruleRow(rule = { pattern: '', artist: '', title: '' }) {
  const row = el('div', { class: 'rule' });
  row.append(field('Pattern (named groups)', rule.pattern, 'rule-pattern'),
             field('Artist template', rule.artist, 'rule-artist'),
             field('Title template', rule.title, 'rule-title'));
  const remove = el('button', { type: 'button', class: 'danger' }, 'Remove rule');
  remove.addEventListener('click', () => row.remove());
  row.append(remove);
  return row;
}

function channelCard(channel = { id: '', name: '', language: 'en', rules: [] }) {
  const card = el('div', { class: 'channel' });
  const details = el('div', { class: 'row' });
  details.append(field('Permanent channel ID', channel.id, 'channel-id'),
                 field('Display name', channel.name, 'channel-name'),
                 field('Language', channel.language, 'channel-language'));
  const up = el('button', { type: 'button', class: 'subtle' }, 'Move up');
  up.addEventListener('click', () => {
    if (card.previousElementSibling) {
      $('#channels').insertBefore(card, card.previousElementSibling);
      renderPreviewChannels();
    }
  });
  const down = el('button', { type: 'button', class: 'subtle' }, 'Move down');
  down.addEventListener('click', () => {
    if (card.nextElementSibling) {
      $('#channels').insertBefore(card.nextElementSibling, card);
      renderPreviewChannels();
    }
  });
  const remove = el('button', { type: 'button', class: 'danger' }, 'Remove channel');
  remove.addEventListener('click', () => { card.remove(); renderPreviewChannels(); });
  details.append(up, down, remove);
  const rules = el('div', { class: 'rules' });
  rules.replaceChildren(...channel.rules.map(ruleRow));
  const add = el('button', { type: 'button', class: 'subtle' }, 'Add parsing rule');
  add.addEventListener('click', () => rules.append(ruleRow()));
  card.append(details, rules, add);
  $('.channel-id', card).addEventListener('change', renderPreviewChannels);
  $('.channel-name', card).addEventListener('change', renderPreviewChannels);
  return card;
}

function renderPreviewChannels() {
  const select = $('#preview-channel');
  const previous = select.value;
  const options = [el('option', { value: '' }, 'Generic fallback')];
  $$('#channels .channel').forEach(card => options.push(el('option', { value: $('.channel-id', card).value }, $('.channel-name', card).value || 'Unnamed')));
  select.replaceChildren(...options);
  select.value = previous;
}

function $$(selector, root = document) { return [...root.querySelectorAll(selector)]; }

function collectProfile() {
  const profile = structuredClone(current());
  profile.name = $('#profile-name').value.trim();
  profile.output_dir = $('#output-dir').value.trim();
  profile.search_terms = $('#search-terms').value.split(',').map(value => value.trim()).filter(Boolean);
  profile.search_count = Number($('#search-count').value);
  profile.min_height = $('#min-height').value ? Number($('#min-height').value) : null;
  profile.max_height = $('#max-height').value ? Number($('#max-height').value) : null;
  profile.quality_order = $$('#quality-order li').map(item => Number(item.dataset.height));
  profile.channels = $$('#channels .channel').map(card => ({
    id: $('.channel-id', card).value.trim(),
    name: $('.channel-name', card).value.trim(),
    language: $('.channel-language', card).value.trim(),
    rules: $$('.rule', card).map(row => ({
      pattern: $('.rule-pattern', row).value,
      artist: $('.rule-artist', row).value,
      title: $('.rule-title', row).value,
    })),
  }));
  return profile;
}

function renderKeys() {
  $('#new-secret').textContent = '';
  $('#keys').replaceChildren(...(state.keys[selected] || []).map(key => {
    const row = el('div', { class: 'key' });
    row.append(el('span', {}, `${key.label} · ${key.id.slice(0, 8)} · ${key.revoked ? 'revoked' : 'active'}`));
    if (!key.revoked) {
      const folder = el('label', {}, 'Output directory');
      const input = el('input', { placeholder: `Default: ${current().output_dir}` });
      input.value = key.output_dir || '';
      folder.append(input);
      const browse = el('button', { type: 'button', class: 'subtle' }, 'Browse…');
      browse.addEventListener('click', () => openFolderPicker(input, current().output_dir));
      const save = el('button', { type: 'button' }, 'Save folder');
      save.addEventListener('click', async () => {
        try {
          await api('PUT', `/admin/api/profiles/${selected}/keys/${key.id}`,
                    { output_dir: input.value.trim() || null });
          await reload(); notice('Client folder saved');
        } catch (error) { notice(error.message, true); }
      });
      const button = el('button', { type: 'button', class: 'danger' }, 'Revoke');
      button.addEventListener('click', async () => {
        if (!confirm(`Revoke key for ${key.label}?`)) return;
        try { await api('DELETE', `/admin/api/profiles/${selected}/keys/${key.id}`); await reload(); notice('Key revoked'); }
        catch (error) { notice(error.message, true); }
      });
      row.append(folder, browse, save, button);
    }
    return row;
  }));
}

$('#profile-select').addEventListener('change', event => { selected = event.target.value; render(); });
$('#browse-output-dir').addEventListener('click', () => openFolderPicker($('#output-dir'), current().output_dir));
$('#browse-key-output-dir').addEventListener('click', () => openFolderPicker($('#key-output-dir'), $('#output-dir').value.trim()));
$('#folder-up').addEventListener('click', () => {
  if (folderParent) loadFolder(folderParent).catch(showFolderError);
});
$('#folder-select').addEventListener('click', () => {
  if (!folderTarget || !folderCurrent) return;
  folderTarget.value = folderCurrent;
  folderTarget.dispatchEvent(new Event('change', { bubbles: true }));
  $('#folder-dialog').close();
});
$('#folder-cancel').addEventListener('click', () => $('#folder-dialog').close());
$('#folder-dialog').addEventListener('close', () => {
  folderRequest += 1;
  folderTarget = null;
  folderCurrent = null;
  folderParent = null;
});
$('#add-channel').addEventListener('click', () => { $('#channels').append(channelCard()); renderPreviewChannels(); });
for (const id of ['min-height', 'max-height']) $(`#${id}`).addEventListener('change', () => {
  const min = Number($('#min-height').value) || 0;
  const max = Number($('#max-height').value) || Infinity;
  const allowed = heights.filter(height => height >= min && height <= max);
  const previous = $$('#quality-order li').map(item => Number(item.dataset.height));
  renderQuality([...previous.filter(height => allowed.includes(height)), ...allowed.filter(height => !previous.includes(height)).reverse()]);
});
$('#profile-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await api('PUT', `/admin/api/profiles/${selected}`, collectProfile()); await reload(); notice('Profile saved'); }
  catch (error) { notice(error.message, true); }
});
$('#create-profile').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const profile = structuredClone(current());
    profile.slug = $('#new-slug').value.trim(); profile.name = $('#new-name').value.trim();
    profile.output_dir = profile.output_dir.replace(/\/[^/]+$/, `/${profile.slug}`);
    await api('POST', '/admin/api/profiles', profile);
    selected = profile.slug; await reload(); notice('Profile created');
  } catch (error) { notice(error.message, true); }
});
$('#delete-profile').addEventListener('click', async () => {
  if (!confirm(`Delete profile ${selected}, its keys and download history? Media files stay on disk.`)) return;
  try { await api('DELETE', `/admin/api/profiles/${selected}`); selected = null; await reload(); notice('Profile deleted'); }
  catch (error) { notice(error.message, true); }
});
$('#preview-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const result = await api('POST', '/admin/api/preview', { profile: collectProfile(), title: $('#preview-title').value, channel_id: $('#preview-channel').value });
    $('#preview-result').textContent = `Artist: ${result.artist}\nTitle: ${result.title}`;
  } catch (error) { notice(error.message, true); }
});
$('#new-key').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    const result = await api('POST', `/admin/api/profiles/${selected}/keys`, {
      label: $('#key-label').value, output_dir: $('#key-output-dir').value.trim() || null,
    });
    await reload(); $('#new-secret').textContent = `Copy this key now; it will not be shown again: ${result.secret}`;
    notice('Key created');
  } catch (error) { notice(error.message, true); }
});
$('#update-config').addEventListener('submit', async event => {
  event.preventDefault();
  try { await api('POST', '/admin/api/updates/config', { channel: $('#update-channel').value, interval_hours: Number($('#update-interval').value) }); await reload(); notice('Update schedule saved'); }
  catch (error) { notice(error.message, true); }
});
$('#run-update').addEventListener('click', async () => {
  try { await api('POST', '/admin/api/updates/run'); await reload(); notice('Update started'); }
  catch (error) { notice(error.message, true); }
});
$('#password-form').addEventListener('submit', async event => {
  event.preventDefault();
  try { await api('POST', '/admin/api/password', { current: $('#password-current').value, new: $('#password-new').value }); window.location.href = '/login'; }
  catch (error) { notice(error.message, true); }
});
$('#logout').addEventListener('click', async () => { await api('POST', '/admin/api/logout'); window.location.href = '/login'; });
reload().catch(error => notice(error.message, true));

(() => {
  const ROOM_KEY = window.ROOM_KEY;
  const IS_ADMIN = !!window.IS_ADMIN;
  const ME = window.ME || { nick: '', masked_ip: '' };

  const $ = (sel) => document.querySelector(sel);

  const elShare = $('#shareUrl');
  const elCopyShare = $('#copyShare');
  const elNickInput = $('#nickInput');
  const elSaveNick = $('#saveNick');
  const elMyIpMasked = $('#myIpMasked');
  const elMeBadge = $('#meBadge');
  const elOnlineCount = $('#onlineCount');
  const elOnlineList = $('#onlineList');
  const elRoomState = $('#roomState');
  const elReviewBadge = $('#reviewBadge');
  const elAddItemForm = $('#addItemForm');
  const elItemsBox = $('#itemsBox');
  const elSpinsBox = $('#spinsBox');
  const elSpinBtn = $('#spinBtn');
  const elSpinResult = $('#spinResult');

  const elReviewStatus = $('#reviewStatus');
  const elJoinReqBox = $('#joinReqBox');

  const elAddColorPicker = $('#addColorPicker');
  const elAddColorText = $('#addColorText');

  const wheelInner = $('#wheelInner');
  const canvas = $('#wheelCanvas');
  const ctx = canvas.getContext('2d');

  let snapshot = {
    room: { room_name: '', review_enabled: false },
    me: ME,
    state: { is_spinning: false },
    items: [],
    spins: [],
    online: [],
    join_requests: []
  };

  let wheel = {
    currentRotation: 0,
    segments: [],
    lastSpinId: 0,
    spinning: false,
    imageCache: new Map(),
  };

  function escapeHtml(str) {
    return String(str || '').replace(/[&<>'"]/g, (c) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      "'": '&#39;',
      '"': '&quot;'
    }[c]));
  }

  function formatTime(iso) {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      const pad = (n) => String(n).padStart(2, '0');
      return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    } catch {
      return iso;
    }
  }

  function hashHue(id) {
    const n = Number(id) || 0;
    return (n * 67) % 360;
  }

  function isValidHexColor(s) {
    return /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(String(s || '').trim());
  }

  function normalizeHex(s) {
    const t = String(s || '').trim();
    if (!isValidHexColor(t)) return null;
    if (t.length === 4) {
      return `#${t[1]}${t[1]}${t[2]}${t[2]}${t[3]}${t[3]}`.toLowerCase();
    }
    return t.toLowerCase();
  }

  function hexToRgb(hex) {
    const h = normalizeHex(hex);
    if (!h) return null;
    const v = parseInt(h.slice(1), 16);
    return {
      r: (v >> 16) & 255,
      g: (v >> 8) & 255,
      b: v & 255,
    };
  }

  function luminance(rgb) {
    // https://www.w3.org/TR/WCAG20/#relativeluminancedef
    const srgb = [rgb.r, rgb.g, rgb.b].map((x) => {
      const c = x / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * srgb[0] + 0.7152 * srgb[1] + 0.0722 * srgb[2];
  }

  function textColorFor(bg) {
    const rgb = hexToRgb(bg);
    if (!rgb) return '#111827';
    return luminance(rgb) > 0.6 ? '#111827' : '#ffffff';
  }

  function itemColor(it) {
    const c = (it && it.color) ? String(it.color).trim() : '';
    if (isValidHexColor(c)) return normalizeHex(c);
    // fallback: old style hue
    const hue = hashHue(it.id);
    return `hsl(${hue} 70% 85%)`;
  }

  function api(path, options = {}) {
    return fetch(path, {
      credentials: 'include',
      ...options,
    });
  }

  async function fetchSnapshot() {
    const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/snapshot`);
    if (!res.ok) {
      let msg = '加载失败';
      try {
        const data = await res.json();
        msg = data.error || msg;
      } catch {}
      elRoomState.textContent = msg;
      return;
    }
    const data = await res.json();
    if (!data.ok) {
      elRoomState.textContent = data.error || '加载失败';
      return;
    }
    snapshot = data;
    updateUIFromSnapshot();
  }

  function updateUIFromSnapshot() {
    // Me
    elNickInput.value = snapshot.me.nick || '';
    elMyIpMasked.textContent = snapshot.me.masked_ip || '';
    elMeBadge.textContent = `${snapshot.me.nick || '我'} (${snapshot.me.masked_ip || ''})`;

    // Review badge
    const reviewEnabled = !!snapshot.room.review_enabled;
    elReviewBadge.innerHTML = reviewEnabled
      ? `审查：<span class="pill" style="border-color: rgba(225,29,72,.25);">开启</span>`
      : `审查：<span class="pill">关闭</span>`;

    // Online
    renderOnline(snapshot.online || []);

    // State
    renderState(snapshot.state || {});

    // Items / wheel
    renderItems(snapshot.items || []);
    computeSegments();
    drawWheel();

    // Spins
    renderSpins(snapshot.spins || []);

    // Join requests
    renderJoinRequests(snapshot.join_requests || [], reviewEnabled);
  }

  function renderOnline(list) {
    elOnlineCount.textContent = String(list.length || 0);

    if (!list || list.length === 0) {
      elOnlineList.innerHTML = '<div class="muted">暂无在线用户</div>';
      return;
    }

    const html = list.map((u) => {
      const who = escapeHtml(u.nick || '');
      const ip = escapeHtml(u.ip || '');
      const adminBadge = u.is_admin ? '<span class="pill" style="margin-left:6px;">管理员</span>' : '';

      let actions = '';
      if (IS_ADMIN && !u.is_admin) {
        actions = `
          <div class="join-req-actions" style="margin-top:6px;">
            <button class="btn-mini js-kick" data-uid="${escapeHtml(u.uid)}" type="button">踢出</button>
            <button class="btn-mini btn-danger js-ban-room" data-uid="${escapeHtml(u.uid)}" type="button">房间封禁</button>
            <button class="btn-mini btn-danger js-ban-global" data-uid="${escapeHtml(u.uid)}" type="button">全局封禁</button>
          </div>
        `;
      }

      return `
        <div class="online-item">
          <div>
            <div><strong>${who}</strong>${adminBadge}</div>
            <div class="muted small">${ip}</div>
            ${actions}
          </div>
        </div>
      `;
    }).join('');

    elOnlineList.innerHTML = html;
  }

  function renderState(state) {
    if (state.is_spinning) {
      const by = escapeHtml(state.spinning_by_nick || '有人');
      const ip = escapeHtml(state.spinning_by_ip || '');
      elRoomState.innerHTML = `正在转动中：<strong>${by}</strong> <span class="muted">(${ip})</span>`;
    } else {
      elRoomState.textContent = '空闲（可增删改项目，也可以开始转动）';
    }
    wheel.spinning = !!state.is_spinning;
    elSpinBtn.disabled = !!state.is_spinning;
    elAddItemForm.querySelectorAll('input,button,select,textarea').forEach((el) => {
      // allow nickname controls elsewhere
      if (el.closest('#addItemForm')) {
        el.disabled = !!state.is_spinning;
      }
    });
  }

  function renderItems(items) {
    if (!items || items.length === 0) {
      elItemsBox.innerHTML = '<div class="muted">暂无项目，先添加一个吧。</div>';
      return;
    }

    const total = items.reduce((s, it) => s + (Number(it.weight) > 0 ? Number(it.weight) : 0), 0) || 0;

    const html = items.map((it) => {
      const p = total > 0 ? ((Number(it.weight) > 0 ? Number(it.weight) : 0) / total * 100).toFixed(1) : '0.0';
      const color = itemColor(it);
      const swatch = isValidHexColor(color) ? `<span class="color-swatch" style="background:${color}"></span>` : '';

      const img = it.image_url ? `<img class="item-thumb" src="${escapeHtml(it.image_url)}" alt="" />` : '';
      const by = escapeHtml(it.created_by_nick || '');
      const ip = escapeHtml(it.created_by_ip || '');
      const meta = `<div class="muted small">by ${by} · ${ip} · ${formatTime(it.updated_at)}</div>`;

      const colorInput = `
        <div class="row" style="gap:8px; flex-wrap:wrap;">
          <label class="pill" style="display:flex; align-items:center; gap:8px;">
            ${swatch}颜色
            <input class="js-color" type="color" value="${escapeHtml(normalizeHex(color) || '#ffcc00')}" data-id="${it.id}" />
          </label>
          <span class="pill">概率：${p}%</span>
        </div>
      `;

      return `
        <div class="item">
          ${img}
          <div class="item-body">
            <div class="item-title">
              <input class="input js-text" data-id="${it.id}" value="${escapeHtml(it.text)}" />
              <button class="btn-mini js-del" data-id="${it.id}" type="button">删除</button>
            </div>

            <div class="row" style="gap:10px; flex-wrap:wrap;">
              <label class="pill">权重 <input class="input js-weight" data-id="${it.id}" type="number" step="0.1" min="0" value="${escapeHtml(it.weight)}" style="width:110px;" /></label>
              ${colorInput}
            </div>

            <div class="row" style="gap:10px; flex-wrap:wrap;">
              <label class="pill">图片链接 <input class="input js-image" data-id="${it.id}" type="url" value="${escapeHtml(it.image_url || '')}" placeholder="https://..." style="min-width:220px;" /></label>
              <button class="btn-mini js-save" data-id="${it.id}" type="button">保存</button>
              <button class="btn-mini js-clear-image" data-id="${it.id}" type="button">清除图片</button>
            </div>
            ${meta}
          </div>
        </div>
      `;
    }).join('');

    elItemsBox.innerHTML = html;
  }

  function renderSpins(spins) {
    if (!spins || spins.length === 0) {
      elSpinsBox.innerHTML = '<div class="muted">暂无记录</div>';
      return;
    }

    const html = spins.map((s) => {
      const by = escapeHtml(s.created_by_nick || '');
      const ip = escapeHtml(s.created_by_ip || '');
      const t = formatTime(s.created_at);
      const text = escapeHtml(s.item_text_snapshot || '(空)');
      const img = s.item_image_snapshot ? `<img class="spin-thumb" src="${escapeHtml(s.item_image_snapshot)}" alt="" />` : '';
      return `
        <div class="spin">
          ${img}
          <div>
            <div><strong>${text}</strong></div>
            <div class="muted small">${t} · ${by} · ${ip}</div>
          </div>
        </div>
      `;
    }).join('');

    elSpinsBox.innerHTML = html;
  }

  function renderJoinRequests(list, reviewEnabled) {
    if (!elReviewStatus || !elJoinReqBox) return;

    if (!reviewEnabled) {
      elReviewStatus.textContent = '当前未开启审查：任何人都可以直接进入房间。';
      elJoinReqBox.innerHTML = '<div class="muted">（未开启审查，无待处理请求）</div>';
      return;
    }

    elReviewStatus.textContent = '当前已开启审查：新用户进入会先发起加入请求，房间内任意在线用户都可以同意/拒绝。';

    if (!list || list.length === 0) {
      elJoinReqBox.innerHTML = '<div class="muted">暂无待处理加入请求</div>';
      return;
    }

    const html = list.map((r) => {
      const nick = escapeHtml(r.requester_nick || '游客');
      const ip = escapeHtml(r.requester_ip || '');
      const t = formatTime(r.created_at);
      return `
        <div class="join-req-item">
          <div>
            <div><strong>${nick}</strong> <span class="muted">(${ip})</span></div>
            <div class="muted small">请求时间：${t} · ID: <code>#${r.id}</code></div>
          </div>
          <div class="join-req-actions">
            <button class="btn-mini js-req-approve" data-id="${r.id}" type="button">同意</button>
            <button class="btn-mini btn-danger js-req-reject" data-id="${r.id}" type="button">拒绝</button>
          </div>
        </div>
      `;
    }).join('');

    elJoinReqBox.innerHTML = html;
  }

  function computeSegments() {
    const items = snapshot.items || [];
    const eligible = items.filter((it) => Number(it.weight) > 0);

    wheel.segments = eligible.map((it) => ({
      id: it.id,
      text: it.text,
      image_url: it.image_url,
      weight: Number(it.weight) || 0,
      color: itemColor(it),
    }));
  }

  function pickWeighted(segments) {
    const sum = segments.reduce((s, x) => s + (x.weight > 0 ? x.weight : 0), 0);
    if (sum <= 0) return null;
    let r = Math.random() * sum;
    for (const s of segments) {
      if (s.weight <= 0) continue;
      r -= s.weight;
      if (r <= 0) return s;
    }
    return segments[segments.length - 1] || null;
  }

  function drawWheel() {
    const segments = wheel.segments;
    const size = canvas.width;
    const cx = size / 2;
    const cy = size / 2;
    const radius = size / 2 - 6;

    ctx.clearRect(0, 0, size, size);

    // background
    ctx.beginPath();
    ctx.arc(cx, cy, radius + 2, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();

    if (!segments || segments.length === 0) {
      ctx.fillStyle = '#6b7280';
      ctx.font = '16px system-ui';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText('暂无可转动项目', cx, cy);
      return;
    }

    const total = segments.reduce((s, it) => s + it.weight, 0);
    let startAngle = wheel.currentRotation;

    segments.forEach((seg, idx) => {
      const angle = (seg.weight / total) * Math.PI * 2;
      const endAngle = startAngle + angle;

      // segment fill
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, radius, startAngle, endAngle);
      ctx.closePath();

      const fill = seg.color || `hsl(${hashHue(seg.id)} 70% 85%)`;
      ctx.fillStyle = fill;
      ctx.fill();

      // border
      ctx.strokeStyle = 'rgba(17,24,39,0.08)';
      ctx.lineWidth = 2;
      ctx.stroke();

      // text
      const mid = (startAngle + endAngle) / 2;
      const tx = cx + Math.cos(mid) * (radius * 0.62);
      const ty = cy + Math.sin(mid) * (radius * 0.62);

      ctx.save();
      ctx.translate(tx, ty);
      ctx.rotate(mid);
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      const txt = String(seg.text || '').slice(0, 14);

      ctx.font = 'bold 14px system-ui';
      ctx.fillStyle = textColorFor(fill);
      ctx.fillText(txt, 0, 0);

      ctx.restore();

      startAngle = endAngle;
    });

    // center cap
    ctx.beginPath();
    ctx.arc(cx, cy, 40, 0, Math.PI * 2);
    ctx.fillStyle = '#ffffff';
    ctx.fill();
    ctx.strokeStyle = 'rgba(17,24,39,0.12)';
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.fillStyle = '#111827';
    ctx.font = 'bold 14px system-ui';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('SPIN', cx, cy);
  }

  function animateSpinTo(targetIndex) {
    // Smooth spin animation: rotate to make targetIndex land at pointer (top)
    const segments = wheel.segments;
    if (!segments || segments.length === 0) return;

    const totalWeight = segments.reduce((s, it) => s + it.weight, 0);

    // compute angle of center of target segment from start 0
    let acc = 0;
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i];
      const ang = (seg.weight / totalWeight) * Math.PI * 2;
      if (i === targetIndex) {
        acc += ang / 2;
        break;
      }
      acc += ang;
    }

    // pointer is at -90deg (top)
    const pointerAngle = -Math.PI / 2;

    // desired rotation such that segment center equals pointerAngle
    let desired = pointerAngle - acc;

    // add extra rotations
    const extra = Math.PI * 2 * (4 + Math.floor(Math.random() * 2));
    desired += extra;

    const start = wheel.currentRotation;
    const delta = desired - start;

    const duration = 6000; // ms
    const startTime = performance.now();

    function easeOutCubic(t) {
      return 1 - Math.pow(1 - t, 3);
    }

    function step(now) {
      const t = Math.min(1, (now - startTime) / duration);
      const eased = easeOutCubic(t);
      wheel.currentRotation = start + delta * eased;
      drawWheel();
      if (t < 1) {
        requestAnimationFrame(step);
      }
    }

    requestAnimationFrame(step);
  }

  async function spin() {
    if (wheel.spinning) return;
    elSpinResult.textContent = '';

    if (!wheel.segments || wheel.segments.length === 0) {
      elSpinResult.textContent = '没有可转动的项目（权重需 > 0）';
      return;
    }

    wheel.spinning = true;
    elSpinBtn.disabled = true;

    try {
      const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/spin`, { method: 'POST' });
      const data = await res.json();
      if (!res.ok || !data.ok) {
        throw new Error(data.error || 'spin failed');
      }

      // Local prediction: choose same item index if we can match snapshot id
      const spin = data.spin;
      const idx = wheel.segments.findIndex((s) => s.id === spin.item_id);
      if (idx >= 0) {
        animateSpinTo(idx);
      } else {
        // fallback
        animateSpinTo(Math.floor(Math.random() * wheel.segments.length));
      }

      elSpinResult.innerHTML = `已开始转动：<strong>${escapeHtml(spin.created_by_nick || '')}</strong>`;
    } catch (e) {
      elSpinResult.textContent = e.message || '转动失败';
      wheel.spinning = false;
      elSpinBtn.disabled = false;
    }
  }

  async function heartbeat(nick) {
    const body = JSON.stringify({ nick: nick || elNickInput.value || '' });
    const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/heartbeat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body
    });
    if (!res.ok) return;
    const data = await res.json();
    if (!data.ok) return;
    snapshot.me = data.me;
    elMyIpMasked.textContent = snapshot.me.masked_ip || '';
    elMeBadge.textContent = `${snapshot.me.nick || '我'} (${snapshot.me.masked_ip || ''})`;
  }

  async function decideJoin(requestId, decision) {
    const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/join_requests/${requestId}/decide`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      alert(data.error || '操作失败');
      return;
    }
    // refresh
    fetchSnapshot();
  }

  async function adminModerate(targetUid, banScope) {
    const payload = { target_uid: targetUid };
    if (banScope) payload.ban_scope = banScope;

    const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/admin/kick`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      alert(data.error || '操作失败');
      return;
    }
    fetchSnapshot();
  }

  // ===== Event bindings =====

  elCopyShare?.addEventListener('click', () => {
    const text = elShare.value;
    navigator.clipboard.writeText(text).then(() => {
      elCopyShare.textContent = '已复制';
      setTimeout(() => (elCopyShare.textContent = '复制'), 1200);
    }).catch(() => {
      alert('复制失败，请手动复制：\n' + text);
    });
  });

  elSaveNick?.addEventListener('click', () => {
    heartbeat(elNickInput.value);
  });

  // color picker on add item: only write into text field when user changes picker
  if (elAddColorPicker && elAddColorText) {
    elAddColorPicker.addEventListener('input', () => {
      elAddColorText.value = elAddColorPicker.value;
    });
  }

  elAddItemForm?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(elAddItemForm);

    // if user didn't type color, keep blank so server can randomize
    const color = String(fd.get('color') || '').trim();
    if (!color) {
      fd.delete('color');
    }

    const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/items`, {
      method: 'POST',
      body: fd
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      alert(data.error || '添加失败');
      return;
    }
    elAddItemForm.reset();
    if (elAddColorText) elAddColorText.value = '';
    await fetchSnapshot();
  });

  elItemsBox?.addEventListener('click', async (e) => {
    const del = e.target.closest('.js-del');
    const save = e.target.closest('.js-save');
    const clear = e.target.closest('.js-clear-image');

    if (del) {
      const id = del.getAttribute('data-id');
      if (!confirm('确定删除该项目吗？（管理员可在日志中恢复）')) return;
      const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/items/${id}`, {
        method: 'POST'
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        alert(data.error || '删除失败');
        return;
      }
      fetchSnapshot();
      return;
    }

    if (clear) {
      const id = clear.getAttribute('data-id');
      const payload = { clear_image: true };
      const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/items/${id}?_method=PATCH`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        alert(data.error || '操作失败');
        return;
      }
      fetchSnapshot();
      return;
    }

    if (save) {
      const id = save.getAttribute('data-id');
      const row = save.closest('.item');
      if (!row) return;

      const textEl = row.querySelector(`.js-text[data-id="${id}"]`);
      const weightEl = row.querySelector(`.js-weight[data-id="${id}"]`);
      const imgEl = row.querySelector(`.js-image[data-id="${id}"]`);
      const colorEl = row.querySelector(`.js-color[data-id="${id}"]`);

      const payload = {
        text: (textEl?.value || '').trim(),
        weight: Number(weightEl?.value || 0),
        image_url: (imgEl?.value || '').trim(),
        color: colorEl?.value || null,
      };

      const res = await api(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/items/${id}?_method=PATCH`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) {
        alert(data.error || '保存失败');
        return;
      }
      fetchSnapshot();
      return;
    }
  });

  elSpinBtn?.addEventListener('click', spin);

  // join requests decisions
  elJoinReqBox?.addEventListener('click', (e) => {
    const approve = e.target.closest('.js-req-approve');
    const reject = e.target.closest('.js-req-reject');
    if (approve) {
      const id = Number(approve.getAttribute('data-id'));
      decideJoin(id, 'approve');
      return;
    }
    if (reject) {
      const id = Number(reject.getAttribute('data-id'));
      decideJoin(id, 'reject');
      return;
    }
  });

  // admin moderate actions from online list
  elOnlineList?.addEventListener('click', (e) => {
    const kick = e.target.closest('.js-kick');
    const banRoom = e.target.closest('.js-ban-room');
    const banGlobal = e.target.closest('.js-ban-global');

    if (kick) {
      adminModerate(kick.getAttribute('data-uid'), null);
      return;
    }
    if (banRoom) {
      if (!confirm('确定房间封禁该用户 IP 吗？')) return;
      adminModerate(banRoom.getAttribute('data-uid'), 'room');
      return;
    }
    if (banGlobal) {
      if (!confirm('确定全局封禁该用户 IP 吗？')) return;
      adminModerate(banGlobal.getAttribute('data-uid'), 'global');
      return;
    }
  });

  // ===== SSE =====
  function connectSSE() {
    const es = new EventSource(`/api/rooms/${encodeURIComponent(ROOM_KEY)}/events`, { withCredentials: true });

    es.addEventListener('hello', () => {
      // no-op
    });

    es.addEventListener('items_changed', () => fetchSnapshot());
    es.addEventListener('spins_changed', () => fetchSnapshot());
    es.addEventListener('state_changed', () => fetchSnapshot());
    es.addEventListener('online_changed', () => fetchSnapshot());

    es.addEventListener('join_request', () => fetchSnapshot());
    es.addEventListener('join_request_decided', () => fetchSnapshot());
    es.addEventListener('review_changed', () => fetchSnapshot());

    es.addEventListener('kicked', (e) => {
      try {
        const payload = JSON.parse(e.data || '{}');
        if (payload && payload.target_uid && payload.target_uid === snapshot.me.uid) {
          alert(payload.message || '你已被管理员踢出');
          window.location.href = `/kicked?key=${encodeURIComponent(ROOM_KEY)}`;
        }
      } catch {}
    });

    es.onerror = () => {
      // auto-reconnect handled by browser; also do periodic snapshot
    };
  }

  // Initial
  elMyIpMasked.textContent = ME.masked_ip || '';
  elMeBadge.textContent = `${ME.nick || '我'} (${ME.masked_ip || ''})`;

  fetchSnapshot();
  connectSSE();

  // Heartbeat interval
  setInterval(() => heartbeat(), 15000);

})();

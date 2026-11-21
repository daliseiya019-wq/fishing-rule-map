// 釣り規制マップ：公式に「釣りをしてよい」と公表された場所だけを表示する。
// データはすべて静的 JSON。表示は textContent で組み立て、HTML 文字列は使わない（XSS 対策）。
(function () {
  'use strict';

  const STATUS = {
    // 記号は小さいマーカーの中でも潰れずに読めるものを選ぶ（○ は円の中で判別しにくい）
    open: { label: '常設の釣り場', mark: 'OK' },
    limited: { label: '決まった日だけ開放', mark: '日' },
    unconfirmed: { label: '今年の開放を確認できず', mark: '？' },
  };
  const CHECKED_HOW = { page: 'ページ', search: '検索' };
  const TOHOKU_CENTER = [39.3, 140.8];

  // ---- 小さな DOM ヘルパー ----
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [key, value] of Object.entries(attrs)) {
        if (value == null) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else node.setAttribute(key, value);
      }
    }
    for (const child of children || []) {
      if (child != null) node.append(child);
    }
    return node;
  }

  // データに想定外の URL が混ざっても javascript: などを出さないよう、https だけ許可する
  function safeLink(url, text) {
    let href = null;
    try {
      const parsed = new URL(url);
      if (parsed.protocol === 'https:') href = parsed.href;
    } catch (e) { /* 不正な URL はリンクにしない */ }
    if (!href) return el('span', { text: text });
    return el('a', { href: href, target: '_blank', rel: 'noopener noreferrer', text: text });
  }

  // 日本時間の日付（YYYY-MM-DD）。データの日付は日本時間で書いているので、比較も日本時間でそろえる
  const jstDateFormat = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Tokyo' });
  function jstDate(date) {
    return jstDateFormat.format(date);
  }

  function formatDate(iso) {
    if (!iso) return '記載なし';
    const [y, m, d] = iso.split('-');
    return `${y}年${Number(m)}月${Number(d)}日`;
  }

  // ---- 出典の状態（期限切れ・出典ページの変更） ----
  function expiryAlerts(validUntil, today) {
    return validUntil && validUntil < today ? [`有効期限（${formatDate(validUntil)}）を過ぎています`] : [];
  }

  // 注: 確認日は日付単位のため、確認したのと同じ日に出典が変わった場合は検知できない
  function sourceAlerts(entry, sourceStatus, today) {
    const alerts = expiryAlerts(entry.valid_until, today);
    const status = sourceStatus[entry.url];
    const changedAt = status && status.changed_at ? new Date(status.changed_at) : null;
    const changedOn = changedAt && !Number.isNaN(changedAt.getTime()) ? jstDate(changedAt) : null;
    if (changedOn && changedOn > entry.checked) {
      alerts.push(`出典が更新されました（${formatDate(changedOn)}に検知）。内容を再確認中です`);
    }
    if (status && status.error) {
      alerts.push('最新の自動確認で出典ページを開けませんでした（一時的な不具合の場合もあります）');
    }
    return alerts;
  }

  function renderSource(src, sourceStatus, today) {
    const alerts = sourceAlerts(src, sourceStatus, today);
    return el('div', null, [
      el('p', null, ['出典: ', safeLink(src.url, src.title), `（${src.publisher}）`]),
      el('p', {
        text: `掲載日: ${formatDate(src.published)} ／ 確認日: ${formatDate(src.checked)}` +
          `（確認方法: ${CHECKED_HOW[src.checked_how] || '不明'}）`,
      }),
      ...alerts.map((a) => el('p', { class: 'alert-line', text: `⚠ ${a}` })),
    ]);
  }

  function statusBadge(status) {
    const s = STATUS[status] || STATUS.unconfirmed;
    return el('span', { class: `badge status-${status in STATUS ? status : 'unconfirmed'}`, text: `${s.mark} ${s.label}` });
  }

  // 一覧のカードと地図のポップアップで同じ中身を使う
  function spotDetails(p, sourceStatus, today) {
    return [
      el('p', { class: 'meta', text: `${p.prefecture} ／ ${p.port}` }),
      statusBadge(p.status),
      ...expiryAlerts(p.valid_until, today).map((a) => el('p', { class: 'alert-line', text: `⚠ ${a}` })),
      el('p', { text: p.summary }),
      el('ul', null, p.conditions.map((c) => el('li', { text: c }))),
      el('dl', null, [
        el('dt', { text: '時間' }), el('dd', { text: p.hours }),
        el('dt', { text: '料金' }), el('dd', { text: p.fee }),
        el('dt', { text: '地図の位置' }), el('dd', { text: p.position.note }),
      ]),
      el('div', { class: 'sources' }, p.sources.map((s) => renderSource(s, sourceStatus, today))),
    ];
  }

  // ---- 地図 ----
  function createMap() {
    const map = L.map('map', { scrollWheelZoom: false }).setView(TOHOKU_CENTER, 7);
    L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener noreferrer">国土地理院</a>',
    }).addTo(map);
    return map;
  }

  function markerIcon(status) {
    const key = status in STATUS ? status : 'unconfirmed';
    const icon = el('div', { class: `spot-marker status-${key}`, text: STATUS[key].mark });
    return L.divIcon({ html: icon.outerHTML, className: '', iconSize: [28, 28], iconAnchor: [14, 14], popupAnchor: [0, -14] });
  }

  // ---- 一覧 ----
  function renderList(features, markers, map, sourceStatus, today) {
    const list = document.getElementById('spot-list');
    const prefSelect = document.getElementById('filter-pref');
    const statusSelect = document.getElementById('filter-status');

    const prefs = [...new Set(features.map((f) => f.properties.prefecture))];
    for (const pref of prefs) prefSelect.append(el('option', { value: pref, text: pref }));

    function draw() {
      const pref = prefSelect.value;
      const status = statusSelect.value;
      list.replaceChildren();
      const shown = features.filter((f) =>
        (!pref || f.properties.prefecture === pref) && (!status || f.properties.status === status));

      for (const f of features) {
        const marker = markers.get(f.properties.id);
        if (shown.includes(f)) marker.addTo(map); else marker.remove();
      }
      if (shown.length === 0) {
        list.append(el('li', { class: 'card', text: 'この条件に合う場所はありません。' }));
        return;
      }
      for (const f of shown) {
        const p = f.properties;
        const button = el('button', { type: 'button', class: 'show-on-map', text: '地図で見る' });
        button.addEventListener('click', () => {
          const marker = markers.get(p.id);
          map.setView(marker.getLatLng(), 13);
          marker.openPopup();
          document.getElementById('map').scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
        list.append(el('li', { class: 'card' }, [
          el('h3', { text: p.name }),
          ...spotDetails(p, sourceStatus, today),
          button,
        ]));
      }
    }

    prefSelect.addEventListener('change', draw);
    statusSelect.addEventListener('change', draw);
    draw();
  }

  // ---- 県ごとの公式ルール索引 ----
  function renderIndex(index, sourceStatus, today) {
    const root = document.getElementById('rule-index');
    for (const pref of index.prefectures) {
      const items = pref.links.map((link) => {
        const alerts = sourceAlerts(link, sourceStatus, today);
        return el('li', null, [
          el('span', { class: 'badge status-unconfirmed', text: link.kind }), ' ',
          safeLink(link.url, link.label),
          el('div', {
            class: 'meta',
            text: `${link.publisher} ／ 掲載日: ${formatDate(link.published)} ／ 確認日: ${formatDate(link.checked)}` +
              `（${CHECKED_HOW[link.checked_how] || '不明'}）`,
          }),
          link.memo ? el('div', { class: 'meta', text: `メモ: ${link.memo}` }) : null,
          ...alerts.map((a) => el('div', { class: 'alert-line', text: `⚠ ${a}` })),
        ]);
      });
      root.append(el('div', { class: 'pref-block' }, [el('h3', { text: pref.name }), el('ul', null, items)]));
    }
  }

  async function loadJson(path) {
    const res = await fetch(path, { cache: 'no-cache' });
    if (!res.ok) throw new Error(`${path}: ${res.status}`);
    return res.json();
  }

  async function main() {
    const today = jstDate(new Date());
    const map = createMap();
    try {
      const [spots, index, status] = await Promise.all([
        loadJson('data/spots.geojson'),
        loadJson('data/sources.json'),
        // 変更チェックの結果は無くても表示できるようにする
        loadJson('data/source-status.json').catch(() => ({ sources: {} })),
      ]);
      const sourceStatus = status.sources || {};
      const markers = new Map();
      for (const f of spots.features) {
        const [lng, lat] = f.geometry.coordinates;
        const marker = L.marker([lat, lng], {
          icon: markerIcon(f.properties.status),
          title: f.properties.name,
          alt: `${f.properties.name}（${(STATUS[f.properties.status] || STATUS.unconfirmed).label}）`,
        });
        marker.bindPopup(() => el('div', null, [
          el('strong', { text: f.properties.name }),
          ...spotDetails(f.properties, sourceStatus, today),
        ]), { maxWidth: 320, maxHeight: 360 });
        markers.set(f.properties.id, marker);
      }
      renderList(spots.features, markers, map, sourceStatus, today);
      renderIndex(index, sourceStatus, today);
    } catch (err) {
      const box = document.getElementById('load-error');
      box.textContent = 'データを読み込めませんでした。時間をおいてページを再読み込みしてください。';
      box.hidden = false;
      console.error(err);
    }
  }

  main();
})();

// 釣り規制マップ：公式に「釣りをしてよい」と公表された場所だけを表示する。
// データはすべて静的 JSON。表示は textContent で組み立て、HTML 文字列は使わない（XSS 対策）。
(function () {
  'use strict';

  const STATUS = {
    // 記号は小さいマーカーの中でも潰れずに読めるものを選ぶ（○ は円の中で判別しにくい）
    open: { label: '常設の釣り場', mark: 'OK' },
    limited: { label: '決まった日だけ開放', mark: '日' },
    unconfirmed: { label: '今年の開放を確認できず', mark: '？' },
    local: { label: '現地の案内のみ', mark: '現' },
  };
  const CHECKED_HOW = { page: 'ページ', search: '検索', notice: '現地の掲示・告知', inquiry: '管理者への問い合わせ' };
  // 地図に既定では出さない状態（OK の場所と並べると誤解されるもの）と、表示を切り替えるチェックボックス
  const HIDDEN_BY_DEFAULT = { unconfirmed: 'show-unconfirmed', local: 'show-local' };
  // 「現地の案内のみ」は自動で確かめられないので、確認からの日数で古さを知らせ、期限を過ぎたら出さない
  const LOCAL_STALE_DAYS = 365;
  const LOCAL_EXPIRE_DAYS = 730;
  const LOCAL_NOTICE = 'ウェブ上の公式発表はありません。町・漁協の現地の看板や告知、問い合わせに基づく情報です。' +
    '釣りをする前に、必ず現地の看板で範囲と条件を確認してください。';
  const JAPAN_VIEW = { center: [37.5, 137.5], zoom: 5 };
  const REPORT_URL = 'https://github.com/daliseiya019-wq/fishing-rule-map/issues/new';
  // 位置が「目安」の点は、寄りすぎると施設の場所だと誤解されるので、精度に応じて寄せ方と範囲の円を変える
  const PRECISION = {
    point: { zoom: 15, radius: 0 },
    district: { zoom: 12, radius: 1500 },
    port: { zoom: 11, radius: 3000 },
  };
  // 範囲の円は、ある程度寄ったときだけ出す（全国表示で円が重なって見づらくならないように）
  const CIRCLE_MIN_ZOOM = 10;
  const CLUSTER_OFF_ZOOM = Math.min(...Object.values(PRECISION).map((p) => p.zoom));

  const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  const statusKey = (status) => (hasOwn(STATUS, status) ? status : 'unconfirmed');

  function precisionOf(p) {
    const key = p.position && p.position.precision;
    return hasOwn(PRECISION, key) ? PRECISION[key] : PRECISION.port;
  }

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

  function reportLink(spotName) {
    const params = new URLSearchParams({ template: 'report.yml', title: `[誤り報告] ${spotName}` });
    return safeLink(`${REPORT_URL}?${params}`, 'この場所の誤りを報告');
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

  function daysSince(iso, today) {
    return Math.round((Date.parse(today) - Date.parse(iso)) / 86400000);
  }

  // 出典の確認日のうち最も新しいものからの日数
  function daysSinceChecked(p, today) {
    return Math.min(...p.sources.map((s) => daysSince(s.checked, today)));
  }

  function localAlerts(p, today) {
    if (statusKey(p.status) !== 'local') return [];
    const days = daysSinceChecked(p, today);
    return days > LOCAL_STALE_DAYS ? [`最後の確認から 1 年以上たっています（${days} 日）。看板の内容が変わっている可能性があります`] : [];
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
      // 現地の看板や問い合わせの出典には URL が無いことがある（safeLink は URL が無ければ文字だけにする）
      el('p', null, ['出典: ', safeLink(src.url, src.title), `（${src.publisher}）`]),
      el('p', {
        text: `掲載日: ${formatDate(src.published)} ／ 確認日: ${formatDate(src.checked)}` +
          `（確認方法: ${CHECKED_HOW[src.checked_how] || '不明'}）`,
      }),
      ...alerts.map((a) => el('p', { class: 'alert-line', text: `⚠ ${a}` })),
    ]);
  }

  function statusBadge(status) {
    const key = statusKey(status);
    return el('span', { class: `badge status-${key}`, text: `${STATUS[key].mark} ${STATUS[key].label}` });
  }

  // 一覧のカードと地図のポップアップで同じ中身を使う
  function spotDetails(p, sourceStatus, today) {
    return [
      el('p', { class: 'meta', text: p.port ? `${p.prefecture} ／ ${p.port}` : p.prefecture }),
      statusBadge(p.status),
      statusKey(p.status) === 'local' ? el('p', { class: 'local-note', text: LOCAL_NOTICE }) : null,
      ...[...expiryAlerts(p.valid_until, today), ...localAlerts(p, today)]
        .map((a) => el('p', { class: 'alert-line', text: `⚠ ${a}` })),
      el('p', { text: p.summary }),
      el('ul', null, p.conditions.map((c) => el('li', { text: c }))),
      el('dl', null, [
        el('dt', { text: '時間' }), el('dd', { text: p.hours }),
        el('dt', { text: '料金' }), el('dd', { text: p.fee }),
        el('dt', { text: '地図の位置' }), el('dd', { text: p.position.note }),
      ]),
      el('div', { class: 'sources' }, p.sources.map((s) => renderSource(s, sourceStatus, today))),
      el('p', { class: 'report' }, [reportLink(p.name)]),
    ];
  }

  // ---- 地図 ----
  function createMap() {
    const map = L.map('map', { scrollWheelZoom: false }).setView(JAPAN_VIEW.center, JAPAN_VIEW.zoom);
    L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/pale/{z}/{x}/{y}.png', {
      maxZoom: 18,
      attribution: '<a href="https://maps.gsi.go.jp/development/ichiran.html" target="_blank" rel="noopener noreferrer">国土地理院</a>',
    }).addTo(map);
    return map;
  }

  function markerIcon(status) {
    const key = statusKey(status);
    const icon = el('div', { class: `spot-marker status-${key}`, text: STATUS[key].mark });
    return L.divIcon({ html: icon.outerHTML, className: '', iconSize: [28, 28], iconAnchor: [14, 14], popupAnchor: [0, -14] });
  }

  // 場所ごとのマーカーと（位置が目安なら）範囲の点線の円を作る
  function createSpotLayers(features, sourceStatus, today) {
    const spots = new Map();
    for (const f of features) {
      const p = f.properties;
      const [lng, lat] = f.geometry.coordinates;
      const marker = L.marker([lat, lng], {
        icon: markerIcon(p.status),
        title: p.name,
        alt: `${p.name}（${STATUS[statusKey(p.status)].label}）`,
      });
      marker.bindPopup(() => el('div', null, [
        el('strong', { text: p.name }),
        ...spotDetails(p, sourceStatus, today),
      ]), { maxWidth: 320, maxHeight: 360 });
      const { radius } = precisionOf(p);
      const circle = radius
        ? L.circle([lat, lng], { radius, color: '#5a6460', weight: 1, dashArray: '4 4', fill: false, interactive: false })
        : null;
      spots.set(p.id, { feature: f, marker, circle });
    }
    return spots;
  }

  // ---- 一覧と地図の連動 ----
  function setupListAndMap(features, spots, map, prefs, sourceStatus, today) {
    const list = document.getElementById('spot-list');
    const prefSelect = document.getElementById('filter-pref');
    const statusSelect = document.getElementById('filter-status');
    const toggles = Object.fromEntries(
      Object.entries(HIDDEN_BY_DEFAULT).map(([key, id]) => [key, document.getElementById(id)]));
    const visibleOnMap = (p) => {
      const toggle = toggles[statusKey(p.status)];
      return !toggle || toggle.checked;
    };
    // 大量の点は近いものをまとめて数字で表示する（全国表示で点が重なって押せなくなるのを防ぐ）
    // 「地図で見る」で寄る倍率（11 以上）ではまとめず、1 つずつの印にする
    const cluster = L.markerClusterGroup({
      showCoverageOnHover: false, maxClusterRadius: 40, disableClusteringAtZoom: CLUSTER_OFF_ZOOM,
    });
    const circles = L.layerGroup();
    map.addLayer(cluster);

    // 釣り場のある都道府県だけを JIS コード順で選択肢に出す
    const prefsWithSpots = prefs.filter((pref) => features.some((f) => f.properties.prefecture === pref.name));
    for (const pref of prefsWithSpots) prefSelect.append(el('option', { value: pref.name, text: pref.name }));

    // ?pref=<slug> で開いたときはその都道府県を選んだ状態にする（都道府県ページからのリンク用）
    const requested = new URLSearchParams(location.search).get('pref');
    const requestedPref = prefsWithSpots.find((p) => p.slug === requested);
    if (requestedPref) prefSelect.value = requestedPref.name;

    function updateCircles() {
      if (map.getZoom() >= CIRCLE_MIN_ZOOM) map.addLayer(circles); else map.removeLayer(circles);
    }
    map.on('zoomend', updateCircles);

    function fitTo(shown, animate) {
      // 読み込み直後はレイアウトが固まっておらず地図の大きさを誤ることがあるので、測り直してから合わせる
      map.invalidateSize();
      const prefName = prefSelect.value;
      const pref = prefs.find((p) => p.name === prefName);
      const points = shown.map((f) => spots.get(f.properties.id).marker.getLatLng());
      if (points.length) {
        map.fitBounds(L.latLngBounds(points), { padding: [30, 30], maxZoom: pref ? 10 : 7, animate });
      } else if (pref) {
        map.setView(pref.center, pref.zoom, { animate });
      } else {
        map.setView(JAPAN_VIEW.center, JAPAN_VIEW.zoom, { animate });
      }
    }

    function draw({ moveMap, animate = true }) {
      const pref = prefSelect.value;
      const status = statusSelect.value;
      const shown = features.filter((f) =>
        (!pref || f.properties.prefecture === pref) && (!status || f.properties.status === status));

      // 「今年の開放を確認できず」「現地の案内のみ」は OK の場所と並べると誤解されるので、地図には選んだときだけ出す
      const onMap = shown.filter((f) => visibleOnMap(f.properties));
      cluster.clearLayers();
      circles.clearLayers();
      cluster.addLayers(onMap.map((f) => spots.get(f.properties.id).marker));
      for (const f of onMap) {
        const { circle } = spots.get(f.properties.id);
        if (circle) circles.addLayer(circle);
      }
      updateCircles();
      if (moveMap) fitTo(onMap, animate);

      list.replaceChildren();
      if (shown.length === 0) {
        list.append(el('li', { class: 'card', text: 'この条件に合う場所はまだ収録していません。' }));
        return;
      }
      for (const f of shown) {
        const p = f.properties;
        const button = el('button', { type: 'button', class: 'show-on-map', text: '地図で見る' });
        button.addEventListener('click', () => {
          if (!visibleOnMap(p)) {
            toggles[statusKey(p.status)].checked = true;
            draw({ moveMap: false });
          }
          const { marker } = spots.get(p.id);
          // この倍率ではまとめ表示が解除されるので、アニメーションなしで移動してすぐ開ける
          map.setView(marker.getLatLng(), precisionOf(p).zoom, { animate: false });
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

    prefSelect.addEventListener('change', () => draw({ moveMap: true }));
    statusSelect.addEventListener('change', () => draw({ moveMap: false }));
    for (const toggle of Object.values(toggles)) toggle.addEventListener('change', () => draw({ moveMap: false }));
    // 最初の表示はアニメーションなしで合わせる（裏のタブで開かれるとアニメーションが止まり、範囲が合わないため）
    draw({ moveMap: true, animate: false });
  }

  // ---- 都道府県ごとの公式ルール索引 ----
  function renderIndex(index, prefs, sourceStatus, today) {
    const root = document.getElementById('rule-index');
    root.replaceChildren(); // 事前に書き出した HTML を、警告付きの最新表示に置き換える
    const order = new Map(prefs.map((p, i) => [p.name, i]));
    const sorted = [...index.prefectures].sort((a, b) => (order.get(a.name) ?? 99) - (order.get(b.name) ?? 99));
    for (const pref of sorted) {
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
      const [spotsData, index, prefData, status] = await Promise.all([
        loadJson('data/spots.geojson'),
        loadJson('data/sources.json'),
        loadJson('data/prefectures.json'),
        // 変更チェックの結果は無くても表示できるようにする
        loadJson('data/source-status.json').catch(() => ({ sources: {} })),
      ]);
      const sourceStatus = status.sources || {};
      const prefs = prefData.prefectures;
      // 確認から期限を過ぎた「現地の案内のみ」は、古い情報で人を誘導しないよう表示しない
      const features = spotsData.features.filter((f) =>
        statusKey(f.properties.status) !== 'local' || daysSinceChecked(f.properties, today) <= LOCAL_EXPIRE_DAYS);
      const spots = createSpotLayers(features, sourceStatus, today);
      document.getElementById('static-note').hidden = true;
      setupListAndMap(features, spots, map, prefs, sourceStatus, today);
      renderIndex(index, prefs, sourceStatus, today);
    } catch (err) {
      const box = document.getElementById('load-error');
      box.textContent = 'データを読み込めませんでした。時間をおいてページを再読み込みしてください。';
      box.hidden = false;
      console.error(err);
    }
  }

  main();
})();

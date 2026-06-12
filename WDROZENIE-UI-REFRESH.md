# WP Downloader — wdrożenie odświeżenia UI (etapy 1–3)

Wszystkie zmiany dotyczą **`static/index.html`** + jeden nowy plik **`static/anime.umd.min.js`**.
Zero zmian w Pythonie. Kolejność sekcji = bezpieczna kolejność wdrażania.

---

## 0. Nowy plik: anime.js v4 (lokalnie, bez CDN)

```bash
curl -L "https://cdn.jsdelivr.net/npm/animejs@4/dist/bundles/anime.umd.min.js" \
     -o static/anime.umd.min.js
```

W `<head>` pliku `static/index.html`:

```html
<!-- USUŃ --> <script src="/static/anime.min.js"></script>
<!-- DODAJ --> <script src="/static/anime.umd.min.js"></script>
```

Stary `static/anime.min.js` (v3) można skasować z repo.

**Zmiany API v3 → v4 (obowiązują we wszystkich fragmentach poniżej):**
- `anime({ targets: el, ... })` → `anime.animate(el, { ... })`
- `easing: 'easeOutQuart'` → `ease: 'outQuart'`
- gardy: `typeof anime !== 'undefined'` → `window.anime?.animate`

Jedyne istniejące wywołanie v3 (animacja szerokości paska w `renderTasks()`):

```js
// BYŁO: anime({ targets: bar, width: to + '%', duration: 450, easing: 'easeOutQuart' });
anime.animate(bar, { width: to + '%', duration: 450, ease: 'outQuart' });
```

---

## 1. CSS — rzeczy do USUNIĘCIA

1. Blok fade-in body (zastępuje go splash + reveal):
   ```css
   body { opacity: 0; transition: opacity 450ms ease-out; }
   body.ready { opacity: 1; }
   ```
2. Wszystkie style `.toast` (łącznie z overridem `[data-theme="dark"] .toast`).
3. W `.tab-btn.active` usuń `border-bottom-color: var(--red);`
   (rolę przejmuje przesuwny `.tab-indicator`).

## 2. HTML — rzeczy do USUNIĘCIA / PODMIANY

1. `<div class="toast" id="toast"></div>` → `<div class="toast-stack" id="toast-stack" aria-live="polite"></div>`
2. W `renderTasks()` status na kontener zadania:
   `class="task"` → `class="task ${t.status}"`

---

## 3. Splash + orkiestrowany reveal

### HTML — zaraz po `<body>`:

```html
<div id="splash">
  <div class="splash-inner">
    <div class="splash-logo-wrap">
      <div class="splash-glow"></div>
      <img src="/static/wp_logo.png" alt="WP" class="splash-logo-img">
      <div class="splash-sheen"></div>
    </div>
    <div class="splash-bar"><div class="splash-bar-inner"></div></div>
    <div class="splash-status">Uruchamianie serwera…</div>
  </div>
</div>
```

### CSS:

```css
/* Ukrycie UI do czasu reveal (zamiast body{opacity:0}) */
body:not(.revealed) .header,
body:not(.revealed) .main { opacity: 0; }

#splash { position: fixed; inset: 0; z-index: 99999; background: var(--bg);
  display: flex; align-items: center; justify-content: center; }
.splash-inner { text-align: center; }
.splash-logo-wrap { position: relative; width: 110px; height: 110px; margin: 0 auto;
  display: flex; align-items: center; justify-content: center; }
.splash-glow { position: absolute; inset: -45%;
  background: radial-gradient(circle, rgba(227,0,15,.30), transparent 62%);
  filter: blur(12px); animation: glow-pulse 2.4s ease-in-out infinite; }
[data-theme="dark"] .splash-glow {
  background: radial-gradient(circle, rgba(255,34,51,.42), transparent 62%); }
@keyframes glow-pulse { 0%,100% { opacity:.5; transform:scale(.9); }
  50% { opacity:1; transform:scale(1.08); } }
.splash-logo-img { width: 88px; position: relative; z-index: 1;
  animation: logo-breathe 2.4s ease-in-out infinite; }
@keyframes logo-breathe { 0%,100% { transform:scale(1); } 50% { transform:scale(1.035); } }
/* Refleks maskowany kształtem logo — błysk nie wychodzi poza znak.
   Jeśli PNG ma duże przezroczyste marginesy, dostosuj inset. */
.splash-sheen { position: absolute; inset: 11px; z-index: 2; pointer-events: none; overflow: hidden;
  -webkit-mask: url('/static/wp_logo.png') center / contain no-repeat;
          mask: url('/static/wp_logo.png') center / contain no-repeat; }
.splash-sheen::after { content: ''; position: absolute; top: -20%; left: -60%;
  width: 45%; height: 140%;
  background: linear-gradient(105deg, transparent, rgba(255,255,255,.6), transparent);
  transform: skewX(-18deg); animation: sheen-sweep 2.6s ease-in-out infinite; }
@keyframes sheen-sweep { 0%,55% { left:-60%; } 100% { left:130%; } }
.splash-bar { width: 180px; height: 3px; margin: 24px auto 10px;
  background: var(--surf-track); border-radius: 99px; overflow: hidden; }
.splash-bar-inner { height: 100%; width: 40%; background: var(--red); border-radius: 99px;
  animation: splash-slide 1.2s cubic-bezier(.4,0,.6,1) infinite; }
@keyframes splash-slide { 0% { margin-left:-40%; } 100% { margin-left:100%; } }
.splash-status { font-size: 11px; color: var(--text-3); font-weight: 500; letter-spacing: .02em; }
```

### JS — na końcu skryptu:

```js
(function initSplashReveal() {
  function playReveal() {
    const splash = document.getElementById('splash');
    const cards  = document.querySelectorAll('.tab-content.active > .card');
    const header = document.querySelector('.header');
    const nav    = document.querySelector('.tabs-nav');
    [header, nav, ...cards].forEach(el => el && (el.style.opacity = 0));
    document.body.classList.add('revealed');

    if (!window.anime?.createTimeline) {           // fallback bez biblioteki
      splash?.remove();
      document.querySelectorAll('.header, .tabs-nav, .card')
        .forEach(el => el.style.opacity = 1);
      positionTabIndicator();
      return;
    }
    anime.createTimeline({ defaults: { ease: 'outQuart' } })
      .add('#splash .splash-inner', { opacity: [1,0], scale: [1,.94], duration: 360, ease: 'inQuart' })
      .add('#splash', { opacity: [1,0], duration: 300, onComplete: () => splash.remove() }, '-=120')
      .add(header,  { opacity: [0,1], translateY: [-14,0], duration: 420 }, '-=160')
      .add(nav,     { opacity: [0,1], translateY: [-8,0],  duration: 320,
                      onComplete: positionTabIndicator }, '-=260')
      .add(cards,   { opacity: [0,1], translateY: [16,0],  duration: 440,
                      delay: anime.stagger(80) }, '-=200');
  }
  // AppController dodaje body.ready — zero zmian w Pythonie
  if (document.body.classList.contains('ready')) { playReveal(); return; }
  const obs = new MutationObserver(() => {
    if (!document.body.classList.contains('ready')) return;
    obs.disconnect(); playReveal();
  });
  obs.observe(document.body, { attributes: true, attributeFilter: ['class'] });
})();
```

---

## 4. Zakładki — przesuwny wskaźnik + stagger zawartości

### HTML — w `<nav class="tabs-nav">` na końcu:

```html
<span class="tab-indicator" id="tab-indicator"></span>
```

### CSS:

```css
.tabs-nav { position: relative; }
.tab-indicator { position: absolute; bottom: -1px; height: 2px;
  background: var(--red); border-radius: 2px;
  transition: left .28s var(--ease-spring), width .28s var(--ease-spring); }
```

### JS — zastąp `switchTab()`:

```js
function positionTabIndicator() {
  const ind = document.getElementById('tab-indicator');
  const act = document.querySelector('.tabs-nav .tab-btn.active');
  if (!ind || !act) return;
  ind.style.left  = act.offsetLeft  + 'px';
  ind.style.width = act.offsetWidth + 'px';
}
window.addEventListener('resize', positionTabIndicator);

function switchTab(id) {
  const target = document.getElementById(id);
  if (target.classList.contains('active')) return;
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  event.currentTarget.classList.add('active');
  positionTabIndicator();
  document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
  target.classList.add('active');
  if (window.anime?.animate) {
    anime.animate(target.querySelectorAll(':scope > .card'), {
      opacity: [0, 1], translateY: [10, 0],
      duration: 320, delay: anime.stagger(55), ease: 'outQuart'
    });
  }
  if (id === 'settings') loadAboutInfo();
  if (id === 'recording') _initRecordingTab();
}
```

---

## 5. Karty zadań — beam, glow, lift, akcje na hover, ticker, live dot

### CSS:

```css
/* Lift + glow */
.task { position: relative;
  transition: transform .18s var(--ease-spring), box-shadow .18s ease, border-color .18s ease; }
.task:hover { transform: translateY(-1px); border-color: #D0D0CC;
  box-shadow: 0 4px 14px rgba(0,0,0,.06); }
[data-theme="dark"] .task:hover { border-color: #3A3A3A;
  box-shadow: 0 4px 18px rgba(0,0,0,.35); }
[data-theme="dark"] .task.downloading {
  box-shadow: 0 0 0 1px rgba(255,34,51,.10), 0 0 26px -8px rgba(255,34,51,.30); }

/* Border beam */
@property --beam-angle { syntax: '<angle>'; initial-value: 0deg; inherits: false; }
.task.downloading::before { content: ''; position: absolute; inset: -1px;
  border-radius: 11px; padding: 1.5px;
  background: conic-gradient(from var(--beam-angle),
    transparent 0% 72%, var(--red) 88%, transparent 100%);
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor; mask-composite: exclude;
  animation: beam-rotate 3.2s linear infinite; pointer-events: none; }
@keyframes beam-rotate { to { --beam-angle: 360deg; } }

/* Akcje na hover (focus-within = dostępność z klawiatury) */
.task-actions { opacity: 0; transform: translateX(4px);
  transition: opacity .18s ease, transform .18s var(--ease-spring); }
.task:hover .task-actions,
.task:focus-within .task-actions { opacity: 1; transform: none; }

/* Mocniejszy puls kropki live */
.timer-dot::after { content: ''; position: absolute; inset: -4px; border-radius: 50%;
  border: 2px solid currentColor;
  animation: ping 1.4s cubic-bezier(0,0,.2,1) infinite; }
@keyframes ping { 0% { transform:scale(.55); opacity:.8; }
  100% { transform:scale(1.6); opacity:0; } }
```

(`.timer-dot` musi mieć `position: relative;` — dopisz, jeśli nie ma.)

### JS — w `renderTasks()`:

```js
// Na poziomie modułu:
const seenTaskIds = new Set();
const counters = {};

// Płynny licznik %, prędkości itd.
function animNum(el, to, suffix = '%') {
  if (!el) return;
  const key = el.id || el.dataset.cid;
  const from = counters[key] || 0;
  counters[key] = to;
  if (window.anime?.animate && Math.abs(to - from) >= 0.2) {
    const obj = { v: from };
    anime.animate(obj, { v: to, duration: 420, ease: 'outQuart',
      onUpdate: () => { el.textContent = obj.v.toFixed(1) + suffix; } });
  } else el.textContent = to.toFixed(1) + suffix;
}

// Na KOŃCU renderTasks() (po pętli z paskami):
const freshEls = [...list.querySelectorAll('.task[data-id]')]
  .filter(el => !seenTaskIds.has(el.dataset.id));
freshEls.forEach(el => seenTaskIds.add(el.dataset.id));
if (freshEls.length && window.anime?.animate) {
  anime.animate(freshEls, { opacity: [0,1], translateY: [12,0], scale: [.98,1],
    duration: 420, delay: anime.stagger(70), ease: 'outQuart' });
}
```

---

## 6. Toast 2.0

### CSS:

```css
.toast-stack { position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  z-index: 10000; pointer-events: none; }
.toast2 { position: relative; overflow: hidden; pointer-events: auto; cursor: pointer;
  display: flex; align-items: center; gap: 10px;
  max-width: min(480px, 90vw); padding: 11px 16px 13px;
  background: #1E1E1E; color: #F0F0F0; border: 1px solid #333;
  border-radius: 10px; font-size: 13px; font-weight: 500; letter-spacing: -.01em;
  box-shadow: 0 8px 30px rgba(0,0,0,.35), 0 2px 8px rgba(0,0,0,.2); }
.toast2.error { background: var(--red); border-color: transparent; color: #fff; }
.toast2 svg { flex-shrink: 0; }
.toast2 .t-ico-ok { color: #4ade80; }
.toast2 .t-ico-info { color: #93c5fd; }
.toast2 .t-life { position: absolute; left: 0; bottom: 0; height: 2px;
  background: rgba(255,255,255,.3); width: 100%; }
```

### JS — zastąp `showToast()` (zgodność wsteczna: `showToast(msg, true)` działa):

```js
const TOAST_ICONS = {
  success: '<svg class="t-ico-ok" width="15" height="15" viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="7.5" r="6.2"/><path d="M4.8 7.7l1.8 1.8 3.6-3.8"/></svg>',
  error:   '<svg width="15" height="15" viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="7.5" cy="7.5" r="6.2"/><path d="M5.4 5.4l4.2 4.2M9.6 5.4l-4.2 4.2"/></svg>',
  info:    '<svg class="t-ico-info" width="15" height="15" viewBox="0 0 15 15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="7.5" cy="7.5" r="6.2"/><path d="M7.5 7v3.4M7.5 4.6v.1"/></svg>'
};

function showToast(msg, opts = false) {
  const o    = typeof opts === 'boolean' ? { type: opts ? 'error' : 'info' } : (opts || {});
  const type = o.type || 'info';
  const dur  = o.duration ?? (type === 'error' ? 4200 : 2600);
  const stack = document.getElementById('toast-stack');
  while (stack.children.length >= 3) stack.firstElementChild._dismiss?.();

  const el = document.createElement('div');
  el.className = 'toast2' + (type === 'error' ? ' error' : '');
  el.innerHTML = TOAST_ICONS[type] + '<span></span><div class="t-life"></div>';
  el.querySelector('span').textContent = msg;
  stack.appendChild(el);

  let lifeAnim = null, dismissed = false;
  el._dismiss = () => {
    if (dismissed) return; dismissed = true;
    lifeAnim?.cancel?.();
    if (window.anime?.animate) {
      anime.animate(el, { opacity: 0, translateY: 8, scale: .97, duration: 220,
        ease: 'inQuart', onComplete: () => el.remove() });
    } else el.remove();
  };
  el.addEventListener('click', el._dismiss);

  if (window.anime?.animate) {
    anime.animate(el, { opacity: [0,1], translateY: [16,0], scale: [.96,1],
      duration: 450, ease: 'outBack(1.4)' });
    lifeAnim = anime.animate(el.querySelector('.t-life'),
      { width: ['100%','0%'], duration: dur, ease: 'linear', onComplete: el._dismiss });
    el.addEventListener('mouseenter', () => lifeAnim.pause());
    el.addEventListener('mouseleave', () => lifeAnim.play());
  } else setTimeout(el._dismiss, dur);
}
```

---

## 7. Akordeony w Ustawieniach

### HTML — każda karta sekcji w `#settings` (Wygląd / Działanie / Diagnostyka / O aplikacji):

```html
<div class="card acc open" id="acc-look">  <!-- open = domyślnie rozwinięta -->
  <button type="button" class="acc-head" onclick="toggleAcc(this)">
    <span class="section-label">Wygląd</span>
    <svg class="acc-chev" width="12" height="12" viewBox="0 0 12 12" fill="none"
         stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
      <path d="M2.5 4.5L6 8l3.5-3.5"/>
    </svg>
  </button>
  <div class="acc-body"><div class="acc-inner">
    ...dotychczasowa zawartość karty...
  </div></div>
</div>
```

Identyfikatory: `acc-look`, `acc-behavior`, `acc-diag`, `acc-about`.

### CSS:

```css
.acc-head { width: 100%; display: flex; align-items: center; justify-content: space-between;
  background: none; border: none; cursor: pointer; padding: 0; font-family: inherit; }
.acc-head .section-label { margin-bottom: 0; }
.acc-head:hover .section-label, .acc-head:hover .acc-chev { color: var(--text-2); }
.acc-chev { color: var(--text-3);
  transition: transform .26s var(--ease-spring), color .15s; flex-shrink: 0; }
.acc.open .acc-chev { transform: rotate(180deg); }
.acc-body { height: 0; overflow: hidden; }
.acc-inner { padding-top: 14px; }
```

### JS:

```js
function toggleAcc(head) {
  const card  = head.closest('.acc');
  const body  = card.querySelector('.acc-body');
  const inner = body.firstElementChild;
  const opening = !card.classList.contains('open');
  card.classList.toggle('open', opening);
  try { localStorage.setItem('wp-acc-' + card.id, opening ? '1' : '0'); } catch (e) {}

  if (!window.anime?.animate) { body.style.height = opening ? 'auto' : '0px'; return; }
  const h = inner.offsetHeight;
  if (opening) {
    anime.animate(body, { height: [0, h], duration: 380, ease: 'outQuart',
      onComplete: () => { body.style.height = 'auto'; } });
    anime.animate(inner, { opacity: [0,1], translateY: [-6,0], duration: 300, delay: 70, ease: 'outQuart' });
  } else {
    body.style.height = h + 'px';
    anime.animate(body, { height: 0, duration: 320, ease: 'outQuart' });
  }
}

function initAccordions() {
  document.querySelectorAll('#settings .acc').forEach(card => {
    let saved = null;
    try { saved = localStorage.getItem('wp-acc-' + card.id); } catch (e) {}
    const open = saved === null ? card.classList.contains('open') : saved === '1';
    card.classList.toggle('open', open);
    card.querySelector('.acc-body').style.height = open ? 'auto' : '0px';
  });
}
// Przy starcie: initAccordions(); positionTabIndicator();
```

---

## 8. Shimmer na przycisku głównym

```css
.btn-primary { position: relative; overflow: hidden; }
.btn-primary::after { content: ''; position: absolute; top: 0; left: -150%;
  width: 60%; height: 100%;
  background: linear-gradient(100deg, transparent, rgba(255,255,255,.35), transparent);
  transform: skewX(-20deg); animation: shimmer 3.8s ease-in-out infinite;
  pointer-events: none; }
@keyframes shimmer { 0%,60% { left: -150%; } 100% { left: 150%; } }
```

---

## 9. Test lokalny → commit → release z buildami

```bash
# 1. Test lokalny (sprawdź: splash, zakładki, akordeony, toasty, pobieranie, dark mode)
python main.py

# 2. Commit
git checkout -b feature/ui-refresh
git add static/index.html static/anime.umd.min.js
git rm --cached static/anime.min.js && rm static/anime.min.js
git commit -m "UI refresh: anime.js v4, splash z logo, akordeony, toasty 2.0, animacje zakładek i kart"
git push -u origin feature/ui-refresh
# → merge do main (PR lub bezpośrednio)

# 3. Release — to wyzwala builds Windows + macOS w istniejącym workflow
git checkout main && git pull
git tag v1.1
git push origin v1.1
```

Po ~20–40 min w zakładce **Actions** zobaczysz dwa joby (`build-macos`, `build-windows`),
a artefakty (`WP_Downloader_macOS.dmg`, `WP_Downloader_Windows.zip` + instalator Inno)
wylądują przy release `v1.1`. Alternatywnie: Actions → **Build & Release** → **Run workflow**
(`workflow_dispatch`) bez tagowania.

### Checklist przed tagiem

- [ ] `static/anime.umd.min.js` jest w repo (workflow buduje offline z `--add-data=static`)
- [ ] `wp_downloader.spec` jest śledzony przez gita — **uwaga:** `.gitignore` zawiera `*.spec`,
      a job macOS go wymaga (`pyinstaller wp_downloader.spec`); jeśli nie jest w repo:
      `git add -f wp_downloader.spec`
- [ ] usunięte stare `body{opacity:0}` i style `.toast`
- [ ] `static/wp_logo.png` bez zmian (używany przez splash, ICO na Windows i .icns na macOS)

'use strict';
(function(){

// ── TOAST ─────────────────────────────────────────────────────────────────
const toast = Object.assign(document.createElement('div'), {className:'toast'})
document.body.appendChild(toast)
let toastTimer
function showToast(msg, icon) {
    clearTimeout(toastTimer)
    toast.innerHTML = `<span>${icon||'✓'}</span><span>${msg}</span>`
    toast.classList.add('show')
    toastTimer = setTimeout(() => toast.classList.remove('show'), 2200)
}

// ── COPY ──────────────────────────────────────────────────────────────────
document.addEventListener('click', e => {
    const el = e.target.closest('[data-copy]')
    if (!el) return
    const txt = el.dataset.copy || el.textContent.trim();
    (navigator.clipboard?.writeText(txt) || Promise.reject())
        .catch(() => {
            const ta = Object.assign(document.createElement('textarea'),
                {value: txt, style: 'position:fixed;opacity:0'})
            document.body.appendChild(ta); ta.select()
            document.execCommand('copy'); ta.remove()
        })
        .then(() => showToast('Скопировано'))
        .catch(() => showToast('Ошибка копирования', '✕'))
})

// ── FLASH AUTO-DISMISS ────────────────────────────────────────────────────
document.querySelectorAll('.flash').forEach((f, i) =>
    setTimeout(() => {
        f.style.transition = 'opacity .4s, transform .4s'
        f.style.opacity = '0'; f.style.transform = 'translateX(8px)'
        setTimeout(() => f.remove(), 400)
    }, 4500 + i * 300)
)

// ── PASSWORD STRENGTH ─────────────────────────────────────────────────────
document.querySelectorAll('input[type="password"]').forEach(inp => {
    if (inp.name !== 'password' && inp.id !== 'password') return
    const bar = inp.closest('.form-group')?.querySelector('.pwd-meter-bar')
    if (!bar) return
    inp.addEventListener('input', () => {
        const v = inp.value; let s = 0
        if (v.length >= 6)  s += 20
        if (v.length >= 10) s += 20
        if (/[A-Z]/.test(v)) s += 20
        if (/[0-9]/.test(v)) s += 20
        if (/[^A-Za-z0-9]/.test(v)) s += 20
        bar.style.width = s + '%'
        bar.style.background = s<=20?'#ef4444':s<=60?'#f59e0b':s<=80?'#3b82f6':'#10b981'
    })
})

// ── TARIFF SELECTION ──────────────────────────────────────────────────────
const tariffCards = document.querySelectorAll('.tariff-card[data-tariff-id]')
const subBtn  = document.getElementById('subscribe-btn')
const subForm = document.getElementById('subscribe-form')
tariffCards.forEach(card => {
    card.addEventListener('click', () => {
        tariffCards.forEach(c => c.classList.remove('selected'))
        card.classList.add('selected')
        const tid = card.dataset.tariffId
        if (subBtn) { subBtn.disabled = false; subBtn.dataset.tariffId = tid }
    })
})
if (subBtn) subBtn.addEventListener('click', () => {
    const tid = subBtn.dataset.tariffId
    if (!tid || !subForm) return
    subForm.action = `/dashboard/subscribe/${tid}`
    subBtn.classList.add('btn-loading')
    subBtn.textContent = 'Подключение...'
    subForm.submit()
})

// ── CONFIRM DIALOGS ───────────────────────────────────────────────────────
document.addEventListener('click', e => {
    const el = e.target.closest('[data-confirm]')
    if (!el) return
    if (!confirm(el.dataset.confirm)) e.preventDefault()
})

// ── PROGRESS BARS ─────────────────────────────────────────────────────────
setTimeout(() =>
    document.querySelectorAll('.progress-bar[data-width]').forEach(b =>
        b.style.width = b.dataset.width + '%'), 100)

// ── FORM LOADING STATE ────────────────────────────────────────────────────
document.querySelectorAll('form').forEach(form =>
    form.addEventListener('submit', () => {
        const btn = form.querySelector('[type="submit"]')
        if (btn && !btn.classList.contains('btn-danger') && !btn.dataset.noload) {
            btn.classList.add('btn-loading'); btn.textContent = '...'
        }
    })
)

// ── TOKEN CHARS ───────────────────────────────────────────────────────────
document.querySelectorAll('.token-chars[data-token]').forEach(el =>
    el.innerHTML = [...el.dataset.token]
        .map(c => `<div class="token-char">${c}</div>`).join('')
)

// ── USER CABINET SIDEBAR ──────────────────────────────────────────────────
// Only runs on user cabinet pages (not admin, not landing)
const sidebar = document.getElementById('sidebar')
if (sidebar && !document.querySelector('.admin-body')) {
    const SB_KEY  = 'sb_collapsed'
    const toggle  = document.getElementById('sb-toggle')
    const burger  = document.getElementById('topbar-burger')
    const overlay = document.getElementById('sb-overlay')

    sidebar.querySelectorAll('.sb-link').forEach(link => {
        const lbl = link.querySelector('.sb-label')
        if (lbl && !link.dataset.tooltip) link.dataset.tooltip = lbl.textContent.trim()
    })

    function setCollapsed(on, save) {
        sidebar.classList.toggle('collapsed', on)
        document.body.classList.toggle('sb-collapsed', on)
        if (toggle) toggle.title = on ? 'Развернуть' : 'Свернуть'
        if (save) localStorage.setItem(SB_KEY, on ? '1' : '0')
    }
    if (localStorage.getItem(SB_KEY) === '1') setCollapsed(true, false)
    if (toggle) toggle.addEventListener('click', () =>
        setCollapsed(!sidebar.classList.contains('collapsed'), true))

    function openMobile()  {
        sidebar.classList.add('mobile-open')
        overlay?.classList.add('visible')
        burger?.classList.add('open')
        document.body.style.overflow = 'hidden'
    }
    function closeMobile() {
        sidebar.classList.remove('mobile-open')
        overlay?.classList.remove('visible')
        burger?.classList.remove('open')
        document.body.style.overflow = ''
    }
    burger?.addEventListener('click', () =>
        sidebar.classList.contains('mobile-open') ? closeMobile() : openMobile())
    overlay?.addEventListener('click', closeMobile)
    document.addEventListener('keydown', e => e.key === 'Escape' && closeMobile())
    sidebar.querySelectorAll('.sb-link').forEach(a =>
        a.addEventListener('click', () => { if (window.innerWidth < 992) closeMobile() }))
}

})();

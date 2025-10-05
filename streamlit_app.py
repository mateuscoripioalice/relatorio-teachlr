# streamlit_app.py
import asyncio, os, re, unicodedata, json
from pathlib import Path
from typing import List, Dict, Optional
from datetime import datetime

import requests
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Request

# ----------------- CONFIG -----------------
st.set_page_config(page_title="Teachlr | Relatórios em Lote", page_icon="📄", layout="wide")
DOWNLOAD_DIR = Path("./downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)
STATE_PATH = Path("/tmp/state_teachlr.json")

TEACHLR_DOMAIN   = st.secrets.get("TEACHLR_DOMAIN",   os.getenv("TEACHLR_DOMAIN", "alice"))
TEACHLR_API_KEY  = st.secrets.get("TEACHLR_API_KEY",  os.getenv("TEACHLR_API_KEY", ""))
TEACHLR_EMAIL    = st.secrets.get("TEACHLR_EMAIL",    os.getenv("TEACHLR_EMAIL", ""))
TEACHLR_PASSWORD = st.secrets.get("TEACHLR_PASSWORD", os.getenv("TEACHLR_PASSWORD", ""))

BASE_HOST = f"https://{TEACHLR_DOMAIN}.teachlr.com"

# ----------------- UTILS -----------------
def slugify(title: str) -> str:
    title = re.sub(r"[{}]", "", title)
    nfkd = unicodedata.normalize("NFKD", title)
    s = "".join(c for c in nfkd if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")

def students_url_from_title(title: str) -> str:
    return f"{BASE_HOST}/#dashboard/instructor/{slugify(title)}/students"

# ----------------- API: cursos (paginado) -----------------
def fetch_courses_all(domain: str, api_key: str, max_pages: int = 200) -> List[Dict]:
    base = f"https://api.teachlr.com/{domain}/api/courses"
    headers = {"Content-Type": "application/json","Authorization": api_key.strip()}
    all_courses, seen = [], set()
    for page in range(1, max_pages + 1):
        r = requests.get(base, headers=headers, params={"page":page,"per_page":100}, timeout=30)
        if r.status_code != 200 and page == 1:
            r = requests.get(base, headers=headers, timeout=30)
        if r.status_code != 200: break
        data = r.json()
        if not isinstance(data, list): data = data.get("data", []) or []
        if not data: break
        new = 0
        for c in data:
            i = c.get("id")
            if i not in seen:
                seen.add(i); all_courses.append(c); new += 1
        if new == 0: break
    all_courses.sort(key=lambda x: (x.get("title") or "").lower())
    return all_courses

# ----------------- Playwright helpers -----------------
def should_block(req: Request) -> bool:
    return req.url.lower().endswith((
        ".png",".jpg",".jpeg",".gif",".webp",".svg",".mp4",".webm",".woff",".woff2",".ttf",".otf",".eot"
    ))

def ensure_playwright_installed(log):
    try:
        from playwright.___impl._driver import compute_driver_executable  # type: ignore
        compute_driver_executable()
    except Exception:
        import subprocess
        log.write("🧩 Instalando Chromium do Playwright…")
        subprocess.run(["python","-m","playwright","install","--with-deps","chromium"],
                       check=False, capture_output=True)
        log.write("✅ Chromium pronto.")

def modal(page): return page.locator("div.dialog__content")
def action_cells(page): return modal(page).locator("tbody tr td:nth-child(3) .btn-lineal")

async def action_text_of(el):
    try: return (await el.locator("span").last.inner_text()).strip()
    except: return (await el.inner_text()).strip()

RE_DOWNLOAD = re.compile(r"\bBaixar\b", re.I)
RE_PROCESS  = re.compile(r"Processa(mento|ndo)", re.I)

async def open_reports_modal(page, log):
    log.write("📄 Abrindo modal “Desempenho dos estudantes”…")
    for sel in ['button:has-text("Desempenho dos estudantes")','text=/Desempenho dos estudantes/i']:
        try:
            await page.locator(sel).first.click(timeout=9000); return
        except: pass
    await page.screenshot(path=str(DOWNLOAD_DIR/"debug_students.png"), full_page=True)
    (DOWNLOAD_DIR/"debug_students.html").write_text(await page.content(), encoding="utf-8")
    raise RuntimeError('Não achei "Desempenho dos estudantes". Veja downloads/debug_students.*')

async def go_to_signin(page):
    # força a página de login
    if "/#signin" not in page.url:
        await page.goto(f"{BASE_HOST}/#signin", wait_until="domcontentloaded")
        try: await page.get_by_role("button", name=re.compile("Entrar", re.I)).click(timeout=1500)
        except: pass
    # garante que DOM principal carregou
    try: await page.wait_for_load_state("networkidle", timeout=6000)
    except: pass

async def login_if_needed(page, email, password, log):
    # se já tiver em alguma tela interna, sai cedo
    if "/#signin" not in page.url:
        try:
            await page.get_by_text(re.compile(r"(Estudantes|Conteúdo|Anúncios|Classificações)", re.I)).first.wait_for(timeout=2500)
            log.write("🔐 Sessão ativa."); return
        except PWTimeout: pass

    # vai para o login garantidamente
    await go_to_signin(page)

    log.write("🔐 Procurando campos de login…")
    # seletores bem tolerantes (com e sem acento/ hífen)
    mail_rx = re.compile(r"(usu[aá]rio.*mail|e-?mail|email)", re.I)
    pass_rx = re.compile(r"(senha|password)", re.I)

    # tente várias formas (placeholder, name, type)
    candidates = []
    candidates.append(page.get_by_placeholder(mail_rx))
    candidates.append(page.locator('input[name="email"]'))
    candidates.append(page.locator('input[type="email"]'))
    candidates.append(page.locator('input[placeholder*="mail" i]'))
    candidates.append(page.locator('input[placeholder*="Usu" i]'))

    email_loc = None
    for loc in candidates:
        try:
            await loc.first.wait_for(timeout=1500)
            email_loc = loc.first; break
        except: pass

    pass_loc = None
    for loc in [page.get_by_placeholder(pass_rx),
                page.locator('input[type="password"]'),
                page.locator('input[placeholder*="senha" i]')]:
        try:
            await loc.first.wait_for(timeout=1500)
            pass_loc = loc.first; break
        except: pass

    if not email_loc or not pass_loc:
        await page.screenshot(path=str(DOWNLOAD_DIR/"debug_login.png"), full_page=True)
        (DOWNLOAD_DIR/"debug_login.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Não achei campos de login. Veja downloads/debug_login.*")

    await email_loc.click()
    await email_loc.fill(email)
    await pass_loc.click()
    await pass_loc.fill(password)

    # botão de submit
    for b in ['button[type="submit"]','button:has-text("Login")','button:has-text("Entrar")','text=/^Login$/']:
        try: await page.locator(b).first.click(timeout=1500); break
        except: pass
    else:
        try: await pass_loc.press("Enter")
        except: pass

    try: await page.wait_for_load_state("networkidle", timeout=6000)
    except: pass
    log.write("✅ Login submetido.")

async def click_generate_new_report(page, log) -> Optional[int]:
    log.write("🧮 “Gerar novo relatório”…")
    try:
        await modal(page).locator('button.btn-action:has-text("Gerar novo relatório")').first.click(timeout=6000)
    except Exception:
        log.write("ℹ️ Não consegui clicar “Gerar novo relatório”. Vou só baixar o mais recente pronto.")
        return None

    # descobre qual linha virou "Processamento"
    for _ in range(60):
        await page.wait_for_timeout(1000)
        els = action_cells(page); n = await els.count()
        for i in range(n):
            t = await action_text_of(els.nth(i))
            if RE_PROCESS.search(t) or RE_DOWNLOAD.search(t):
                return i
    return None

async def refresh_modal(page):
    try:
        b = modal(page).locator('button[title="Atualizar"]').first
        if await b.is_visible(): await b.click(timeout=800); return True
    except: pass
    return False

async def wait_and_download_same(page, target_index: Optional[int], max_wait_sec: int, log, course_title: str) -> str:
    try:
        await action_cells(page).first.wait_for(timeout=20000)
    except PWTimeout:
        await page.screenshot(path=str(DOWNLOAD_DIR/"debug_modal_no_buttons.png"), full_page=True)
        (DOWNLOAD_DIR/"debug_modal_no_buttons.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Não encontrei a coluna 'Arquivo' no modal. Veja downloads/debug_modal_no_buttons.*")

    elapsed, poll = 0, 1500
    while elapsed < max_wait_sec * 1000:
        els = action_cells(page); n = await els.count()
        if n:
            if target_index is not None and target_index < n:
                el = els.nth(target_index); txt = await action_text_of(el)
                if RE_DOWNLOAD.search(txt):
                    async with page.expect_download(timeout=60000) as d: await el.click()
                    dl = await d.value
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    fname = dl.suggested_filename or "relatorio_teachlr.xlsx"
                    final = DOWNLOAD_DIR / f"{slugify(course_title)}__{ts}__{fname}"
                    await dl.save_as(str(final)); return str(final)
            # senão: primeiro "Baixar" da lista
            for i in range(n):
                el = els.nth(i); txt = await action_text_of(el)
                if RE_DOWNLOAD.search(txt):
                    async with page.expect_download(timeout=60000) as d: await el.click()
                    dl = await d.value
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    fname = dl.suggested_filename or "relatorio_teachlr.xlsx"
                    final = DOWNLOAD_DIR / f"{slugify(course_title)}__{ts}__{fname}"
                    await dl.save_as(str(final)); return str(final)
        if (elapsed // 9000) != ((elapsed + poll) // 9000): await refresh_modal(page)
        await page.wait_for_timeout(poll); elapsed += poll

    await page.screenshot(path=str(DOWNLOAD_DIR/"debug_report_wait_timeout.png"), full_page=True)
    (DOWNLOAD_DIR/"debug_report_wait_timeout.html").write_text(await page.content(), encoding="utf-8")
    raise RuntimeError("Tempo esgotado aguardando o relatório.")

async def run_single_course(course_title: str, generate_first: bool, email: str, password: str, status_container) -> Optional[str]:
    url = students_url_from_title(course_title)
    status_container.write(f"🌐 Abrindo *{course_title}*")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                  "--disable-background-networking","--disable-background-timer-throttling"],
        )
        context = await browser.new_context(
            accept_downloads=True,
            storage_state=str(STATE_PATH) if STATE_PATH.exists() else None,
            viewport={"width": 1440, "height": 900},
        )
        context.set_default_timeout(25000)
        context.set_default_navigation_timeout(35000)
        await context.route("**/*", lambda route, req: asyncio.create_task(route.abort()) if should_block(req) else asyncio.create_task(route.continue_()))
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            try: await page.wait_for_load_state("networkidle", timeout=6000)
            except: pass

            # se cair no login, faça login (agora robusto)
            if "/#dashboard/" not in page.url:
                await login_if_needed(page, email, password, status_container)
                # volta para a página do curso
                await page.goto(url, wait_until="domcontentloaded")
                try: await page.wait_for_load_state("networkidle", timeout=6000)
                except: pass

            await open_reports_modal(page, status_container)
            idx = await click_generate_new_report(page, status_container) if generate_first else None
            saved = await wait_and_download_same(page, idx, max_wait_sec=(480 if generate_first else 180), log=status_container, course_title=course_title)

            try: await context.storage_state(path=str(STATE_PATH))
            except: pass
            return saved
        finally:
            await context.close(); await browser.close()

# ----------------- UI -----------------
st.title("📄 Teachlr – Relatórios de Estudantes em Lote")

with st.sidebar:
    st.subheader("1) Conexão com o Teachlr API")
    TEACHLR_DOMAIN = st.text_input("Domínio (subdomínio do Teachlr)", value=TEACHLR_DOMAIN)
    TEACHLR_API_KEY = st.text_input("Authorization (API Key)", value=TEACHLR_API_KEY, type="password")
    st.subheader("2) Login Web (para baixar relatório)")
    TEACHLR_EMAIL = st.text_input("E-mail", value=TEACHLR_EMAIL)
    TEACHLR_PASSWORD = st.text_input("Senha", value=TEACHLR_PASSWORD, type="password")
    generate_first = st.checkbox("Gerar novo relatório antes de baixar", value=True)

if "courses" not in st.session_state: st.session_state.courses = []
courses = st.session_state.courses

c1, c2 = st.columns([1,2])
with c1:
    if st.button("🔄 Carregar/Atualizar cursos"):
        if not TEACHLR_DOMAIN or not TEACHLR_API_KEY:
            st.error("Preencha domínio e API key.")
        else:
            with st.spinner("Buscando cursos…"):
                try:
                    st.session_state.courses = fetch_courses_all(TEACHLR_DOMAIN, TEACHLR_API_KEY)
                    st.success(f"Carregados {len(st.session_state.courses)} cursos.")
                except Exception as e:
                    st.error(str(e))
with c2:
    if courses: st.write(f"Total de cursos: **{len(courses)}**")

selected_titles: List[str] = []
if courses:
    q = st.text_input("🔎 Filtrar cursos pelo nome")
    filt = [c for c in courses if (q or "").lower() in (c.get("title","").lower())] if q else courses
    sel_all = st.checkbox(f"Selecionar todos os {len(filt)} cursos filtrados", value=False)
    options = [c.get("title","") for c in filt]
    selected_titles = options if sel_all else st.multiselect("Escolha 1+ cursos:", options=options, default=[])
    with st.expander("Prévia das URLs (slug)"):
        st.json([{"title": t, "slug": slugify(t), "students_url": students_url_from_title(t)} for t in selected_titles])

run_btn = st.button("🚀 Gerar & Baixar relatórios dos cursos selecionados", type="primary", disabled=not selected_titles)
if run_btn:
    if not TEACHLR_EMAIL or not TEACHLR_PASSWORD:
        st.error("Informe e-mail e senha.")
    else:
        ensure_playwright_installed(st)
        results, status = [], st.empty()
        prog = st.progress(0.0)
        for i, title in enumerate(selected_titles, start=1):
            status.write(f"**[{i}/{len(selected_titles)}]** {title}")
            try:
                saved = asyncio.run(run_single_course(title, generate_first, TEACHLR_EMAIL, TEACHLR_PASSWORD, status))
                if saved: results.append(saved)
            except Exception as e:
                st.error(f"Falhou em **{title}**: {e}")
            prog.progress(i/len(selected_titles))
        status.write("✅ Finalizado.")
        if results:
            st.success(f"{len(results)} arquivo(s) baixado(s):")
            for p in results:
                with open(p, "rb") as f:
                    st.download_button(f"⬇️ {Path(p).name}", f, file_name=Path(p).name, mime="application/octet-stream")
        else:
            st.warning("Nenhum arquivo foi baixado.")

# Debug artifacts
dbg = sorted(DOWNLOAD_DIR.glob("debug_*"))
if dbg:
    with st.expander("🔍 Artefatos de debug"):
        for p in dbg:
            if p.suffix == ".png": st.image(str(p), caption=p.name)
            else:
                with open(p,"rb") as f: st.download_button(f"Baixar {p.name}", f, file_name=p.name)

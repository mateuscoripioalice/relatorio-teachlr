# streamlit_app.py
import asyncio
import os
import re
import unicodedata
import json
from pathlib import Path
from typing import List, Dict, Optional

import requests
import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Request
from datetime import datetime

# ====================== CONFIG GERAL ======================
st.set_page_config(page_title="Teachlr | Relatórios em Lote", page_icon="📄", layout="wide")

DOWNLOAD_DIR = Path("./downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)
STATE_PATH = Path("/tmp/state_teachlr.json")

# Secrets / Env
TEACHLR_DOMAIN   = st.secrets.get("TEACHLR_DOMAIN",   os.getenv("TEACHLR_DOMAIN", "alice"))
TEACHLR_API_KEY  = st.secrets.get("TEACHLR_API_KEY",  os.getenv("TEACHLR_API_KEY", ""))
TEACHLR_EMAIL    = st.secrets.get("TEACHLR_EMAIL",    os.getenv("TEACHLR_EMAIL", ""))
TEACHLR_PASSWORD = st.secrets.get("TEACHLR_PASSWORD", os.getenv("TEACHLR_PASSWORD", ""))

BASE_HOST = f"https://{TEACHLR_DOMAIN}.teachlr.com"

# ====================== UTIL: SLUG ======================
def slugify(title: str) -> str:
    # remove chaves {…}
    title = re.sub(r"[{}]", "", title)
    # remove acentos
    nfkd = unicodedata.normalize("NFKD", title)
    noacc = "".join(c for c in nfkd if not unicodedata.combining(c))
    # minúsculas
    s = noacc.lower()
    # troca qualquer coisa não alfanumérica por "-"
    s = re.sub(r"[^a-z0-9]+", "-", s)
    # remove - no início/fim e normaliza múltiplos
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s

def students_url_from_title(title: str) -> str:
    return f"{BASE_HOST}/#dashboard/instructor/{slugify(title)}/students"

# ====================== API: LISTAGEM DE CURSOS ======================
def fetch_courses_all(domain: str, api_key: str, max_pages: int = 200) -> List[Dict]:
    """
    Busca todos os cursos com paginação. Tenta ?page=?&per_page=100.
    Se o endpoint ignorar params, ainda assim para quando uma página vier vazia
    ou repetir resultados.
    """
    base = f"https://api.teachlr.com/{domain}/api/courses"
    headers = {
        "Content-Type": "application/json",
        "Authorization": api_key.strip(),
    }

    all_courses: List[Dict] = []
    seen_ids = set()
    for page in range(1, max_pages + 1):
        params = {"page": page, "per_page": 100}
        r = requests.get(base, headers=headers, params=params, timeout=30)
        if r.status_code != 200:
            # fallback: tentativa sem params na primeira página
            if page == 1:
                r = requests.get(base, headers=headers, timeout=30)
                if r.status_code != 200:
                    raise RuntimeError(f"API /courses falhou ({r.status_code}): {r.text[:200]}")
            else:
                break

        data = r.json()
        if not isinstance(data, list):
            # algumas APIs embrulham em objeto {data: [...]}
            data = data.get("data", [])
        if not data:
            break

        new_items = 0
        for c in data:
            cid = c.get("id")
            if cid not in seen_ids:
                seen_ids.add(cid)
                all_courses.append(c)
                new_items += 1
        if new_items == 0:
            # nada novo → provavelmente acabou
            break
    # ordena por título
    all_courses.sort(key=lambda x: (x.get("title") or "").lower())
    return all_courses

# ====================== PLAYWRIGHT HELPERS (mesmos do fluxo anterior) ======================
def should_block(req: Request) -> bool:
    BLOCK_EXT = (".png",".jpg",".jpeg",".gif",".webp",".svg",".mp4",".webm",".woff",".woff2",".ttf",".otf",".eot")
    return any(req.url.lower().endswith(ext) for ext in BLOCK_EXT)

def ensure_playwright_installed(log):
    try:
        from playwright.___impl._driver import compute_driver_executable  # type: ignore
        _ = compute_driver_executable()
    except Exception:
        log.write("🧩 Instalando Chromium do Playwright…")
        import subprocess
        subprocess.run(["python","-m","playwright","install","--with-deps","chromium"],
                       check=False, capture_output=True)
        log.write("✅ Chromium pronto.")

# Selectors/modal
def modal(page):
    return page.locator("div.dialog__content")

def action_cells(page):
    return modal(page).locator("tbody tr td:nth-child(3) .btn-lineal")

async def action_text_of(el) -> str:
    try:
        inner = el.locator("span").last
        txt = (await inner.inner_text()).strip()
        return txt
    except:
        return (await el.inner_text()).strip()

RE_DOWNLOAD = re.compile(r"\bBaixar\b", re.I)
RE_PROCESS  = re.compile(r"Processa(mento|ndo)", re.I)

async def open_reports_modal(page, log):
    log.write("📄 Abrindo modal “Desempenho dos estudantes”…")
    for sel in [
        'button:has-text("Desempenho dos estudantes")',
        'text=/Desempenho dos estudantes/i'
    ]:
        try:
            await page.locator(sel).first.click(timeout=8000)
            return
        except:
            continue
    await page.screenshot(path=str(DOWNLOAD_DIR/"debug_students.png"), full_page=True)
    (DOWNLOAD_DIR/"debug_students.html").write_text(await page.content(), encoding="utf-8")
    raise RuntimeError('Não achei "Desempenho dos estudantes". Veja downloads/debug_students.*')

async def login_if_needed(page, email, password, log):
    if "/#signin" not in page.url:
        try:
            await page.get_by_text(re.compile(r"(Estudantes|Conteúdo|Anúncios)", re.I)).first.wait_for(timeout=3500)
            log.write("🔐 Sessão ativa.")
            return
        except PWTimeout:
            pass

    log.write("🔐 Logando…")
    async def find(selectors):
        ctxs = [page, *page.frames]
        for ctx in ctxs:
            for s in selectors:
                try:
                    loc = ctx.locator(s).first
                    await loc.wait_for(timeout=1200)
                    return loc
                except:
                    pass
        return None

    email_loc = await find(['input[type="email"]','input[name="email"]','input[placeholder*="mail" i]','#email'])
    pass_loc  = await find(['input[type="password"]','input[name="password"]','#password','input[placeholder*="senha" i]'])
    if not email_loc or not pass_loc:
        await page.screenshot(path=str(DOWNLOAD_DIR/"debug_login.png"), full_page=True)
        (DOWNLOAD_DIR/"debug_login.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Não achei campos de login. Veja downloads/debug_login.*")

    await email_loc.fill(email)
    await pass_loc.fill(password)
    for b in ['button[type="submit"]','button:has-text("Login")','button:has-text("Entrar")','text=/^Login$/']:
        try:
            await page.locator(b).first.click(timeout=1000)
            break
        except: pass
    else:
        try: await pass_loc.press("Enter")
        except: pass

    await page.wait_for_load_state("domcontentloaded")
    log.write("✅ Login submetido.")

async def click_generate_new_report(page, log) -> Optional[int]:
    log.write("🧮 “Gerar novo relatório”…")
    btn = modal(page).locator('button.btn-action:has-text("Gerar novo relatório")').first
    try:
        await btn.click(timeout=6000)
    except Exception:
        log.write("ℹ️ Não consegui clicar “Gerar novo relatório”. Vou só baixar o mais recente pronto.")
        return None

    # identifica a linha “Processamento”
    for _ in range(60):
        await page.wait_for_timeout(1000)
        els = action_cells(page)
        n = await els.count()
        for i in range(n):
            t = await action_text_of(els.nth(i))
            if RE_PROCESS.search(t) or RE_DOWNLOAD.search(t):
                return i
    return None

async def refresh_modal(page):
    try:
        btn = modal(page).locator('button[title="Atualizar"]').first
        if await btn.is_visible():
            await btn.click(timeout=800)
            return True
    except: pass
    return False

async def wait_and_download_same(page, target_index: Optional[int], max_wait_sec: int, log, course_title: str) -> str:
    try:
        await action_cells(page).first.wait_for(timeout=20000)
    except PWTimeout:
        await page.screenshot(path=str(DOWNLOAD_DIR/"debug_modal_no_buttons.png"), full_page=True)
        (DOWNLOAD_DIR/"debug_modal_no_buttons.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Não encontrei a coluna 'Arquivo' no modal. Veja downloads/debug_modal_no_buttons.*")

    elapsed = 0
    poll = 1500
    while elapsed < max_wait_sec * 1000:
        els = action_cells(page)
        n = await els.count()
        if n:
            # tenta item alvo
            if target_index is not None and target_index < n:
                el = els.nth(target_index)
                txt = await action_text_of(el)
                if RE_DOWNLOAD.search(txt):
                    async with page.expect_download(timeout=60000) as d:
                        await el.click()
                    dl = await d.value
                    suggested = dl.suggested_filename or "relatorio_teachlr.xlsx"
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    final = DOWNLOAD_DIR / f"{slugify(course_title)}__{ts}__{suggested}"
                    await dl.save_as(str(final))
                    return str(final)

            # senão, baixa o primeiro “Baixar” disponível
            for i in range(n):
                el = els.nth(i); txt = await action_text_of(el)
                if RE_DOWNLOAD.search(txt):
                    async with page.expect_download(timeout=60000) as d:
                        await el.click()
                    dl = await d.value
                    suggested = dl.suggested_filename or "relatorio_teachlr.xlsx"
                    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                    final = DOWNLOAD_DIR / f"{slugify(course_title)}__{ts}__{suggested}"
                    await dl.save_as(str(final))
                    return str(final)

        if (elapsed // 9000) != ((elapsed + poll) // 9000):
            await refresh_modal(page)

        await page.wait_for_timeout(poll)
        elapsed += poll

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
            try: await page.wait_for_load_state("networkidle", timeout=8000)
            except: pass

            await login_if_needed(page, email, password, status_container)

            if "/#dashboard/" not in page.url:
                await page.goto(url, wait_until="domcontentloaded")

            await open_reports_modal(page, status_container)

            idx = None
            if generate_first:
                idx = await click_generate_new_report(page, status_container)

            saved = await wait_and_download_same(page, idx, max_wait_sec=(480 if generate_first else 180), log=status_container, course_title=course_title)
            try: await context.storage_state(path=str(STATE_PATH))
            except: pass
            return saved
        finally:
            await context.close()
            await browser.close()

# ====================== UI ======================
st.title("📄 Teachlr – Relatórios de Estudantes em Lote")

with st.sidebar:
    st.subheader("1) Conexão com o Teachlr API")
    TEACHLR_DOMAIN = st.text_input("Domínio (subdomínio do Teachlr)", value=TEACHLR_DOMAIN, placeholder="ex.: alice")
    TEACHLR_API_KEY = st.text_input("Authorization (API Key)", value=TEACHLR_API_KEY, type="password")

    st.subheader("2) Login Web (para baixar relatório)")
    TEACHLR_EMAIL = st.text_input("E-mail", value=TEACHLR_EMAIL)
    TEACHLR_PASSWORD = st.text_input("Senha", value=TEACHLR_PASSWORD, type="password")

    generate_first = st.checkbox("Gerar novo relatório antes de baixar", value=True)
    st.caption("Dica: marque quando quiser forçar um relatório fresquinho; desmarque se só quer baixar o último gerado.")

if "courses" not in st.session_state:
    st.session_state.courses = []
if "filtered_ids" not in st.session_state:
    st.session_state.filtered_ids = set()

colA, colB = st.columns([1,2], vertical_alignment="center")
with colA:
    if st.button("🔄 Carregar/Atualizar cursos"):
        if not TEACHLR_DOMAIN or not TEACHLR_API_KEY:
            st.error("Preencha domínio e API key.")
        else:
            with st.spinner("Buscando cursos…"):
                try:
                    courses = fetch_courses_all(TEACHLR_DOMAIN, TEACHLR_API_KEY)
                    st.session_state.courses = courses
                    st.success(f"Carregados {len(courses)} cursos.")
                except Exception as e:
                    st.error(str(e))

with colB:
    if st.session_state.courses:
        st.write(f"Total de cursos: **{len(st.session_state.courses)}**")

# ==== Lista / Filtro / Seleção ====
courses = st.session_state.courses
selected_titles: List[str] = []

if courses:
    q = st.text_input("🔎 Filtrar por nome do curso")
    if q:
        filt = [c for c in courses if q.lower() in (c.get("title","").lower())]
    else:
        filt = courses

    # check de selecionar todos filtrados
    sel_all = st.checkbox(f"Selecionar todos os {len(filt)} cursos filtrados", value=False, key="sel_all_toggle")
    labels = [f'{c.get("title","")}  (id {c.get("id")})' for c in filt]
    values = [c.get("title","") for c in filt]

    if sel_all:
        selected_titles = values
    else:
        selected_titles = st.multiselect("Escolha 1+ cursos:", options=values, default=[])

    # mostra prévia de URLs geradas (slug)
    with st.expander("Ver slugs/URLs que serão usadas"):
        preview = [
            {"title": t, "slug": slugify(t), "students_url": students_url_from_title(t)}
            for t in selected_titles
        ]
        st.json(preview)

# ==== Ação principal: rodar em lote ====
run_btn = st.button("🚀 Gerar & Baixar relatórios dos cursos selecionados", type="primary", disabled=not selected_titles)

if run_btn:
    if not TEACHLR_EMAIL or not TEACHLR_PASSWORD:
        st.error("Informe e-mail e senha para login web.")
    else:
        ensure_playwright_installed(st)
        results = []
        progress = st.progress(0)
        status = st.empty()

        for i, title in enumerate(selected_titles, start=1):
            status.write(f"**[{i}/{len(selected_titles)}]** Processando: _{title}_ …")
            try:
                saved = asyncio.run(run_single_course(title, generate_first, TEACHLR_EMAIL, TEACHLR_PASSWORD, status))
                if saved:
                    results.append(saved)
            except Exception as e:
                st.error(f"Falhou em **{title}**: {e}")
            progress.progress(i/len(selected_titles))

        status.write("✅ Finalizado.")
        if results:
            st.success(f"Arquivos prontos ({len(results)}):")
            for p in results:
                with open(p, "rb") as f:
                    st.download_button(f"⬇️ {Path(p).name}", f, file_name=Path(p).name, mime="application/octet-stream")
        else:
            st.warning("Nenhum arquivo foi baixado.")

# Área de debug se existir
dbg_files = sorted(DOWNLOAD_DIR.glob("debug_*"))
if dbg_files:
    with st.expander("🔍 Artefatos de debug"):
        for p in dbg_files:
            if p.suffix == ".png":
                st.image(str(p), caption=p.name)
            else:
                with open(p, "rb") as f:
                    st.download_button(f"Baixar {p.name}", f, file_name=p.name)

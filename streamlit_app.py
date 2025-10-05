import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List

import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Request

# ====================== CONFIG ======================
st.set_page_config(page_title="Teachlr | Relatório de Estudantes", page_icon="📄")

DEFAULT_STUDENTS_URL = (
    "https://alice.teachlr.com/#dashboard/instructor/"
    "skip-level-meeting-pitayas-navegs-nurses-physicians-e-sales/students"
)

LOGIN = st.secrets.get("TEACHLR_EMAIL", os.getenv("TEACHLR_EMAIL", ""))
PASSWORD = st.secrets.get("TEACHLR_PASSWORD", os.getenv("TEACHLR_PASSWORD", ""))

DOWNLOAD_DIR = Path("./downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)
STATE_PATH = Path("/tmp/state_teachlr.json")

RE_DOWNLOAD = re.compile(r"\bBaixar\b", re.I)
RE_PROCESS = re.compile(r"Processa(mento|ndo)", re.I)

# ====================== HELPERS ======================
def should_block(req: Request) -> bool:
    BLOCK_EXT = (".png",".jpg",".jpeg",".gif",".webp",".svg",".mp4",".webm",".woff",".woff2",".ttf",".otf",".eot")
    return any(req.url.lower().endswith(ext) for ext in BLOCK_EXT)

def show_debug():
    dbg = sorted(DOWNLOAD_DIR.glob("debug_*"))
    if not dbg: return
    st.caption("Arquivos de debug:")
    for p in dbg:
        if p.suffix == ".png":
            st.image(str(p), caption=p.name)
        else:
            with open(p, "rb") as f:
                st.download_button(f"Baixar {p.name}", f, file_name=p.name)

def ensure_playwright_installed(log):
    try:
        from playwright.___impl._driver import compute_driver_executable  # type: ignore
        _ = compute_driver_executable()
    except Exception:
        log.write("🧩 Instalando Chromium do Playwright…")
        subprocess.run(["python","-m","playwright","install","--with-deps","chromium"],
                       check=False, capture_output=True)
        log.write("✅ Chromium pronto.")

# ---------------------- Selectors do modal Teachlr ----------------------
def modal(page):
    # container do modal de relatórios
    return page.locator("div.dialog__content")

def table_rows(page):
    return modal(page).locator("tbody tr")

def action_cells(page):
    # coluna Arquivo → <td> (3a coluna) com <span class='btn-lineal'> … <span>Baixar|Processamento</span>
    return modal(page).locator("tbody tr td:nth-child(3) .btn-lineal")

async def action_text_of(el) -> str:
    # pega o último <span> interno (onde está “Baixar” / “Processamento”)
    try:
        inner = el.locator("span").last
        txt = (await inner.inner_text()).strip()
        return txt
    except:
        return (await el.inner_text()).strip()

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
    # já logado?
    if "/#signin" not in page.url:
        try:
            await page.get_by_text(re.compile(r"(Estudantes|Conteúdo|Anúncios)", re.I)).first.wait_for(timeout=4000)
            log.write("🔐 Sessão ativa.")
            return
        except PWTimeout:
            pass

    log.write("🔐 Logando na página de sign-in…")
    # tenta achar os campos em qualquer frame
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
    # clica Login/Entrar
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

async def snapshot_actions(page) -> List[str]:
    els = action_cells(page)
    try:
        n = await els.count()
    except:
        n = 0
    out = []
    for i in range(n):
        try:
            out.append(await action_text_of(els.nth(i)))
        except:
            out.append("")
    return out

async def click_generate_new_report(page, log) -> Optional[int]:
    """Clica 'Gerar novo relatório' e devolve o índice da nova linha (normalmente 0)."""
    log.write("🧮 Clicando “Gerar novo relatório”…")
    btn = modal(page).locator('button.btn-action:has-text("Gerar novo relatório")').first
    try:
        await btn.click(timeout=5000)
    except Exception:
        log.write("ℹ️ Não consegui clicar “Gerar novo relatório”. Vou apenas baixar o mais recente pronto.")
        return None

    # A lista é ordenada desc. O novo geralmente aparece na 1ª linha como “Processamento”.
    for _ in range(60):  # até ~60s para aparecer “Processamento”
        await page.wait_for_timeout(1000)
        texts = await snapshot_actions(page)
        if not texts:
            continue
        # pegue o índice do primeiro "Processa..."
        for i, t in enumerate(texts):
            if RE_PROCESS.search(t):
                return i
        # às vezes vem direto como Baixar (cacheado)
        for i, t in enumerate(texts):
            if RE_DOWNLOAD.search(t):
                return i
    return None

async def refresh_modal(page):
    # botão de atualizar com title="Atualizar"
    try:
        btn = modal(page).locator('button[title="Atualizar"]').first
        if await btn.is_visible():
            await btn.click(timeout=800)
            return True
    except: pass
    return False

async def wait_and_download_same(page, target_index: Optional[int], max_wait_sec: int, log) -> str:
    """Mantém foco no MESMO item (se informado) até virar 'Baixar'. Caso None, baixa o 1º 'Baixar' disponível."""
    # garante que a coluna Arquivo existe
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
        count = await els.count()
        if count:
            # tenta o índice-alvo primeiro
            if target_index is not None and target_index < count:
                el = els.nth(target_index)
                txt = await action_text_of(el)
                if RE_DOWNLOAD.search(txt):
                    # alguns sites disparam download por navegação; expect_download cobre ambos
                    async with page.expect_download(timeout=60000) as d:
                        await el.click()
                    dl = await d.value
                    suggested = dl.suggested_filename or "relatorio_teachlr.xlsx"
                    final = DOWNLOAD_DIR / suggested
                    await dl.save_as(str(final))
                    log.write("✅ Baixei o relatório do MESMO item acompanhado.")
                    return str(final)

            # sem alvo, tenta qualquer “Baixar” habilitado (de cima p/ baixo)
            for i in range(count):
                el = els.nth(i)
                txt = await action_text_of(el)
                if RE_DOWNLOAD.search(txt):
                    async with page.expect_download(timeout=60000) as d:
                        await el.click()
                    dl = await d.value
                    suggested = dl.suggested_filename or "relatorio_teachlr.xlsx"
                    final = DOWNLOAD_DIR / suggested
                    await dl.save_as(str(final))
                    log.write("✅ Baixei o relatório disponível.")
                    return str(final)

        # refresh a cada ~8–10s
        if (elapsed // 9000) != ((elapsed + poll) // 9000):
            await refresh_modal(page)

        await page.wait_for_timeout(poll)
        elapsed += poll

    await page.screenshot(path=str(DOWNLOAD_DIR/"debug_report_wait_timeout.png"), full_page=True)
    (DOWNLOAD_DIR/"debug_report_wait_timeout.html").write_text(await page.content(), encoding="utf-8")
    raise RuntimeError("Tempo esgotado aguardando o relatório. Veja downloads/debug_report_wait_timeout.*")

# ====================== FLUXO PRINCIPAL ======================
async def run_flow(students_url: str, force_generate: bool, email: str, password: str, log) -> str:
    ensure_playwright_installed(log)

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
            log.write("🌐 Abrindo a aba *Estudantes* do curso…")
            await page.goto(students_url, wait_until="domcontentloaded")
            try: await page.wait_for_load_state("networkidle", timeout=8000)
            except: pass

            await login_if_needed(page, email, password, log)

            if "/#dashboard/" not in page.url:
                await page.goto(students_url, wait_until="domcontentloaded")

            await open_reports_modal(page, log)

            idx = None
            if force_generate:
                idx = await click_generate_new_report(page, log)

            max_wait = 480 if force_generate else 180
            saved = await wait_and_download_same(page, idx, max_wait_sec=max_wait, log=log)

            try: await context.storage_state(path=str(STATE_PATH))
            except: pass
            return saved

        finally:
            await context.close()
            await browser.close()

# ====================== UI ======================
st.title("📄 Teachlr – Relatório de Desempenho dos Estudantes")
with st.sidebar:
    st.subheader("Configuração")
    students_url = st.text_input("URL da aba *Estudantes*", value=DEFAULT_STUDENTS_URL)
    email = st.text_input("E-mail (Teachlr)", value=LOGIN, type="default")
    password = st.text_input("Senha (Teachlr)", value=PASSWORD, type="password")
    force = st.checkbox("Gerar novo relatório antes de baixar", value=True)
    btn = st.button("🚀 Gerar & Baixar")

st.write("Após rodar, o arquivo para download aparece abaixo.")

if btn:
    if not students_url.strip():
        st.error("Informe a URL de *Estudantes* do curso.")
    elif not email or not password:
        st.error("Informe e-mail e senha.")
    else:
        status = st.status("Iniciando…")
        try:
            path = asyncio.run(run_flow(students_url.strip(), force, email.strip(), password, status))
            status.update(label="Concluído!", state="complete")
            with open(path, "rb") as f:
                st.download_button("⬇️ Baixar arquivo", f, file_name=Path(path).name, mime="application/octet-stream")
        except Exception as e:
            status.update(label="Falhou", state="error")
            st.error(str(e))
            show_debug()

# debug persistente
if any(DOWNLOAD_DIR.glob("debug_*")):
    with st.expander("🔍 Ver artefatos de debug"):
        show_debug()

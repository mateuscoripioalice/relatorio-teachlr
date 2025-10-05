import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Request

st.set_page_config(page_title="Teachlr | Relatório de Estudantes", page_icon="📄")

BASE_APP = "https://alice.teachlr.com/"
DEFAULT_STUDENTS_URL = (
    "https://alice.teachlr.com/#dashboard/instructor/"
    "skip-level-meeting-pitayas-navegs-nurses-physicians-e-sales/students"
)

LOGIN = st.secrets.get("TEACHLR_EMAIL", os.getenv("TEACHLR_EMAIL", ""))
PASSWORD = st.secrets.get("TEACHLR_PASSWORD", os.getenv("TEACHLR_PASSWORD", ""))

DOWNLOAD_DIR = Path("./downloads"); DOWNLOAD_DIR.mkdir(exist_ok=True)
STATE_PATH = Path("/tmp/state_teachlr.json")

# Bloqueie só mídia pesada; não bloqueie JS/CSS
BLOCK_EXT = (".png",".jpg",".jpeg",".gif",".webp",".svg",".mp4",".webm",".woff",".woff2",".ttf",".otf",".eot")
def should_block(req: Request) -> bool:
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

# ---------- util: garantir chromium instalado ----------
def ensure_playwright_installed(log):
    try:
        # checa se já há um browser baixado
        from playwright.___impl._driver import compute_driver_executable
        _ = compute_driver_executable()  # só força import; se falhar, instala
    except Exception:
        log.write("🧩 Instalando Chromium do Playwright (primeira execução)...")
        # instala com dependências (seguro na Cloud)
        subprocess.run(
            ["python", "-m", "playwright", "install", "--with-deps", "chromium"],
            check=False, capture_output=True
        )
        log.write("✅ Chromium instalado (ou já presente).")

# ---------- login apenas se necessário ----------
async def login_if_needed(page, email: str, password: str, log) -> None:
    on_signin = bool(re.search(r"/#signin", page.url))
    if not on_signin:
        try:
            await page.locator(
                'input[type="password"], input[type="email"], '
                'input[placeholder*="senha" i], input[placeholder*="mail" i]'
            ).first.wait_for(timeout=1200)
            on_signin = True
        except PWTimeout:
            log.write("🔐 Já parece logado (não vou relogar).")
            return

    log.write("🔐 Fazendo login por e-mail/senha…")

    async def find(selectors):
        ctxs = [page, *page.frames]
        for ctx in ctxs:
            for sel in selectors:
                try:
                    loc = ctx.locator(sel).first
                    await loc.wait_for(timeout=1500)
                    return loc
                except: pass
        return None

    email_loc = await find([
        'input[placeholder*="e-mail" i]', 'input[placeholder*="email" i]',
        'input[name="email"]', 'input[name="username"]', 'input[type="email"]', '#email'
    ])
    senha_loc = await find([
        'input[placeholder*="senha" i]', 'input[type="password"]',
        'input[name="password"]', '#password'
    ])
    if not email_loc or not senha_loc:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_login.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_login.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError('Não achei campos de login. Veja downloads/debug_login.*')

    await email_loc.click(); await email_loc.fill(email)
    await senha_loc.click(); await senha_loc.fill(password)

    clicked = False
    for bsel in [
        'button[type="submit"]','button:has-text("Login")','button:has-text("Entrar")',
        'text=/^Login$/','text=/^Entrar$/'
    ]:
        try:
            await page.locator(bsel).first.click(timeout=900)
            clicked = True; break
        except: pass
    if not clicked:
        try: await senha_loc.press("Enter")
        except: pass

    await page.wait_for_load_state("domcontentloaded")
    log.write("✅ Login submetido.")

# ---------- UI actions ----------
async def open_reports_modal(page, log):
    log.write("📄 Abrindo modal “Desempenho dos estudantes”…")
    try:
        btn = page.get_by_role("button", name=re.compile(r"Desempenho dos estudantes", re.I))
        await btn.first.wait_for(timeout=8000)
        await btn.first.click()
        return
    except:
        pass
    try:
        await page.get_by_text(re.compile(r"Desempenho dos estudantes", re.I)).first.click(timeout=6000)
        return
    except:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_students.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_students.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError('Não achei "Desempenho dos estudantes". Veja downloads/debug_students.*')

async def click_generate_new_report(page, log):
    log.write("🧮 Clicando “Gerar novo relatório”…")
    for sel in [
        'button:has-text("Gerar novo relatório")',
        'text=/^Gerar novo relatório$/i',
        'role=button[name="Gerar novo relatório"]',
    ]:
        try:
            await page.locator(sel).first.click(timeout=4000)
            log.write("✅ Solicitação de geração enviada.")
            return
        except: pass
    log.write("ℹ️ Não encontrei o botão “Gerar novo relatório” (seguindo para baixar o existente).")

async def wait_and_download_latest(page, max_wait_sec: int, log) -> str:
    log.write("⏳ Aguardando botão “Baixar”…")
    try:
        await page.get_by_role("button", name=re.compile(r"Baixar", re.I)).first.wait_for(timeout=max_wait_sec*1000)
    except PWTimeout:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_modal_timeout.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_modal_timeout.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Demorou demais esperando “Baixar”. Veja downloads/debug_modal_timeout.*")

    dl_button = page.get_by_role("button", name=re.compile(r"Baixar", re.I)).first
    log.write("⬇️ Iniciando download…")
    download = await page.expect_download(timeout=60000)
    await dl_button.click()
    suggested = download.suggested_filename or "relatorio_teachlr.xlsx"
    final_path = DOWNLOAD_DIR / suggested
    await download.save_as(str(final_path))
    log.write(f"✅ Download salvo: {final_path.name}")
    return str(final_path)

# ---------- fluxo principal ----------
async def run_flow(students_url: str, force_generate: bool, email: str, password: str, log) -> str:
    ensure_playwright_installed(log)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                "--disable-background-networking","--disable-background-timer-throttling",
            ],
        )
        context = await browser.new_context(
            accept_downloads=True,
            storage_state=str(STATE_PATH) if STATE_PATH.exists() else None,
            viewport={"width": 1366, "height": 900},
        )
        context.set_default_timeout(20000)      # 20s padrão
        context.set_default_navigation_timeout(30000)

        async def router(route, request):
            try:
                if should_block(request): await route.abort()
                else: await route.continue_()
            except:
                try: await route.continue_()
                except: pass

        await context.route("**/*", router)
        page = await context.new_page()

        try:
            log.write("🌐 Abrindo a aba *Estudantes* do curso…")
            await page.goto(students_url, wait_until="domcontentloaded")
            try: await page.wait_for_load_state("networkidle", timeout=8000)
            except: pass

            await login_if_needed(page, email, password, log)

            if not re.search(r"/#dashboard/.*/students", page.url):
                log.write("↩️ Voltando para a aba *Estudantes*…")
                await page.goto(students_url, wait_until="domcontentloaded")
                try: await page.wait_for_load_state("networkidle", timeout=8000)
                except: pass

            await open_reports_modal(page, log)
            if force_generate:
                await click_generate_new_report(page, log)

            saved = await wait_and_download_latest(page, max_wait_sec=240, log=log)

            try:
                await context.storage_state(path=str(STATE_PATH))
            except: pass

            return saved

        finally:
            await context.close()
            await browser.close()

# ---------- UI ----------
st.title("📄 Teachlr – Relatório de Desempenho dos Estudantes")
with st.sidebar:
    st.subheader("Configuração")
    students_url = st.text_input("URL da aba *Estudantes* do curso", value=DEFAULT_STUDENTS_URL)
    st.caption("Ex.: https://alice.teachlr.com/#dashboard/.../students")
    email = st.text_input("E-mail (Teachlr)", value=LOGIN, type="default")
    password = st.text_input("Senha (Teachlr)", value=PASSWORD, type="password")
    force = st.checkbox("Gerar novo relatório antes de baixar", value=True)
    run_btn = st.button("🚀 Gerar & Baixar")

st.write("Use a barra lateral para configurar. O arquivo baixado aparecerá abaixo quando pronto.")

if run_btn:
    if not students_url.strip():
        st.error("Informe a URL de *Estudantes* do curso.")
    elif not email or not password:
        st.error("Informe e-mail e senha.")
    else:
        log = st.status("Iniciando…", state="running")
        try:
            path = asyncio.run(run_flow(students_url.strip(), force, email.strip(), password, log))
            log.update(label="Concluído!", state="complete")
            with open(path, "rb") as f:
                st.download_button("⬇️ Baixar arquivo", f, file_name=Path(path).name, mime="application/octet-stream")
        except Exception as e:
            log.update(label="Falhou", state="error")
            st.error(str(e))
            show_debug()

# Sempre mostra debug se existir
if any(DOWNLOAD_DIR.glob("debug_*")):
    with st.expander("🔍 Ver artefatos de debug"):
        show_debug()

import asyncio
import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple, List

import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Request

# ---------------------- Config da página ----------------------
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

# textos que aparecem no botão/coluna "Arquivo"
RE_DOWNLOAD = re.compile(r"Baixar", re.I)
RE_PROCESS = re.compile(r"(Processando|Processamento)", re.I)

# ---------------------- Filtro de rede ----------------------
BLOCK_EXT = (".png",".jpg",".jpeg",".gif",".webp",".svg",".mp4",".webm",".woff",".woff2",".ttf",".otf",".eot")
def should_block(req: Request) -> bool:
    return any(req.url.lower().endswith(ext) for ext in BLOCK_EXT)

# ---------------------- Debug helpers ----------------------
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

# ---------------------- Garantir browser ----------------------
def ensure_playwright_installed(log):
    try:
        from playwright.___impl._driver import compute_driver_executable
        _ = compute_driver_executable()
    except Exception:
        log.write("🧩 Instalando Chromium do Playwright (primeira execução)…")
        subprocess.run(
            ["python", "-m", "playwright", "install", "--with-deps", "chromium"],
            check=False, capture_output=True
        )
        log.write("✅ Chromium instalado (ou já presente).")

# ---------------------- Login se necessário ----------------------
async def login_if_needed(page, email: str, password: str, log) -> None:
    if "/#signin" not in page.url:
        # Tenta detectar se já está logado procurando algo típico de dashboard
        try:
            await page.get_by_text(re.compile(r"Estudantes|Conteúdo|Anúncios", re.I)).first.wait_for(timeout=4000)
            log.write("🔐 Sessão ativa — sem relogar.")
            return
        except PWTimeout:
            pass

    log.write("🔐 Fazendo login por e-mail/senha…")

    async def find(selectors):
        ctxs = [page, *page.frames]
        for ctx in ctxs:
            for sel in selectors:
                try:
                    loc = ctx.locator(sel).first
                    await loc.wait_for(timeout=1500)
                    return loc
                except:
                    pass
        return None

    email_loc = await find([
        'input[placeholder*="e-mail" i]','input[placeholder*="email" i]',
        'input[name="email"]','input[name="username"]','input[type="email"]','#email'
    ])
    senha_loc = await find([
        'input[placeholder*="senha" i]','input[type="password"]',
        'input[name="password"]','#password'
    ])
    if not email_loc or not senha_loc:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_login.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_login.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError('Não achei campos de login. Veja downloads/debug_login.*')

    await email_loc.click(); await email_loc.fill(email)
    await senha_loc.click(); await senha_loc.fill(password)

    # clica Login/Entrar
    for bsel in [
        'button[type="submit"]','button:has-text("Login")','button:has-text("Entrar")',
        'text=/^Login$/','text=/^Entrar$/'
    ]:
        try:
            await page.locator(bsel).first.click(timeout=900)
            break
        except:
            continue
    else:
        try: await senha_loc.press("Enter")
        except: pass

    await page.wait_for_load_state("domcontentloaded")
    log.write("✅ Login submetido.")

# ---------------------- Ações do modal ----------------------
def dialog_scope(page):
    try:
        return page.get_by_role("dialog", name=re.compile(r"Relat[oó]rios", re.I))
    except:
        return page.locator('div[role="dialog"]')

def report_rows_locator(page):
    dlg = dialog_scope(page)
    # tente tbody > tr; senão, qualquer row visível
    loc = dlg.locator("tbody tr")
    return loc if loc else dlg.locator('[role="row"]')

def report_action_elements(dlg):
    """
    Retorna um locator único contendo os elementos clicáveis da coluna 'Arquivo'
    (podem ser <a> ou <button>) que tenham 'Baixar' ou 'Processa…' no texto.
    """
    return dlg.locator("button, a").filter(has_text=re.compile(r"(Baixar|Processa)", re.I))

async def open_reports_modal(page, log):
    log.write("📄 Abrindo modal “Desempenho dos estudantes”…")
    # tenta pelo botão/aba
    for sel in [
        'button:has-text("Desempenho dos estudantes")',
        'text=/Desempenho dos estudantes/i'
    ]:
        try:
            await page.locator(sel).first.click(timeout=10000)
            return
        except:
            continue
    await page.screenshot(path=str(DOWNLOAD_DIR / "debug_students.png"), full_page=True)
    (DOWNLOAD_DIR / "debug_students.html").write_text(await page.content(), encoding="utf-8")
    raise RuntimeError('Não achei "Desempenho dos estudantes". Veja downloads/debug_students.*')

async def snapshot_buttons(page) -> Tuple[int, List[str]]:
    dlg = dialog_scope(page)
    els = report_action_elements(dlg)
    try:
        count = await els.count()
    except:
        count = 0
    texts: List[str] = []
    for i in range(count):
        try:
            texts.append((await els.nth(i).inner_text()).strip())
        except:
            texts.append("")
    return count, texts

async def click_generate_new_report(page, log) -> Tuple[bool, Optional[int]]:
    """
    Clica “Gerar novo relatório” e devolve (clicked, index_do_novo_item).
    O index é do MESMO item que está "Processamento".
    """
    log.write("🧮 Clicando “Gerar novo relatório”…")
    dlg = dialog_scope(page)

    before_count, _ = await snapshot_buttons(page)

    for sel in [
        'button:has-text("Gerar novo relatório")',
        'text=/^Gerar novo relatório$/i',
        'role=button[name="Gerar novo relatório"]',
    ]:
        try:
            await page.locator(sel).first.click(timeout=5000)
            break
        except:
            continue
    else:
        log.write("ℹ️ Não encontrei “Gerar novo relatório”. Vou apenas tentar baixar o mais recente.")
        return False, None

    log.write("✅ Solicitação enviada. Aguardando o novo item (“Processamento”)…")

    # espera surgir algum "Processamento" (mesmo que a contagem NÃO mude)
    elapsed = 0
    while elapsed < 60000:
        await page.wait_for_timeout(1000)
        els = report_action_elements(dlg)
        cnt = await els.count()
        if cnt == 0:
            elapsed += 1000
            continue

        # pega o índice do *último* com "Processa" (normalmente o recém-criado)
        last_idx = None
        for i in range(cnt - 1, -1, -1):
            try:
                t = (await els.nth(i).inner_text()).strip()
                if RE_PROCESS.search(t):
                    last_idx = i
                    break
            except:
                pass

        if last_idx is not None:
            return True, last_idx

        elapsed += 1000

    log.write("⚠️ Não identifiquei o novo item no tempo esperado; seguirei em modo geral.")
    return True, None

async def wait_and_download_same_button(page, target_index: Optional[int], max_wait_sec: int, log) -> str:
    """
    Se target_index existir, ficamos nesse índice do modal até virar “Baixar”.
    Caso contrário, baixamos o primeiro “Baixar” habilitado que existir.
    """
    dlg = dialog_scope(page)

    # botão/ícone de refresh dentro do modal (se existir)
    async def click_refresh_if_available():
        for sel in [
            'button[aria-label*="Atualizar" i]',
            'button[title*="Atualizar" i]',
            'button >> nth=2'  # fallback: costuma ser o terceiro botão no cabeçalho
        ]:
            try:
                b = dlg.locator(sel).first
                if await b.is_visible():
                    await b.click(timeout=500)
                    return True
            except:
                pass
        return False

    async def try_download(el) -> Optional[str]:
        try:
            txt = (await el.inner_text()).strip()
        except:
            txt = ""
        enabled = False
        try:
            enabled = await el.is_enabled()
        except:
            pass

        if RE_DOWNLOAD.search(txt) and enabled:
            download = await page.expect_download(timeout=60000)
            await el.click()
            suggested = download.suggested_filename or "relatorio_teachlr.xlsx"
            final = DOWNLOAD_DIR / suggested
            await download.save_as(str(final))
            return str(final)
        return None

    poll = 2000
    elapsed = 0

    # garante que o modal tenha, ao menos uma vez, renderizado a coluna "Arquivo"
    try:
        await report_action_elements(dlg).first.wait_for(timeout=20000)
    except PWTimeout:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_modal_no_buttons.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_modal_no_buttons.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Não encontrei a coluna 'Arquivo' no modal. Veja downloads/debug_modal_no_buttons.*")

    while elapsed < max_wait_sec * 1000:
        els = report_action_elements(dlg)
        cnt = await els.count()

        if cnt:
            # se temos índice-alvo, tenta nele primeiro
            if target_index is not None and target_index < cnt:
                got = await try_download(els.nth(target_index))
                if got:
                    log.write("✅ Baixei o relatório do MESMO item que estava em Processamento.")
                    return got

            # fallback: qualquer “Baixar” habilitado
            for i in range(cnt):
                got = await try_download(els.nth(i))
                if got:
                    log.write("✅ Baixei o relatório disponível mais recente/habilitado.")
                    return got

        # ainda processando — dá um refresh a cada ~10s
        if (elapsed // 10000) != ((elapsed + poll) // 10000):
            await click_refresh_if_available()

        await page.wait_for_timeout(poll)
        elapsed += poll

    await page.screenshot(path=str(DOWNLOAD_DIR / "debug_report_wait_timeout.png"), full_page=True)
    (DOWNLOAD_DIR / "debug_report_wait_timeout.html").write_text(await page.content(), encoding="utf-8")
    raise RuntimeError("Tempo esgotado aguardando o relatório ficar pronto. Veja downloads/debug_report_wait_timeout.*")

# ---------------------- Fluxo principal ----------------------
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
            viewport={"width": 1440, "height": 900},
        )
        context.set_default_timeout(25000)
        context.set_default_navigation_timeout(35000)

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

            if "/#dashboard/" not in page.url:
                log.write("↩️ Voltando para a aba *Estudantes*…")
                await page.goto(students_url, wait_until="domcontentloaded")
                try: await page.wait_for_load_state("networkidle", timeout=8000)
                except: pass

            await open_reports_modal(page, log)

            clicked_generate = False
            target_index = None
            if force_generate:
                clicked_generate, target_index = await click_generate_new_report(page, log)

            max_wait = 480 if clicked_generate else 180
            saved = await wait_and_download_same_button(page, target_index, max_wait_sec=max_wait, log=log)

            try:
                await context.storage_state(path=str(STATE_PATH))
            except: pass

            return saved

        finally:
            await context.close()
            await browser.close()

# ---------------------- UI ----------------------
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

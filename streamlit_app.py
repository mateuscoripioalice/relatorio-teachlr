# streamlit_app.py
import asyncio
import os
import re
from pathlib import Path
from typing import Optional

import streamlit as st
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Request

# -----------------------------
# Config & helpers
# -----------------------------
st.set_page_config(page_title="Teachlr | Relatório de Estudantes", page_icon="📄")

BASE_APP = "https://alice.teachlr.com/"
DEFAULT_STUDENTS_URL = (
    "https://alice.teachlr.com/#dashboard/instructor/skip-level-meeting-pitayas-"
    "navegs-nurses-physicians-e-sales/students"
)

# Em produção, coloque em st.secrets:
LOGIN = st.secrets.get("TEACHLR_EMAIL", os.getenv("TEACHLR_EMAIL", ""))
PASSWORD = st.secrets.get("TEACHLR_PASSWORD", os.getenv("TEACHLR_PASSWORD", ""))

DOWNLOAD_DIR = Path("./downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

BLOCK_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".mp4", ".webm", ".woff", ".woff2", ".ttf", ".otf", ".eot"
)

def should_block(req: Request) -> bool:
    url = req.url.lower()
    # Bloqueia apenas mídia/peso. Não bloqueie JS/CSS em login.
    return any(url.endswith(ext) for ext in BLOCK_EXT)

def info(msg: str):
    st.write(f"✅ {msg}")

def warn(msg: str):
    st.warning(msg)

def err(msg: str):
    st.error(msg)

def show_debug():
    dbg_files = sorted(DOWNLOAD_DIR.glob("debug_*"))
    if not dbg_files:
        return
    st.caption("Arquivos de debug gerados:")
    for p in dbg_files:
        if p.suffix == ".png":
            st.image(str(p), caption=p.name)
        else:
            with open(p, "rb") as f:
                st.download_button(f"Baixar {p.name}", f, file_name=p.name)

# -----------------------------
# Login apenas se necessário
# -----------------------------
async def login_if_needed(page, email: str, password: str, out_dir: Path) -> None:
    """
    Faz login APENAS se a página atual for /#signin ou se detectar o formulário de login.
    """
    on_signin = bool(re.search(r"/#signin", page.url))
    if not on_signin:
        # Talvez o hash não revele, detecta inputs de login
        try:
            await page.locator(
                'input[type="password"], input[type="email"], '
                'input[placeholder*="senha" i], input[placeholder*="mail" i]'
            ).first.wait_for(timeout=1200)
            on_signin = True
        except PWTimeout:
            return  # já logado

    if not on_signin:
        return

    # Procura campos (page + iframes)
    async def find(selectors):
        ctxs = [page, *page.frames]
        for ctx in ctxs:
            for sel in selectors:
                try:
                    loc = ctx.locator(sel).first
                    await loc.wait_for(timeout=1500)
                    return loc
                except:
                    continue
        return None

    email_loc = await find([
        'input[placeholder*="e-mail" i]', 'input[placeholder*="email" i]',
        'input[name="email"]', 'input[name="username"]',
        'input[type="email"]', '#email'
    ])
    senha_loc = await find([
        'input[placeholder*="senha" i]', 'input[type="password"]',
        'input[name="password"]', '#password'
    ])

    if not email_loc or not senha_loc:
        # artefatos para diagnóstico
        out_dir.mkdir(exist_ok=True)
        await page.screenshot(path=str(out_dir / "debug_login.png"), full_page=True)
        (out_dir / "debug_login.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError('Não achei campos de login. Veja downloads/debug_login.*')

    await email_loc.click(); await email_loc.fill(email)
    await senha_loc.click(); await senha_loc.fill(password)

    # Tenta clicar os botões habituais:
    clicked = False
    for bsel in [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Entrar")',
        'text=/^Login$/',
        'text=/^Entrar$/',
    ]:
        try:
            await page.locator(bsel).first.click(timeout=900)
            clicked = True
            break
        except:
            pass
    if not clicked:
        try:
            await senha_loc.press("Enter")
        except:
            pass

    await page.wait_for_load_state("domcontentloaded")

# -----------------------------
# Relatórios: gerar/baixar
# -----------------------------
async def open_reports_modal(page) -> None:
    """
    Abre o modal 'Relatórios' a partir da página de estudantes do curso.
    """
    # Botão principal “Desempenho dos estudantes”
    try:
        btn = page.get_by_role("button", name=re.compile(r"Desempenho dos estudantes", re.I))
        await btn.first.wait_for(timeout=6000)
        await btn.first.click()
        return
    except:
        pass

    # Fallback por texto
    try:
        await page.get_by_text(re.compile(r"Desempenho dos estudantes", re.I)).first.click(timeout=4000)
        return
    except:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_students.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_students.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError('Não achei "Desempenho dos estudantes". Veja downloads/debug_students.*')

async def click_generate_new_report(page) -> None:
    """
    No modal aberto, clica 'Gerar novo relatório'.
    """
    # Botão grande do modal
    for sel in [
        'button:has-text("Gerar novo relatório")',
        'text=/^Gerar novo relatório$/i',
        'role=button[name="Gerar novo relatório"]',
    ]:
        try:
            await page.locator(sel).first.click(timeout=3000)
            return
        except:
            continue
    # Se não achou, só siga — talvez já exista relatório pronto
    return

async def wait_and_download_latest(page, max_wait_sec: int = 180) -> str:
    """
    Espera aparecer/ficar pronto um item com 'Baixar' e baixa.
    Retorna o caminho do arquivo salvo.
    """
    # Espera algum 'Baixar' ficar visível/ativo
    try:
        await page.get_by_role("button", name=re.compile(r"Baixar", re.I)).first.wait_for(timeout=max_wait_sec * 1000)
    except PWTimeout:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_modal_timeout.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_modal_timeout.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Demorou demais esperando 'Baixar' no modal. Veja downloads/debug_modal_timeout.*")

    # Baixar o mais recente (normalmente o primeiro da lista)
    dl_button = page.get_by_role("button", name=re.compile(r"Baixar", re.I)).first

    download = await page.expect_download(timeout=60000)
    await dl_button.click()
    path = await download.path()
    # nome final
    suggested = download.suggested_filename or "relatorio_teachlr.xlsx"
    final_path = DOWNLOAD_DIR / suggested
    # alguns providers só dão stream — garanta a cópia
    await download.save_as(str(final_path))
    return str(final_path)

# -----------------------------
# Fluxo principal
# -----------------------------
async def run_flow(students_url: str, force_generate: bool, email: str, password: str) -> str:
    """
    Abre a aba de Estudantes do curso, loga se necessário, abre o modal,
    (opcional) gera novo relatório, e baixa o arquivo.
    """
    state_path = Path("/tmp/state.json")  # reaproveitar sessão entre execuções

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
            ],
        )
        context = await browser.new_context(
            accept_downloads=True,
            storage_state=str(state_path) if state_path.exists() else None,
            viewport={"width": 1366, "height": 900},
        )

        async def router(route, request):
            try:
                if should_block(request):
                    await route.abort()
                else:
                    await route.continue_()
            except:
                try:
                    await route.continue_()
                except:
                    pass

        await context.route("**/*", router)
        page = await context.new_page()

        try:
            # 1) Vai direto para a aba Estudantes
            await page.goto(students_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=6000)
            except:
                pass

            # 2) Se caiu em #signin, faz login UMA vez
            await login_if_needed(page, email, password, DOWNLOAD_DIR)

            # 3) Volta/garante a URL de Estudantes
            if not re.search(r"/#dashboard/.*/students", page.url):
                await page.goto(students_url, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=6000)
                except:
                    pass

            # 4) Abre modal de Relatórios
            await open_reports_modal(page)

            # 5) Gera novo relatório, se solicitado
            if force_generate:
                await click_generate_new_report(page)

            # 6) Espera e baixa o mais recente
            saved = await wait_and_download_latest(page, max_wait_sec=180)

            # 7) Salva storage_state para próximas execuções mais rápidas
            try:
                await context.storage_state(path=str(state_path))
            except:
                pass

            return saved

        finally:
            await context.close()
            await browser.close()

# -----------------------------
# UI
# -----------------------------
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
        err("Informe a URL de *Estudantes* do curso.")
    elif not email or not password:
        err("Informe e-mail e senha.")
    else:
        st.session_state["busy"] = True
        st.info("Iniciando… (isso pode levar alguns segundos na primeira execução)")
        try:
            path = asyncio.run(run_flow(students_url.strip(), force, email.strip(), password))
            info("Relatório pronto!")
            with open(path, "rb") as f:
                st.download_button("⬇️ Baixar arquivo", f, file_name=Path(path).name, mime="application/octet-stream")
        except Exception as e:
            err(str(e))
            show_debug()
        finally:
            st.session_state["busy"] = False

# Rodapé de debug sempre que houver arquivos
if any(DOWNLOAD_DIR.glob("debug_*")):
    with st.expander("🔍 Ver artefatos de debug"):
        show_debug()

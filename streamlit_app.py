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

def save_dom_dump(page, png_name: str, html_name: str):
    try:
        page.screenshot(path=str(DOWNLOAD_DIR / png_name), full_page=True)
    except Exception:
        pass
    try:
        (DOWNLOAD_DIR / html_name).write_text(asyncio.get_event_loop().run_until_complete(page.content()), encoding="utf-8")
    except Exception:
        pass

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

# ---------------------- Ações da UI ----------------------
async def open_reports_modal(page, log):
    log.write("📄 Abrindo modal “Desempenho dos estudantes”…")
    try:
        btn = page.get_by_role("button", name=re.compile(r"Desempenho dos estudantes", re.I))
        await btn.first.wait_for(timeout=10000)
        await btn.first.click()
        return
    except:
        pass
    try:
        await page.get_by_text(re.compile(r"Desempenho dos estudantes", re.I)).first.click(timeout=8000)
        return
    except:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_students.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_students.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError('Não achei "Desempenho dos estudantes". Veja downloads/debug_students.*')

def dialog_scope(page):
    # tenta achar o dialog pelo título "Relatórios"
    try:
        dlg = page.get_by_role("dialog", name=re.compile(r"Relat[oó]rios", re.I))
        return dlg
    except:
        return page.locator('div[role="dialog"]')

def report_rows_locator(page):
    # linhas do grid dentro do modal
    dlg = dialog_scope(page)
    # tenta tbody > tr; se não houver, pega os "rows" visíveis
    loc = dlg.locator("tbody tr")
    return loc if loc else dlg.locator('[role="row"]')

def report_action_buttons_locator(page):
    # botões na coluna Arquivo (Processando / Baixar)
    dlg = dialog_scope(page)
    return dlg.get_by_role("button", name=re.compile(r"(Processando|Baixar)", re.I))

async def snapshot_buttons(page) -> Tuple[int, List[str]]:
    btns = report_action_buttons_locator(page)
    try:
        count = await btns.count()
    except:
        count = 0
    texts: List[str] = []
    for i in range(count):
        try:
            texts.append((await btns.nth(i).inner_text()).strip())
        except:
            texts.append("")
    return count, texts

async def click_generate_new_report(page, log) -> Tuple[bool, Optional[int]]:
    """
    Clica em "Gerar novo relatório" e retorna:
    (clicked, index_btn_gerado)
    - clicked: True se conseguiu clicar
    - index_btn_gerado: índice do novo botão/linha (para segui-lo)
    """
    log.write("🧮 Clicando “Gerar novo relatório”…")

    # snapshot antes: quantidade e textos dos botões
    before_count, before_texts = await snapshot_buttons(page)

    for sel in [
        'button:has-text("Gerar novo relatório")',
        'text=/^Gerar novo relatório$/i',
        'role=button[name="Gerar novo relatório"]',
    ]:
        try:
            await page.locator(sel).first.click(timeout=5000)
            log.write("✅ Solicitação enviada. Aguardando o novo item aparecer…")
            break
        except:
            continue
    else:
        log.write("ℹ️ Não encontrei “Gerar novo relatório”. Tentarei baixar um existente.")
        return False, None

    # Aguarda surgir um novo botão "Processando" (ou aumentar a contagem)
    # Damos até 60s para a linha aparecer
    elapsed = 0
    while elapsed < 60000:
        await page.wait_for_timeout(1000)
        after_count, after_texts = await snapshot_buttons(page)
        if after_count > before_count:
            # novo índice é o último (assumindo que entra no final)
            return True, after_count - 1

        # Mesmo count, mas apareceu um "Processando" que não havia antes
        if "Processando" in [t.capitalize() for t in after_texts] and after_texts != before_texts:
            # pega o índice do "Processando" mais à direita/embaixo
            idx = None
            for i in range(len(after_texts) - 1, -1, -1):
                if re.search(r"Processando", after_texts[i], re.I):
                    idx = i; break
            if idx is not None:
                return True, idx

        elapsed += 1000

    # Não apareceu; vamos tentar mesmo assim baixar o mais recente que estiver habilitado
    log.write("⚠️ Novo item não apareceu no tempo esperado; vou tentar baixar o mais recente disponível.")
    return True, None

async def wait_and_download_same_button(page, target_index: Optional[int], max_wait_sec: int, log) -> str:
    """
    Se target_index for fornecido, seguimos *aquele* botão específico
    (o que estava em "Processando"). Esperamos virar "Baixar" e habilitar.
    Caso contrário, baixamos o *primeiro habilitado* (fallback).
    """
    log.write("⏳ Aguardando relatório ficar pronto…")

    # helper: refresh do modal (ícone de setas)
    async def click_refresh_if_available():
        dlg = dialog_scope(page)
        for sel in [
            'button[aria-label*="Atualizar" i]',
            'button[title*="Atualizar" i]',
        ]:
            try:
                btn = dlg.locator(sel).first
                if await btn.is_visible():
                    await btn.click(timeout=1000)
                    return True
            except:
                pass
        return False

    poll_ms = 2000
    elapsed = 0

    btns = report_action_buttons_locator(page)
    # garante que haja pelo menos algum botão na tela
    try:
        await btns.first.wait_for(timeout=20000)
    except PWTimeout:
        await page.screenshot(path=str(DOWNLOAD_DIR / "debug_modal_no_buttons.png"), full_page=True)
        (DOWNLOAD_DIR / "debug_modal_no_buttons.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Não encontrei botões de ação no modal. Veja downloads/debug_modal_no_buttons.*")

    while elapsed < max_wait_sec * 1000:
        try:
            count = await btns.count()
            if count == 0:
                # lista vazia? tenta refresh
                if (elapsed // 10000) != ((elapsed + poll_ms) // 10000):
                    await click_refresh_if_available()
            else:
                target_i = target_index if (target_index is not None and target_index < count) else 0
                target = btns.nth(target_i)

                # lê o texto e estado
                txt = ""
                try:
                    txt = (await target.inner_text()).strip()
                except:
                    pass
                enabled = False
                try:
                    enabled = await target.is_enabled()
                except:
                    pass

                # se já está "Baixar" e habilitado, baixa!
                if re.search(r"Baixar", txt, re.I) and enabled:
                    log.write("⬇️ Iniciando download do item gerado…")
                    download = await page.expect_download(timeout=60000)
                    await target.click()
                    suggested = download.suggested_filename or "relatorio_teachlr.xlsx"
                    final_path = DOWNLOAD_DIR / suggested
                    await download.save_as(str(final_path))
                    log.write(f"✅ Download salvo: {final_path.name}")
                    return str(final_path)

                # se ainda "Processando", só espera/pinga refresh às vezes
                if re.search(r"Processando", txt, re.I) or not enabled:
                    if (elapsed // 10000) != ((elapsed + poll_ms) // 10000):
                        await click_refresh_if_available()
                else:
                    # fallback: se não conseguimos identificar o mesmo,
                    # tenta encontrar o primeiro "Baixar" habilitado
                    for i in range(count):
                        b = btns.nth(i)
                        try:
                            b_txt = (await b.inner_text()).strip()
                            if re.search(r"Baixar", b_txt, re.I) and await b.is_enabled():
                                log.write("⬇️ Iniciando download do mais recente habilitado…")
                                download = await page.expect_download(timeout=60000)
                                await b.click()
                                suggested = download.suggested_filename or "relatorio_teachlr.xlsx"
                                final_path = DOWNLOAD_DIR / suggested
                                await download.save_as(str(final_path))
                                log.write(f"✅ Download salvo: {final_path.name}")
                                return str(final_path)
                        except:
                            continue

        except Exception:
            pass

        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms

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
            viewport={"width": 1366, "height": 900},
        )
        context.set_default_timeout(20000)
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

            clicked_generate = False
            target_index = None
            if force_generate:
                clicked_generate, target_index = await click_generate_new_report(page, log)

            # Se gerou agora, dá mais tempo (até 8 min). Senão, 2 min.
            max_wait = 480 if clicked_generate else 120
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

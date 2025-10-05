# main/streamlit_app.py
import os, re, sys, time, subprocess, asyncio
from pathlib import Path

import streamlit as st
import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# =========================
# Config & Segredos
# =========================
st.set_page_config(page_title="Relatório Teachlr", page_icon="📊")
st.title("📊 Relatório de Desempenho — Teachlr")

DOMAIN   = os.getenv("TEACHLR_DOMAIN")   or st.secrets.get("TEACHLR_DOMAIN", "")
API_KEY  = os.getenv("TEACHLR_API_KEY")  or st.secrets.get("TEACHLR_API_KEY", "")
LOGIN    = os.getenv("TEACHLR_EMAIL")    or st.secrets.get("TEACHLR_EMAIL", "")
PASSWORD = os.getenv("TEACHLR_PASSWORD") or st.secrets.get("TEACHLR_PASSWORD", "")

if not all([DOMAIN, API_KEY, LOGIN, PASSWORD]):
    st.warning("Configure os *Secrets* do app: TEACHLR_DOMAIN, TEACHLR_API_KEY, TEACHLR_EMAIL, TEACHLR_PASSWORD.")
    st.stop()

BASE_API = f"https://api.teachlr.com/{DOMAIN}/api"
BASE_APP = f"https://{DOMAIN}.teachlr.com/"
HEADERS  = {"Content-Type": "application/json", "Authorization": API_KEY}

# Instala Chromium de forma idempotente (útil em restarts)
try:
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
except Exception as e:
    st.info(f"Aviso ao preparar Chromium: {e}")

# =========================
# Helpers
# =========================
def search_courses(query: str, limit: int = 50):
    """Busca cursos por título (para referência)"""
    url = f"{BASE_API}/courses?paginate=true&limit={limit}&search={query}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data.get("data", data)
    return [{"id": int(c.get("id")), "title": c.get("title") or "(sem título)"} for c in items]

async def robust_login_hash(page, base_app: str, email: str, password: str, downloads_dir: Path) -> None:
    """Login em instância Teachlr SPA (rotas com #), usando placeholders PT-BR."""
    await page.goto(f"{base_app}#signin", wait_until="domcontentloaded")

    # Fecha banners de cookies comuns (se houver)
    for sel in [
        'button:has-text("Aceitar")',
        'button:has-text("Accept")',
        'button[aria-label="accept"]',
        '[data-testid="cookie-banner"] button',
    ]:
        try:
            await page.locator(sel).click(timeout=800)
        except:
            pass

    # Em alguns casos, já vem logado e redireciona; se aparecer dashboard, pula
    try:
        await page.wait_for_url(re.compile(r".*/#dashboard/.*"), timeout=3000)
        return
    except:
        pass

    # Localizadores dos inputs (variações)
    email_locators = [
        page.get_by_placeholder("Usuário ou e-mail"),
        page.locator('input[placeholder*="e-mail" i]'),
        page.locator('input[type="email"]'),
        page.locator('#email'),
    ]
    pwd_locators = [
        page.get_by_placeholder("Senha"),
        page.locator('input[placeholder*="senha" i]'),
        page.locator('input[type="password"]'),
        page.locator('#password'),
    ]

    # Aguarda até 30s pelo campo de e-mail e preenche
    email_found = False
    for loc in email_locators:
        try:
            await loc.wait_for(timeout=30000)
            await loc.fill(email)
            email_found = True
            break
        except:
            continue
    if not email_found:
        png = downloads_dir / "debug_login.png"
        html = downloads_dir / "debug_login.html"
        await page.screenshot(path=str(png), full_page=True)
        html.write_text(await page.content(), encoding="utf-8")
        raise RuntimeError('Não achei o campo "Usuário ou e-mail" na tela de login (artefatos: debug_login.*).')

    pwd_found = False
    for loc in pwd_locators:
        try:
            await loc.fill(password)
            pwd_found = True
            break
        except:
            continue
    if not pwd_found:
        raise RuntimeError('Não achei o campo "Senha" na tela de login.')

    # Botão Login (variações)
    for bsel in ['button:has-text("Login")', 'button[type="submit"]', 'button:has-text("Entrar")']:
        try:
            await page.locator(bsel).first.click(timeout=3000)
            break
        except:
            pass

    # Espera a SPA autenticar
    try:
        await page.wait_for_url(re.compile(r".*/#dashboard/.*"), timeout=15000)
    except:
        # fallback: espera elementos típicos logado
        await page.wait_for_selector('a[href*="#dashboard/"], a:has-text("Cursos")', timeout=8000)

async def generate_and_download_report_from_students_url(students_url: str, max_wait_sec: int = 300) -> str:
    """
    Abre a URL da aba de 'Estudantes' do curso, clica em 'Desempenho dos estudantes',
    gera um novo relatório (se necessário) e baixa o arquivo. Retorna o caminho salvo.
    """
    downloads_dir = Path("./downloads"); downloads_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        # Se quiser persistir sessão durante o uptime do app, descomente as duas linhas abaixo
        # state_path = "state.json"
        # context = await p.chromium.launch_persistent_context(user_data_dir="/tmp/ud", headless=True, accept_downloads=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # Login
        await robust_login_hash(page, BASE_APP, LOGIN, PASSWORD, downloads_dir)

        # Navega direto para a aba "Estudantes" do curso (rota hash)
        await page.goto(students_url, wait_until="domcontentloaded")

        # Garante que o botão está visível
        perf_btn = 'button:has-text("Desempenho dos estudantes"), text=/Desempenho dos estudantes/i'
        try:
            await page.wait_for_selector(perf_btn, timeout=15000)
        except:
            # clica na tab Estudantes se necessário
            try:
                await page.click('text=/^Estudantes$/', timeout=3000)
            except:
                pass
            await page.wait_for_selector(perf_btn, timeout=15000)

        # Abre o modal de relatórios
        await page.click(perf_btn)
        dialog = page.locator('[role="dialog"], .modal, .v-dialog').first

        # "Gerar novo relatório" se houver
        try:
            await dialog.get_by_role("button", name=re.compile(r"Gerar novo relatório", re.I)).click(timeout=3500)
        except PWTimeout:
            pass  # já existe um relatório pronto/pendente

        # Loop até aparecer "Baixar"
        start = time.time()
        download_path = None
        while time.time() - start < max_wait_sec:
            # 'Atualizar' se tiver um botão/ícone de refresh no modal
            try:
                await dialog.locator('button[title*="Atualizar"], button:has(svg)').first.click(timeout=1200)
            except:
                pass

            try:
                btn_download = dialog.get_by_role("button", name=re.compile(r"Baixar", re.I))
                await btn_download.wait_for(timeout=4000)
                async with page.expect_download(timeout=30000) as dl_info:
                    await btn_download.click()
                download = await dl_info.value

                suggested = download.suggested_filename or "relatorio.csv"
                ts = int(time.time())
                if "." in suggested:
                    fname = re.sub(r"(\.[a-zA-Z0-9]+)$", fr"_{ts}\1", suggested)
                else:
                    fname = f"{suggested}_{ts}.csv"
                download_path = str(downloads_dir / fname)
                await download.save_as(download_path)
                break
            except PWTimeout:
                await page.wait_for_timeout(2500)

        await context.close()
        await browser.close()

        if not download_path:
            raise RuntimeError("Tempo máximo atingido sem aparecer 'Baixar' no modal.")
        return download_path

# =========================
# UI
# =========================
with st.sidebar:
    st.subheader("Credenciais")
    st.write("DOMAIN:", "✅" if DOMAIN else "❌")
    st.write("API KEY:", "✅" if API_KEY else "❌")
    st.write("LOGIN:",   "✅" if LOGIN   else "❌")
    st.write("SENHA:",   "✅" if PASSWORD else "❌")

st.markdown("**Fluxo recomendado:** cole a **URL da aba Estudantes** do curso (ex.: `https://alice.teachlr.com/#dashboard/instructor/<slug>/students`) e clique em **Gerar relatório**.")

with st.expander("🔎 Buscar cursos por título (API) — opcional, só para referência", expanded=False):
    q = st.text_input("Buscar por:", value="Onboarding Alice")
    limit = st.number_input("Limite", min_value=1, max_value=200, value=50, step=1)
    if st.button("Buscar cursos"):
        try:
            res = search_courses(q, limit=limit)
            st.json(res)
            st.caption("Use a lista acima apenas como referência. A URL final usa o *slug* do curso (na plataforma).")
        except Exception as e:
            st.error(f"Erro na API de cursos: {e}")

st.divider()
students_url = st.text_input(
    "URL da aba **Estudantes** do curso (cole aqui):",
    value="https://alice.teachlr.com/#dashboard/instructor/skip-level-meeting-pitayas-navegs-nurses-physicians-e-sales/students",
    help="Abra o curso na plataforma, vá em Estudantes e copie a URL do navegador."
)

col1, col2 = st.columns([1,1])
with col1:
    run = st.button("🚀 Gerar relatório e baixar")
with col2:
    dry = st.button("✅ Só testar login")

if dry:
    with st.status("Testando login…", expanded=True) as status:
        try:
            async def _test():
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                    context = await browser.new_context()
                    page = await context.new_page()
                    await robust_login_hash(page, BASE_APP, LOGIN, PASSWORD, Path("./downloads"))
                    await context.close(); await browser.close()
            asyncio.run(_test())
            status.update(label="Login OK!", state="complete")
        except Exception as e:
            status.update(label="Falhou 😢", state="error")
            st.error(str(e))
            # Mostra artefatos de debug (se existirem)
            dbg_png = Path("./downloads/debug_login.png")
            dbg_html = Path("./downloads/debug_login.html")
            if dbg_png.exists():
                st.image(str(dbg_png), caption="debug_login.png", use_container_width=True)
            if dbg_html.exists():
                with open(dbg_html, "rb") as f:
                    st.download_button("Baixar debug_login.html", f, file_name="debug_login.html")

if run:
    if not students_url.strip():
        st.error("Cole a URL da aba Estudantes do curso.")
    else:
        with st.status("Gerando relatório no Teachlr…", expanded=True) as status:
            try:
                path = asyncio.run(generate_and_download_report_from_students_url(students_url))
                status.update(label="Relatório pronto!", state="complete")
                with open(path, "rb") as f:
                    st.download_button("⬇️ Baixar arquivo", f, file_name=Path(path).name)
            except Exception as e:
                status.update(label="Falhou 😢", state="error")
                st.error(str(e))
                # Artefatos de debug (se existirem)
                dbg_png = Path("./downloads/debug_login.png")
                dbg_html = Path("./downloads/debug_login.html")
                if dbg_png.exists():
                    st.image(str(dbg_png), caption="debug_login.png", use_container_width=True)
                if dbg_html.exists():
                    with open(dbg_html, "rb") as f:
                        st.download_button("Baixar debug_login.html", f, file_name="debug_login.html")

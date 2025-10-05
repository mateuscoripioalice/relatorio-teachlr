# main/streamlit_app.py
import os, re, sys, time, subprocess, asyncio
from pathlib import Path

import streamlit as st
import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---------------------------
# Configuração da página
# ---------------------------
st.set_page_config(page_title="Relatório Teachlr", page_icon="📊")
st.title("📊 Relatório de Desempenho — Teachlr")

# Evita execuções concorrentes no Streamlit (que fecham o browser no meio)
if "busy" not in st.session_state:
    st.session_state["busy"] = False

# ---------------------------
# Segredos / Ambiente
# ---------------------------
DOMAIN   = os.getenv("TEACHLR_DOMAIN")   or st.secrets.get("TEACHLR_DOMAIN", "")
API_KEY  = os.getenv("TEACHLR_API_KEY")  or st.secrets.get("TEACHLR_API_KEY", "")
LOGIN    = os.getenv("TEACHLR_EMAIL")    or st.secrets.get("TEACHLR_EMAIL", "")
PASSWORD = os.getenv("TEACHLR_PASSWORD") or st.secrets.get("TEACHLR_PASSWORD", "")

if not all([DOMAIN, API_KEY, LOGIN, PASSWORD]):
    st.warning("Configure os *Secrets*: TEACHLR_DOMAIN, TEACHLR_API_KEY, TEACHLR_EMAIL, TEACHLR_PASSWORD.")
    st.stop()

BASE_API = f"https://api.teachlr.com/{DOMAIN}/api"
BASE_APP = f"https://{DOMAIN}.teachlr.com/"
HEADERS  = {"Content-Type": "application/json", "Authorization": API_KEY}

# Garante Chromium instalado (idempotente)
try:
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
except Exception as e:
    st.info(f"Aviso ao preparar Chromium: {e}")

# ---------------------------
# Helpers HTTP
# ---------------------------
def search_courses(query: str, limit: int = 50):
    """Busca cursos por título (apenas referência)."""
    url = f"{BASE_API}/courses?paginate=true&limit={limit}&search={query}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data.get("data", data)
    return [{"id": int(c.get("id")), "title": c.get("title") or "(sem título)"} for c in items]

# ---------------------------
# Helper de Login (SPA hash)
# ---------------------------
async def robust_login_hash(page, base_app: str, email: str, password: str, downloads_dir: Path) -> None:
    """Login em instância Teachlr com rotas '#', usando placeholders PT-BR."""
    await page.goto(f"{base_app}#signin", wait_until="domcontentloaded")

    # Fecha banner de cookies comuns
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

    # Se já caiu no dashboard, considera logado
    try:
        await page.wait_for_url(re.compile(r".*/#dashboard/.*"), timeout=3000)
        return
    except:
        pass

    # Seletores possíveis
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

    # E-mail
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
        raise RuntimeError('Não achei o campo "Usuário ou e-mail" (veja downloads/debug_login.*).')

    # Senha
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

    # Botão de submit
    for bsel in ['button:has-text("Login")', 'button[type="submit"]', 'button:has-text("Entrar")']:
        try:
            await page.locator(bsel).first.click(timeout=3000)
            break
        except:
            pass

    # Espera autenticar
    try:
        await page.wait_for_url(re.compile(r".*/#dashboard/.*"), timeout=15000)
    except:
        await page.wait_for_selector('a[href*="#dashboard/"], a:has-text("Cursos")', timeout=8000)

# ---------------------------
# Fluxo: gerar e baixar relatório
# ---------------------------
async def generate_and_download_report_from_students_url(students_url: str, max_wait_sec: int = 300) -> str:
    """
    Abre a URL da aba 'Estudantes' do curso, clica em 'Desempenho dos estudantes',
    gera (se necessário) e baixa o arquivo. Retorna o caminho salvo.
    """
    downloads_dir = Path("./downloads"); downloads_dir.mkdir(exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        try:
            # Login
            await robust_login_hash(page, BASE_APP, LOGIN, PASSWORD, downloads_dir)

            # Navega para a aba "Estudantes"
            await page.goto(students_url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass

            # Garante que o botão "Desempenho dos estudantes" está disponível
            try:
                await page.wait_for_selector('text="Desempenho dos estudantes"', timeout=10000)
            except PWTimeout:
                try:
                    await page.click('text=/^Estudantes$/')
                    await page.wait_for_selector('text="Desempenho dos estudantes"', timeout=10000)
                except PWTimeout:
                    await page.screenshot(path="downloads/debug_students.png", full_page=True)
                    raise RuntimeError('Não achei "Desempenho dos estudantes". Veja downloads/debug_students.png')

            # Abre o modal
            await page.click('text="Desempenho dos estudantes"')

            # Função para reapontar o modal (alguns temas recriam o DOM)
            def modal():
                return page.locator('[role="dialog"], .modal, .v-dialog').first

            # "Gerar novo relatório", se houver
            try:
                await modal().get_by_role("button", name=re.compile(r"Gerar novo relatório", re.I)).click(timeout=3500)
            except PWTimeout:
                pass

            # Espera "Baixar" e baixa
            start = time.time()
            download_path = None
            while time.time() - start < max_wait_sec:
                # Botão/ícone de atualizar (se existir)
                try:
                    await modal().locator('button[title*="Atualizar"], button:has(svg)').first.click(timeout=1200)
                except:
                    pass

                try:
                    await modal().get_by_role("button", name=re.compile(r"Baixar", re.I)).wait_for(timeout=4000)
                    async with page.expect_download(timeout=30000) as dl_info:
                        await modal().get_by_role("button", name=re.compile(r"Baixar", re.I)).click()
                    download = await dl_info.value

                    suggested = download.suggested_filename or "relatorio.csv"
                    ts = int(time.time())
                    fname = (re.sub(r"(\.[a-zA-Z0-9]+)$", fr"_{ts}\1", suggested)
                             if "." in suggested else f"{suggested}_{ts}.csv")
                    download_path = str(downloads_dir / fname)
                    await download.save_as(download_path)
                    break
                except PWTimeout:
                    await page.wait_for_timeout(2000)

            if not download_path:
                await page.screenshot(path="downloads/debug_wait_baixar.png", full_page=True)
                raise RuntimeError("Não apareceu o botão 'Baixar' a tempo. Veja downloads/debug_wait_baixar.png")

            return download_path
        finally:
            # Fecha somente no final (evita "Target page/context/browser has been closed")
            await context.close()
            await browser.close()

# ---------------------------
# UI
# ---------------------------
with st.sidebar:
    st.subheader("Credenciais")
    st.write("DOMAIN:", "✅" if DOMAIN else "❌")
    st.write("API KEY:", "✅" if API_KEY else "❌")
    st.write("LOGIN:",   "✅" if LOGIN   else "❌")
    st.write("SENHA:",   "✅" if PASSWORD else "❌")

st.markdown(
    "Cole a **URL da aba Estudantes** do curso (ex.: "
    "`https://alice.teachlr.com/#dashboard/instructor/<slug>/students`) "
    "e clique em **Gerar relatório**."
)

with st.expander("🔎 Buscar cursos por título (API) — opcional", expanded=False):
    q = st.text_input("Buscar por:", value="Onboarding Alice")
    limit = st.number_input("Limite", min_value=1, max_value=200, value=50, step=1)
    if st.button("Buscar cursos"):
        try:
            res = search_courses(q, limit=limit)
            st.json(res)
            st.caption("Use apenas como referência. A URL final usa o *slug* do curso na plataforma.")
        except Exception as e:
            st.error(f"Erro na API de cursos: {e}")

st.divider()

students_url = st.text_input(
    "URL da aba **Estudantes** do curso:",
    value="https://alice.teachlr.com/#dashboard/instructor/skip-level-meeting-pitayas-navegs-nurses-physicians-e-sales/students",
    help="Abra o curso na plataforma, vá em Estudantes e copie a URL do navegador."
)

col1, col2 = st.columns([1,1])
with col1:
    run = st.button("🚀 Gerar relatório e baixar")
with col2:
    dry = st.button("✅ Só testar login")

if dry:
    if st.session_state["busy"]:
        st.info("Já existe uma execução em andamento… aguarde terminar.")
    else:
        st.session_state["busy"] = True
        try:
            with st.status("Testando login…", expanded=True) as status:
                async def _test():
                    async with async_playwright() as p:
                        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                        context = await browser.new_context()
                        page = await context.new_page()
                        try:
                            await robust_login_hash(page, BASE_APP, LOGIN, PASSWORD, Path("./downloads"))
                        finally:
                            await context.close(); await browser.close()
                asyncio.run(_test())
                status.update(label="Login OK!", state="complete")
        except Exception as e:
            st.error(str(e))
            for dbg in ["downloads/debug_login.png", "downloads/debug_login.html"]:
                p = Path(dbg)
                if p.exists():
                    if p.suffix == ".png":
                        st.image(str(p), caption=p.name, use_container_width=True)
                    else:
                        with open(p, "rb") as f:
                            st.download_button(f"Baixar {p.name}", f, file_name=p.name)
        finally:
            st.session_state["busy"] = False

if run:
    if not students_url.strip():
        st.error("Cole a URL da aba Estudantes do curso.")
    elif st.session_state["busy"]:
        st.info("Já existe uma execução em andamento… aguarde terminar.")
    else:
        st.session_state["busy"] = True
        try:
            with st.status("Gerando relatório no Teachlr…", expanded=True) as status:
                path = asyncio.run(generate_and_download_report_from_students_url(students_url))
                status.update(label="Relatório pronto!", state="complete")
                with open(path, "rb") as f:
                    st.download_button("⬇️ Baixar arquivo", f, file_name=Path(path).name)
        except Exception as e:
            st.error(str(e))
            # Mostra artefatos de debug, se existirem
            for dbg in [
                "downloads/debug_students.png",
                "downloads/debug_wait_baixar.png",
                "downloads/debug_login.png",
                "downloads/debug_login.html",
            ]:
                p = Path(dbg)
                if p.exists():
                    if p.suffix == ".png":
                        st.image(str(p), caption=p.name, use_container_width=True)
                    else:
                        with open(p, "rb") as f:
                            st.download_button(f"Baixar {p.name}", f, file_name=p.name)
        finally:
            st.session_state["busy"] = False

import os, time, re, asyncio, sys, subprocess
from pathlib import Path

import streamlit as st
import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ---------- Config ----------
st.set_page_config(page_title="Relatório Teachlr", page_icon="📊")
st.title("📊 Relatório de Desempenho — Teachlr")

DOMAIN   = os.getenv("TEACHLR_DOMAIN")   or st.secrets.get("TEACHLR_DOMAIN", "")
API_KEY  = os.getenv("TEACHLR_API_KEY")  or st.secrets.get("TEACHLR_API_KEY", "")
LOGIN    = os.getenv("TEACHLR_EMAIL")    or st.secrets.get("TEACHLR_EMAIL", "")
PASSWORD = os.getenv("TEACHLR_PASSWORD") or st.secrets.get("TEACHLR_PASSWORD", "")

BASE_API = f"https://api.teachlr.com/{DOMAIN}/api"
BASE_APP = f"https://{DOMAIN}.teachlr.com/"
HEADERS  = {"Content-Type": "application/json", "Authorization": API_KEY}

# instala Chromium só quando a página abre (idempotente)
try:
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
except Exception as e:
    st.warning(f"Aviso ao preparar Chromium: {e}")

# ---------- helpers ----------
def search_courses(q: str, limit=50):
    url = f"{BASE_API}/courses?paginate=true&limit={limit}&search={q}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data.get("data", data)
    return [{"id": int(c.get("id")), "title": c.get("title") or "(sem título)"} for c in items]

async def generate_and_download_report(course_id: int, max_wait_sec: int = 300) -> str:
    """
    Login -> curso -> 'Desempenho dos estudantes' -> 'Gerar novo relatório' -> aguarda 'Baixar'
    Salva o arquivo em ./downloads e retorna o caminho.
    """
    downloads_dir = Path("./downloads"); downloads_dir.mkdir(exist_ok=True)
    course_url = f"{BASE_APP}courses/{course_id}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # login
        await page.goto(f"{BASE_APP}login", wait_until="domcontentloaded")
        await page.fill('input[name="email"], input[type="email"]', LOGIN)
        await page.fill('input[name="password"], input[type="password"]', PASSWORD)
        await page.click('button:has-text("Entrar"), button[type="submit"]')
        await page.wait_for_load_state("networkidle")

        # curso
        await page.goto(course_url, wait_until="domcontentloaded")
        try:
            await page.click('text=/^Estudantes$/', timeout=2500)
        except:
            pass  # em alguns temas não precisa

        # botão 'Desempenho dos estudantes'
        perf_btn = 'button:has-text("Desempenho dos estudantes"), text=/Desempenho dos estudantes/i'
        await page.wait_for_selector(perf_btn, timeout=15000)
        await page.click(perf_btn)

        # modal
        dialog = page.locator('[role="dialog"], .modal, .v-dialog').first

        # gerar novo relatório (se existir)
        try:
            await dialog.get_by_role("button", name=re.compile(r"Gerar novo relatório", re.I)).click(timeout=4000)
        except PWTimeout:
            pass

        # esperar aparecer Baixar (faz refresh no modal se tiver ícone/botão)
        start = time.time()
        download_path = None
        while time.time() - start < max_wait_sec:
            # refresh (opcional)
            try:
                await dialog.locator('button[title*="Atualizar"], button:has(svg)').first.click(timeout=1500)
            except:
                pass

            try:
                btn_download = dialog.get_by_role("button", name=re.compile(r"Baixar", re.I))
                await btn_download.wait_for(timeout=3000)
                async with page.expect_download(timeout=30000) as dl_info:
                    await btn_download.click()
                download = await dl_info.value

                suggested = download.suggested_filename or f"desempenho_curso_{course_id}.csv"
                ts = int(time.time())
                if "." in suggested:
                    from re import sub
                    fname = sub(r"(\.[a-zA-Z0-9]+)$", fr"_{ts}\1", suggested)
                else:
                    fname = f"{suggested}_{ts}.csv"
                download_path = str(downloads_dir / fname)
                await download.save_as(download_path)
                break
            except PWTimeout:
                await page.wait_for_timeout(2500)

        await context.close(); await browser.close()

        if not download_path:
            raise RuntimeError("Tempo máximo atingido sem aparecer 'Baixar' no modal.")
        return download_path

# ---------- UI ----------
with st.sidebar:
    st.subheader("Credenciais")
    st.write("DOMAIN:", "✅" if DOMAIN else "❌")
    st.write("API KEY:", "✅" if API_KEY else "❌")
    st.write("LOGIN:",   "✅" if LOGIN   else "❌")
    st.write("SENHA:",   "✅" if PASSWORD else "❌")

if not all([DOMAIN, API_KEY, LOGIN, PASSWORD]):
    st.warning("Preencha os *Secrets*: TEACHLR_DOMAIN, TEACHLR_API_KEY, TEACHLR_EMAIL, TEACHLR_PASSWORD.")
    st.stop()

st.write("1) Busque o curso | 2) Selecione | 3) Clique em **Gerar relatório**")

q = st.text_input("🔎 Buscar curso (título contém)", value="Onboarding Alice")
if st.button("Buscar"):
    try:
        st.session_state["courses"] = search_courses(q)
    except Exception as e:
        st.error(f"Erro buscando cursos: {e}")

# lista e seleção
if "courses" in st.session_state:
    opts = {f'[{c["id"]}] {c["title"]}': c["id"] for c in st.session_state["courses"]}
    if not opts:
        st.info("Nenhum curso encontrado.")
    else:
        pick_label = st.selectbox("Selecione o curso:", list(opts.keys()))
        course_id = opts[pick_label]

        if st.button("🚀 Gerar relatório"):
            with st.status("Gerando relatório no Teachlr…", expanded=True) as status:
                try:
                    path = asyncio.run(generate_and_download_report(course_id))
                    status.update(label="Relatório pronto!", state="complete")
                    with open(path, "rb") as f:
                        st.download_button("⬇️ Baixar arquivo", f, file_name=Path(path).name)
                except Exception as e:
                    status.update(label="Falhou 😢", state="error")
                    st.error(str(e))
else:
    st.caption("Dica: clique em **Buscar** para carregar cursos.")

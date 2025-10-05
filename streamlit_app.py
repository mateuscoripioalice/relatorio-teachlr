# streamlit_app.py
import os, re, sys, time, asyncio
from pathlib import Path

import streamlit as st
import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout, Route, Request

# ----------------------------------
# Config Streamlit
# ----------------------------------
st.set_page_config(page_title="Relatório Teachlr", page_icon="📊")
st.title("📊 Relatório de Desempenho — Teachlr")

if "busy" not in st.session_state:
    st.session_state["busy"] = False

# ----------------------------------
# Secrets / Ambiente
# ----------------------------------
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

# ----------------------------------
# Helpers
# ----------------------------------
def search_courses(query: str, limit: int = 50):
    url = f"{BASE_API}/courses?paginate=true&limit={limit}&search={query}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("data", data)
    return [{"id": int(c.get("id")), "title": c.get("title") or "(sem título)"} for c in items]

async def robust_login_hash(page, base_app: str, email: str, password: str, out_dir: Path) -> None:
    """
    Garante sessão logada. Tenta reaproveitar cookies indo direto ao dashboard; se não,
    faz login por e-mail/senha na #signin. Procura campos também dentro de iframes e
    tem fallback via JS.
    """
    # 1) tenta usar sessão existente
    try:
        await page.goto(f"{base_app}#dashboard/", wait_until="domcontentloaded")
        await page.wait_for_url(re.compile(r".*/#dashboard/.*"), timeout=3500)
        return
    except:
        pass

    # 2) vai para a página de login
    await page.goto(f"{base_app}#signin", wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=6000)
    except: 
        pass

    # util: varredura em page e em iframes
    async def find_in_all_contexts(selectors, by="css"):
        pages = [page, *page.frames]
        for ctx in pages:
            for sel in selectors:
                try:
                    loc = (ctx.locator(sel) if by=="css" else
                           ctx.get_by_label(sel) if by=="label" else
                           ctx.get_by_placeholder(sel))
                    await loc.first.wait_for(timeout=1500)
                    return loc.first
                except: 
                    continue
        return None

    # candidatos de campos
    email_placeholders = ["Usuário ou e-mail", "Usuário ou e-mail", "E-mail", "Email", "Usuário", "Login"]
    senha_placeholders = ["Senha", "Password"]

    email_css = [
        'input[placeholder*="e-mail" i]', 'input[placeholder*="email" i]',
        'input[name="email"]', 'input[name="username"]', 'input[type="email"]', '#email'
    ]
    senha_css = [
        'input[placeholder*="senha" i]', 'input[type="password"]',
        'input[name="password"]', '#password'
    ]

    # tenta localizar o campo de e-mail
    email_loc = (await find_in_all_contexts(email_placeholders, by="placeholder")
                 or await find_in_all_contexts(email_placeholders, by="label")
                 or await find_in_all_contexts(email_css, by="css"))

    # tenta localizar o campo de senha
    senha_loc = (await find_in_all_contexts(senha_placeholders, by="placeholder")
                 or await find_in_all_contexts(senha_placeholders, by="label")
                 or await find_in_all_contexts(senha_css, by="css"))

    # Se não encontrou de cara, clica no botão "Entrar" do topo (alguns temas mostram o form após esse clique)
    if not (email_loc and senha_loc):
        try:
            await page.get_by_role("link", name=re.compile(r"Entrar|Login", re.I)).first.click(timeout=1200)
            await page.wait_for_timeout(600)
        except: 
            pass
        # tenta de novo
        email_loc = email_loc or (await find_in_all_contexts(email_placeholders, by="placeholder")
                                  or await find_in_all_contexts(email_placeholders, by="label")
                                  or await find_in_all_contexts(email_css, by="css"))
        senha_loc = senha_loc or (await find_in_all_contexts(senha_placeholders, by="placeholder")
                                  or await find_in_all_contexts(senha_placeholders, by="label")
                                  or await find_in_all_contexts(senha_css, by="css"))

    # debug se ainda não achou
    if not email_loc or not senha_loc:
        out_dir.mkdir(exist_ok=True)
        await page.screenshot(path=str(out_dir / "debug_login.png"), full_page=True)
        (out_dir / "debug_login.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError('Não achei campos de login. Veja downloads/debug_login.*')

    # garantir visibilidade e foco
    try: await email_loc.scroll_into_view_if_needed(timeout=800)
    except: pass
    await email_loc.click(timeout=1500)
    try:
        await email_loc.fill(email, timeout=2000)
    except:
        # fallback por JS
        try:
            handle = await email_loc.element_handle()
            await page.evaluate("(el, val)=>{el.value=''; el.focus(); el.value=val; el.dispatchEvent(new Event('input',{bubbles:true}));}", handle, email)
        except: pass

    try: await senha_loc.scroll_into_view_if_needed(timeout=800)
    except: pass
    await senha_loc.click(timeout=1500)
    try:
        await senha_loc.fill(password, timeout=2000)
    except:
        try:
            handle = await senha_loc.element_handle()
            await page.evaluate("(el, val)=>{el.value=''; el.focus(); el.value=val; el.dispatchEvent(new Event('input',{bubbles:true}));}", handle, password)
        except: pass

    # clicar no botão de login
    clicked = False
    for bsel in [
        'button[type="submit"]',
        'button:has-text("Login")',
        'button:has-text("Entrar")',
        'text=/^Login$/',
        'text=/^Entrar$/',
    ]:
        try:
            await (page.locator(bsel) if bsel.startswith('button') else page.locator(bsel)).first.click(timeout=1200)
            clicked = True
            break
        except:
            continue

    if not clicked:
        # ENTER no campo de senha como último recurso
        try:
            await senha_loc.press("Enter", timeout=800)
        except: pass

    # aguarda cair no dashboard
    try:
        await page.wait_for_url(re.compile(r".*/#dashboard/.*"), timeout=12000)
    except PWTimeout:
        await page.screenshot(path=str(out_dir / "debug_after_submit.png"), full_page=True)
        (out_dir / "debug_after_submit.html").write_text(await page.content(), encoding="utf-8")
        raise RuntimeError("Login não completou. Veja downloads/debug_after_submit.*")


# Bloqueia recursos pesados/3rd-party para acelerar
# substitua o router existente por este:
BLOCK_EXT = (".png",".jpg",".jpeg",".gif",".webp",".svg",".mp4",".webm",".woff",".woff2",".ttf",".otf",".eot")

def should_block(req: Request) -> bool:
    url = req.url.lower()
    # na tela de login, não bloqueie nada além de mídia/arquivos pesados
    return any(url.endswith(ext) for ext in BLOCK_EXT)

BLOCK_HOSTS = ("google-analytics", "facebook", "segment", "hotjar", "doubleclick", "googletagmanager")

def should_block(req: Request) -> bool:
    url = req.url.lower()
    if any(h in url for h in BLOCK_HOSTS): return True
    if any(url.endswith(ext) for ext in BLOCK_EXT): return True
    return False

# ----------------------------------
# Util: achar botão "Desempenho dos estudantes" com variações
# ----------------------------------
BUTTON_VARIANTS = [
    r"Desempenho dos estudantes",
    r"Desempenho",
    r"Relatório de desempenho",
    r"Relatórios",
    r"Performance",
    r"Student.*Performance",
    r"Performance.*Student",
]

async def click_students_performance(page, out_dir: Path):
    # 1) tenta botão direto com has-text
    for pat in BUTTON_VARIANTS:
        try:
            await page.locator(f'button:has-text("{pat}")').first.click(timeout=1200)
            return
        except: pass

    # 2) tenta por role=button com regex no nome
    for pat in BUTTON_VARIANTS:
        try:
            await page.get_by_role("button", name=re.compile(pat, re.I)).first.click(timeout=1200)
            return
        except: pass

    # 3) qualquer elemento com esse texto
    for pat in BUTTON_VARIANTS:
        try:
            await page.get_by_text(re.compile(pat, re.I), exact=False).first.click(timeout=1200)
            return
        except: pass

    # 4) força via JS: clica no primeiro nó visível que contenha o texto
    code = """
    (texts) => {
      function visible(el){
        const rect = el.getBoundingClientRect();
        return !!(rect.width && rect.height);
      }
      for (const t of texts){
        const xpath = `//*[contains(translate(normalize-space(string(.)), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÁÀÃÂÉÊÍÓÔÕÚÇ','abcdefghijklmnopqrstuvwxyzáàãâéêíóôõúç'), "${t.toLowerCase()}")]`;
        const res = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        for (let i=0; i<res.snapshotLength; i++){
          const el = res.snapshotItem(i);
          if (visible(el)) { el.scrollIntoView({behavior:'instant', block:'center'}); el.click(); return true; }
        }
      }
      return false;
    }
    """
    found = await page.evaluate(code, BUTTON_VARIANTS)
    if found: 
        return

    # Se nada deu certo, salva screenshot e erra
    await page.screenshot(path=str(out_dir / "debug_students.png"), full_page=True)
    raise RuntimeError('Não achei "Desempenho dos estudantes". Veja downloads/debug_students.png')

# ----------------------------------
# Fluxo principal
# ----------------------------------
async def generate_and_download_report_from_students_url(
    students_url: str,
    force_generate: bool,
    max_wait_sec: int = 180
) -> str:
    """
    Abre a aba 'Estudantes', tenta Baixar direto (modo rápido). Se 'force_generate' estiver marcado,
    clica 'Gerar novo relatório' antes. Baixa o arquivo e retorna o caminho.
    """
    out_dir = Path("./downloads"); out_dir.mkdir(exist_ok=True)
    state_path = Path("/tmp/state.json")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-background-networking", "--disable-background-timer-throttling",
        ])
        context = await browser.new_context(
            accept_downloads=True,
            storage_state=str(state_path) if state_path.exists() else None,
            viewport={"width": 1280, "height": 900}
        )

        async def router(route: Route, request: Request):
            try:
                if should_block(request): await route.abort()
                else: await route.continue_()
            except Exception:
                try: await route.continue_()
                except: pass
        await context.route("**/*", router)

        page = await context.new_page()

        try:
            # Login (rápido se storage_state válido)
            await robust_login_hash(page, BASE_APP, LOGIN, PASSWORD, out_dir)
            try: await context.storage_state(path=str(state_path))
            except: pass

            # Vai para a aba Estudantes
            await page.goto(students_url, wait_until="domcontentloaded")
            try: await page.wait_for_load_state("networkidle", timeout=8000)
            except: pass

            # Garante estar na seção certa (às vezes precisa clicar na aba "Estudantes")
            try:
                await page.wait_for_selector('text="Desempenho dos estudantes"', timeout=3000)
            except PWTimeout:
                try:
                    await page.click('text=/^Estudantes$/', timeout=1500)
                    await page.wait_for_timeout(500)
                except: pass

            # Abre modal de desempenho (com vários fallbacks)
            await click_students_performance(page, out_dir)

            # Modal helper
            def modal():
                return page.locator('[role="dialog"], .modal, .v-dialog').first

            # Modo rápido: tenta Baixar direto
            if not force_generate:
                try:
                    btn = modal().get_by_role("button", name=re.compile(r"Baixar|Download", re.I))
                    await btn.wait_for(timeout=4000)
                    async with page.expect_download(timeout=25000) as dl_info:
                        await btn.click()
                    download = await dl_info.value
                    suggested = download.suggested_filename or "relatorio.csv"
                    ts = int(time.time())
                    fname = (re.sub(r"(\.[a-zA-Z0-9]+)$", fr"_{ts}\1", suggested)
                             if "." in suggested else f"{suggested}_{ts}.csv")
                    save_as = str(out_dir / fname)
                    await download.save_as(save_as)
                    return save_as
                except PWTimeout:
                    pass

            # Gera novo relatório
            try:
                await modal().get_by_role("button", name=re.compile(r"Gerar novo relatório|Gerar|Generate", re.I)).click(timeout=4000)
            except PWTimeout:
                # às vezes é um link estilizado
                try:
                    await modal().get_by_text(re.compile(r"Gerar", re.I), exact=False).first.click(timeout=2500)
                except: pass

            # Loop até aparecer "Baixar"
            start = time.time()
            while time.time() - start < max_wait_sec:
                try:
                    # Atualiza se tiver
                    try:
                        await modal().locator('button[title*="Atualizar"], button:has(svg)').first.click(timeout=800)
                    except: pass

                    btn = modal().get_by_role("button", name=re.compile(r"Baixar|Download", re.I))
                    await btn.wait_for(timeout=3500)
                    async with page.expect_download(timeout=25000) as dl_info:
                        await btn.click()
                    download = await dl_info.value

                    suggested = download.suggested_filename or "relatorio.csv"
                    ts = int(time.time())
                    fname = (re.sub(r"(\.[a-zA-Z0-9]+)$", fr"_{ts}\1", suggested)
                             if "." in suggested else f"{suggested}_{ts}.csv")
                    save_as = str(out_dir / fname)
                    await download.save_as(save_as)
                    return save_as
                except PWTimeout:
                    await page.wait_for_timeout(1200)

            await page.screenshot(path=str(out_dir / "debug_wait_baixar.png"), full_page=True)
            raise RuntimeError("Não apareceu o botão 'Baixar' a tempo. Veja downloads/debug_wait_baixar.png")
        finally:
            await context.close()
            await browser.close()

# ----------------------------------
# UI
# ----------------------------------
with st.sidebar:
    st.subheader("Credenciais")
    st.write("DOMAIN:", "✅" if DOMAIN else "❌")
    st.write("API KEY:", "✅" if API_KEY else "❌")
    st.write("LOGIN:",   "✅" if LOGIN   else "❌")
    st.write("SENHA:",   "✅" if PASSWORD else "❌")

st.markdown(
    "Cole a **URL da aba Estudantes** do curso (ex.: "
    "`https://alice.teachlr.com/#dashboard/instructor/<slug>/students`)."
)

with st.expander("🔎 Buscar cursos por título (API) — opcional", expanded=False):
    q = st.text_input("Buscar por:", value="Onboarding Alice")
    limit = st.number_input("Limite", min_value=1, max_value=200, value=50, step=1)
    if st.button("Buscar cursos"):
        try:
            st.json(search_courses(q, limit=limit))
            st.caption("Use como referência; a URL final usa o *slug* do curso na plataforma.")
        except Exception as e:
            st.error(f"Erro na API de cursos: {e}")

st.divider()

students_url = st.text_input(
    "URL da aba **Estudantes** do curso:",
    value="https://alice.teachlr.com/#dashboard/instructor/skip-level-meeting-pitayas-navegs-nurses-physicians-e-sales/students",
)
col1, col2, col3 = st.columns([1,1,1])
with col1:
    force_generate = st.toggle("Forçar gerar novo", value=False, help="Se desmarcado, tenta baixar direto (mais rápido).")
with col2:
    run = st.button("🚀 Gerar/baixar relatório")
with col3:
    dry = st.button("✅ Testar login")

def show_debug():
    for dbg in [
        "downloads/debug_students.png",
        "downloads/debug_wait_baixar.png",
        "downloads/debug_login.png",
        "downloads/debug_login.html",
    ]:
        p = Path(dbg)
        if p.exists():
            if p.suffix == ".png":
                # Removido use_container_width (sua versão do Streamlit não aceita)
                st.image(str(p), caption=p.name)
            else:
                with open(p, "rb") as f:
                    st.download_button(f"Baixar {p.name}", f, file_name=p.name)

if dry:
    if st.session_state["busy"]:
        st.info("Já existe uma execução em andamento…")
    else:
        st.session_state["busy"] = True
        try:
            with st.status("Testando login…", expanded=True) as status:
                async def _test():
                    out_dir = Path("./downloads"); out_dir.mkdir(exist_ok=True)
                    async with async_playwright() as p:
                        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                        context = await browser.new_context()
                        page = await context.new_page()
                        try:
                            await robust_login_hash(page, BASE_APP, LOGIN, PASSWORD, out_dir)
                        finally:
                            await context.close(); await browser.close()
                asyncio.run(_test())
                status.update(label="Login OK ✅", state="complete")
        except Exception as e:
            st.error(str(e)); show_debug()
        finally:
            st.session_state["busy"] = False

if run:
    if not students_url.strip():
        st.error("Cole a URL da aba Estudantes do curso.")
    elif st.session_state["busy"]:
        st.info("Já existe uma execução em andamento…")
    else:
        st.session_state["busy"] = True
        try:
            with st.status("Processando…", expanded=True) as status:
                path = asyncio.run(generate_and_download_report_from_students_url(
                    students_url, force_generate=force_generate
                ))
                status.update(label="Relatório pronto! ✅", state="complete")
                with open(path, "rb") as f:
                    st.download_button("⬇️ Baixar arquivo", f, file_name=Path(path).name)
        except Exception as e:
            st.error(str(e)); show_debug()
        finally:
            st.session_state["busy"] = False

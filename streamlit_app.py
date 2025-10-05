import re, time, asyncio
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

async def download_performance_report(course_id:int, headless:bool=True, max_wait_sec:int=240) -> str:
    """
    1) Abre o curso
    2) Abre 'Desempenho dos estudantes'
    3) Clica 'Gerar novo relatório' (se generate_new=True)
    4) Faz refresh até aparecer 'Baixar' e captura o download
    Retorna o caminho salvo.
    """
    base_app = f"https://{TEACHLR_DOMAIN}.teachlr.com/"
    course_url = f"{base_app}courses/{course_id}"
    Path(DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless, args=["--no-sandbox"])
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        # --- login ---
        await page.goto(f"{base_app}login", wait_until="domcontentloaded")
        await page.fill('input[name="email"], input[type="email"]', TEACHLR_EMAIL)
        await page.fill('input[name="password"], input[type="password"]', TEACHLR_PASSWORD)
        await page.click('button:has-text("Entrar"), button[type="submit"]')
        await page.wait_for_load_state("networkidle")

        # --- curso ---
        await page.goto(course_url, wait_until="domcontentloaded")
        # abre a seção de estudantes (alguns layouts mostram direto após clicar no botão de desempenho)
        try:
            await page.click('text=/^Estudantes$/', timeout=3000)
        except:
            pass

        # --- abre o modal ---
        btn_perf = 'button:has-text("Desempenho dos estudantes"), text=/Desempenho dos estudantes/i'
        await page.wait_for_selector(btn_perf, timeout=15000)
        await page.click(btn_perf)

        # modal (escopo p/ seletores)
        dialog = page.locator('[role="dialog"], .modal, .v-dialog').first

        # --- clica em "Gerar novo relatório" (se existir) ---
        try:
            await dialog.get_by_role("button", name=re.compile(r"Gerar novo relatório", re.I)).click(timeout=4000)
        except PWTimeout:
            # pode já existir um relatório pronto, então seguimos
            pass

        # --- espera aparecer "Baixar"; se não, clica refresh em loop ---
        start = time.time()
        download_path = None

        while time.time() - start < max_wait_sec:
            # Se tiver botão de refresh no modal, aciona
            try:
                refresh_btn = dialog.locator('button:has-text("Atualizar"), button:has(svg), [title="Atualizar"]').first
                # Se esse seletor for muito genérico, dá pra trocar por: dialog.locator('button >> nth=2') etc.
                await refresh_btn.click(timeout=2000)
            except:
                pass

            # Se houver link/botão "Baixar", captura o download
            try:
                await dialog.get_by_role("button", name=re.compile(r"Baixar", re.I)).wait_for(timeout=3000)
                async with page.expect_download(timeout=20000) as dl_info:
                    await dialog.get_by_role("button", name=re.compile(r"Baixar", re.I)).click()
                download = await dl_info.value
                suggested = download.suggested_filename or f"desempenho_curso_{course_id}.csv"
                ts = int(time.time())
                if "." in suggested:
                    filename = re.sub(r"(\.[a-zA-Z0-9]+)$", fr"_{ts}\1", suggested)
                else:
                    filename = f"{suggested}_{ts}.csv"
                download_path = str(Path(DOWNLOAD_DIR) / filename)
                await download.save_as(download_path)
                break
            except PWTimeout:
                # ainda não está pronto; espera 3s e tenta de novo
                await page.wait_for_timeout(3000)

        await context.close()
        await browser.close()

        if not download_path:
            raise RuntimeError("Tempo máximo atingido sem aparecer 'Baixar' no modal.")
        return download_path

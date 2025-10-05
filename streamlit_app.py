import os
import streamlit as st

st.set_page_config(page_title="Relatório Teachlr", page_icon="📊")
st.title("📊 Relatório de Desempenho — Teachlr")

# Mostra rapidamente se os segredos estão carregados
domain   = os.getenv("TEACHLR_DOMAIN") or st.secrets.get("TEACHLR_DOMAIN", "")
api_key  = os.getenv("TEACHLR_API_KEY") or st.secrets.get("TEACHLR_API_KEY", "")
email    = os.getenv("TEACHLR_EMAIL") or st.secrets.get("TEACHLR_EMAIL", "")
password = os.getenv("TEACHLR_PASSWORD") or st.secrets.get("TEACHLR_PASSWORD", "")

cols = st.columns(4)
cols[0].metric("DOMAIN", "OK" if domain else "—")
cols[1].metric("API KEY", "OK" if api_key else "—")
cols[2].metric("LOGIN", "OK" if email else "—")
cols[3].metric("SENHA", "OK" if password else "—")

if not all([domain, api_key, email, password]):
    st.warning("Configure os *Secrets* do app (TEACHLR_DOMAIN, TEACHLR_API_KEY, TEACHLR_EMAIL, TEACHLR_PASSWORD).")
    st.stop()

st.divider()
st.write("Clique para validar Playwright/Chromium e listar cursos via API (sanity check).")

if st.button("🔍 Testar ambiente"):
    import sys, subprocess, requests
    # instala chromium só quando você clicar (evita travar no import)
    try:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
        st.success("Playwright/Chromium OK.")
    except Exception as e:
        st.error(f"Falha instalando Chromium: {e}")

    # ping simples na API (lista 5 cursos)
    try:
        headers = {"Content-Type": "application/json", "Authorization": api_key}
        url = f"https://api.teachlr.com/{domain}/api/courses?paginate=true&limit=5"
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = data.get("data", data)
        st.write("Cursos (top 5):", [{ "id": c.get("id"), "title": c.get("title") } for c in items])
    except Exception as e:
        st.error(f"Erro chamando API: {e}")

st.info("Se isto aparece, o app está renderizando corretamente. Depois acoplamos o fluxo de gerar/baixar relatório.")

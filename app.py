import sys
import traceback as tb
import streamlit as st

st.set_page_config(
    page_title="HR Policy Assistant",
    page_icon="🛡️",
    layout="wide",
)

_import_errors = []

try:
    from src.rag import build_vectorstore
except Exception as e:
    _import_errors.append(("src.rag (FAISS / fastembed)", str(e), tb.format_exc()))

try:
    from src.ui.sidebar import render_sidebar
    from src.ui.landing import render_landing
    from src.ui.tab_chat import render_chat_tab
    from src.ui.tab_docs import render_docs_tab
except Exception as e:
    _import_errors.append(("src.ui", str(e), tb.format_exc()))

if _import_errors:
    st.title("🛑 Startup Error — Missing Dependencies")
    st.error("One or more packages failed to import. See details below.")
    for pkg, msg, trace in _import_errors:
        with st.expander(f"❌  {pkg}  —  {msg}"):
            st.code(trace, language="python")
    st.markdown("**Fix:** `pip install -r requirements.txt`")
    st.info(f"Python: `{sys.version}`")
    st.stop()

groq_key, guard_model, chat_model = render_sidebar()

if not groq_key:
    render_landing()
    st.stop()

try:
    vectorstore = build_vectorstore()
except Exception:
    st.error("**Failed to build the HR policy vector store.**")
    with st.expander("Error details"):
        st.code(tb.format_exc(), language="python")
    st.stop()

st.title("HR Policy Assistant")
st.caption("Acme Corp · NeMo Guardrails + FAISS RAG + Groq")

tab_chat, tab_docs = st.tabs(["💬 Assistant", "📄 HR Policy Documents"])

with tab_chat:
    render_chat_tab(vectorstore, groq_key, guard_model, chat_model)

with tab_docs:
    render_docs_tab()

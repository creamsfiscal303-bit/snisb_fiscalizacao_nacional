import io
from datetime import datetime

import pandas as pd
import streamlit as st

from snisb import (
    ESTADOS, buscar_barragens, preparar_dados, preparar_triagem,
    gerar_excel, gerar_csv, SERVICE_URL
)

st.set_page_config(
    page_title="Painel Nacional de Fiscalização de Barragens",
    page_icon="🌊",
    layout="wide",
)

st.title("Painel Nacional de Fiscalização de Barragens")
st.caption("SNISB • Sistema Nacional de Informações sobre Segurança de Barragens")
st.markdown(
    "Ferramenta de apoio ao planejamento fiscalizatório dos CREAs. "
    "Os alertas são indicadores para conferência e não caracterizam, isoladamente, infração."
)

with st.sidebar:
    st.header("Parâmetros")
    uf = st.selectbox(
        "Selecione o estado (CREA)",
        options=list(ESTADOS.keys()),
        format_func=lambda x: f"{x} — {ESTADOS[x]}",
        index=list(ESTADOS.keys()).index("CE"),
    )
    atualizar = st.button("Consultar SNISB", type="primary", use_container_width=True)
    st.divider()
    st.caption("Fonte oficial:")
    st.markdown(f"[Camada ArcGIS REST]({SERVICE_URL})")

if atualizar:
    with st.spinner(f"Consultando barragens de {ESTADOS[uf]}..."):
        try:
            raw = buscar_barragens(uf)
            st.session_state["dados_snisb"] = preparar_dados(raw)
            st.session_state["uf_snisb"] = uf
            st.session_state["extraido_em"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        except Exception as exc:
            st.error(f"Não foi possível consultar o SNISB: {exc}")
            st.stop()

if "dados_snisb" not in st.session_state:
    st.info("Selecione a UF e clique em **Consultar SNISB** para carregar os dados atualizados.")
    st.stop()

df = st.session_state["dados_snisb"].copy()
uf_atual = st.session_state["uf_snisb"]
triagem = preparar_triagem(df)

with st.sidebar:
    st.success(f"UF carregada: {uf_atual}")
    st.caption(f"Extração: {st.session_state.get('extraido_em', '—')}")
    municipios = sorted([x for x in df.get("ING_NM_MUNICIPIO", pd.Series(dtype=str)).dropna().astype(str).unique() if x.strip()])
    municipio_selecionado = st.multiselect("Município", municipios, default=[])
    riscos = sorted([x for x in df.get("CATEGORIA_RISCO", pd.Series(dtype=str)).dropna().astype(str).unique() if x.strip()])
    risco_selecionado = st.multiselect("Categoria de risco", riscos, default=[])
    danos = sorted([x for x in df.get("DANO_POTENCIAL", pd.Series(dtype=str)).dropna().astype(str).unique() if x.strip()])
    dano_selecionado = st.multiselect("Dano potencial", danos, default=[])

filtrado = df.copy()
if municipio_selecionado:
    filtrado = filtrado[filtrado["ING_NM_MUNICIPIO"].astype(str).isin(municipio_selecionado)]
if risco_selecionado:
    filtrado = filtrado[filtrado["CATEGORIA_RISCO"].astype(str).isin(risco_selecionado)]
if dano_selecionado:
    filtrado = filtrado[filtrado["DANO_POTENCIAL"].astype(str).isin(dano_selecionado)]

triagem_filtrada = preparar_triagem(filtrado)
c1, c2, c3, c4 = st.columns(4)
c1.metric("Barragens", f"{len(filtrado):,}".replace(",", "."))
c2.metric("Risco alto", int(triagem_filtrada["TRIAGEM_RISCO_ALTO"].sum()))
c3.metric("Dano potencial alto", int(triagem_filtrada["TRIAGEM_DANO_POTENCIAL_ALTO"].sum()))
c4.metric("Sem data de inspeção", int(triagem_filtrada["PENDENCIA_DATA_INSPECAO_NAO_INFORMADA"].sum()))

tab_mapa, tab_base, tab_triagem, tab_exportar, tab_cruzamento = st.tabs(
    ["Mapa", "Base SNISB", "Triagem fiscalizatória", "Exportar", "Cruzamento CREA"]
)

with tab_mapa:
    st.subheader(f"Distribuição geográfica — {ESTADOS[uf_atual]}")
    lat_col, lon_col = "BAR_NU_LATITUDE", "BAR_NU_LONGITUDE"
    if lat_col in filtrado.columns and lon_col in filtrado.columns:
        mapa = filtrado.copy()
        mapa[lat_col] = pd.to_numeric(mapa[lat_col], errors="coerce")
        mapa[lon_col] = pd.to_numeric(mapa[lon_col], errors="coerce")
        mapa = mapa.dropna(subset=[lat_col, lon_col])
        mapa = mapa[
            mapa[lat_col].between(-34, 6) & mapa[lon_col].between(-75, -30)
        ]
        if not mapa.empty:
            st.map(mapa.rename(columns={lat_col: "lat", lon_col: "lon"})[["lat", "lon"]])
            st.caption(f"{len(mapa)} barragens com coordenadas válidas exibidas no mapa.")
        else:
            st.warning("Não há coordenadas válidas para exibir com os filtros atuais.")
    else:
        st.warning("A camada não retornou os campos de latitude/longitude esperados.")
    if "ING_NM_MUNICIPIO" in filtrado.columns:
        st.bar_chart(filtrado["ING_NM_MUNICIPIO"].fillna("Não informado").value_counts().head(20))

with tab_base:
    st.subheader("Registros do SNISB")
    st.dataframe(filtrado, use_container_width=True, hide_index=True)
    st.caption("Os campos exibidos dependem da camada oficial e podem ser atualizados pelo mantenedor do SNISB.")

with tab_triagem:
    st.subheader("Itens para conferência")
    st.dataframe(triagem_filtrada, use_container_width=True, hide_index=True)
    st.warning(
        "Ausência de informação no cadastro não prova ausência de documento, inspeção ou responsabilidade técnica. "
        "Confirme em fonte oficial antes de iniciar diligência ou procedimento fiscalizatório."
    )

with tab_exportar:
    st.subheader("Baixar dados filtrados")
    csv_bytes = gerar_csv(filtrado)
    xlsx_bytes = gerar_excel(filtrado, triagem_filtrada)
    col_a, col_b = st.columns(2)
    col_a.download_button(
        "Baixar CSV", data=csv_bytes,
        file_name=f"barragens_snisb_{uf_atual.lower()}.csv",
        mime="text/csv", use_container_width=True,
    )
    col_b.download_button(
        "Baixar Excel", data=xlsx_bytes,
        file_name=f"barragens_snisb_{uf_atual.lower()}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

with tab_cruzamento:
    st.subheader("Preparação para cruzamento com dados do CREA")
    st.markdown(
        "Esta versão prepara a base SNISB para cruzamentos posteriores com cadastro de empresas, "
        "profissionais e ARTs. Ainda não consulta sistemas internos do CREA automaticamente."
    )
    st.write("Campos de interesse: CNPJ, razão social, registro da empresa, situação cadastral, "
             "nome/registro do profissional, número da ART, objeto e atividade técnica, datas e fonte.")
    modelo = pd.DataFrame(columns=[
        "CNPJ", "RAZAO_SOCIAL", "NOME_FANTASIA", "MUNICIPIO", "UF",
        "NUMERO_REGISTRO_EMPRESA", "SITUACAO_REGISTRO_EMPRESA",
        "NOME_PROFISSIONAL", "REGISTRO_PROFISSIONAL", "TITULO_PROFISSIONAL",
        "NUMERO_ART", "DATA_REGISTRO_ART", "ATIVIDADE_TECNICA_ART",
        "OBJETO_ART", "DATA_INICIO", "DATA_FIM", "FONTE_DADO", "DATA_CONSULTA",
        "OBSERVACOES"
    ])
    st.download_button(
        "Baixar modelo CSV para cruzamento CREA",
        data=gerar_csv(modelo),
        file_name="modelo_cruzamento_crea.csv",
        mime="text/csv",
    )
    st.info("O cruzamento deve ser feito somente com dados obtidos de fontes autorizadas. "
            "Correspondência de nome, por si só, não comprova vínculo de uma ART com determinada barragem.")

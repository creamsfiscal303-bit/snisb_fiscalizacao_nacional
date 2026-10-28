# -*- coding: utf-8 -*-
"""Painel nacional de barragens do SNISB para planejamento de fiscalização profissional."""
from __future__ import annotations

from io import BytesIO
from datetime import datetime
import re
import unicodedata

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

SERVICE_URL = "https://www.snirh.gov.br/arcgis/rest/services/IG/SNISB/FeatureServer/0"
QUERY_URL = f"{SERVICE_URL}/query"
UF_OPTIONS = ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
              "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"]
UF_NOMES = {"CE": "Ceará", "MS": "Mato Grosso do Sul"}
TIMEOUT = 60


def normalize(value):
    if value is None or pd.isna(value):
        return ""
    s = unicodedata.normalize("NFKD", str(value).strip().upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s)


def get_layer_fields():
    r = requests.get(SERVICE_URL, params={"f": "json"}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "Erro ao consultar camada ArcGIS"))
    return [f["name"] for f in data.get("fields", [])]


@st.cache_data(ttl=1800, show_spinner=False)
def load_data(uf: str) -> pd.DataFrame:
    """Consulta a camada oficial ArcGIS, pagina por objectIds para evitar limite de registros."""
    meta = requests.get(SERVICE_URL, params={"f": "json"}, timeout=TIMEOUT)
    meta.raise_for_status()
    meta = meta.json()
    if "error" in meta:
        raise RuntimeError(meta["error"].get("message", "Erro ao consultar camada SNISB"))
    fields = [f["name"] for f in meta.get("fields", [])]
    oid_field = meta.get("objectIdField", "OBJECTID")
    where_field = "ING_SG_UFMUNICIPIO" if "ING_SG_UFMUNICIPIO" in fields else None
    if not where_field:
        raise RuntimeError("A camada não disponibilizou o campo de UF municipal esperado (ING_SG_UFMUNICIPIO).")
    params = {
        "where": f"{where_field} = '{uf}'",
        "returnIdsOnly": "true",
        "returnGeometry": "false",
        "f": "json",
    }
    response = requests.get(QUERY_URL, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    ids_data = response.json()
    if "error" in ids_data:
        raise RuntimeError(ids_data["error"].get("message", "Erro ao obter IDs do SNISB"))
    ids = ids_data.get("objectIds") or []
    if not ids:
        return pd.DataFrame(columns=fields + ["LATITUDE_MAPA", "LONGITUDE_MAPA"])
    rows = []
    # Consulta em lotes de 500 IDs, independente do limite da camada.
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        q = {
            "objectIds": ",".join(map(str, batch)),
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
        }
        rr = requests.get(QUERY_URL, params=q, timeout=TIMEOUT)
        rr.raise_for_status()
        payload = rr.json()
        if "error" in payload:
            raise RuntimeError(payload["error"].get("message", "Erro ao baixar registros"))
        for feature in payload.get("features", []):
            attrs = feature.get("attributes", {}) or {}
            geom = feature.get("geometry") or {}
            # Preferir coordenadas nos atributos; usar geometria como alternativa.
            lat = attrs.get("BAR_NU_LATITUDE")
            lon = attrs.get("BAR_NU_LONGITUDE")
            if lat in (None, "") or lon in (None, ""):
                lat = geom.get("y", lat)
                lon = geom.get("x", lon)
            try:
                lat = float(lat) if lat not in (None, "") else None
                lon = float(lon) if lon not in (None, "") else None
                if lat is not None and not (-90 <= lat <= 90): lat = None
                if lon is not None and not (-180 <= lon <= 180): lon = None
            except (TypeError, ValueError):
                lat, lon = None, None
            attrs["LATITUDE_MAPA"] = lat
            attrs["LONGITUDE_MAPA"] = lon
            rows.append(attrs)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop_duplicates(subset=[oid_field] if oid_field in df.columns else None)
        df["LATITUDE_MAPA"] = pd.to_numeric(df["LATITUDE_MAPA"], errors="coerce")
        df["LONGITUDE_MAPA"] = pd.to_numeric(df["LONGITUDE_MAPA"], errors="coerce")
        df["URL_MAPA"] = df.apply(
            lambda r: f"https://www.google.com/maps?q={r['LATITUDE_MAPA']},{r['LONGITUDE_MAPA']}"
            if pd.notna(r["LATITUDE_MAPA"]) and pd.notna(r["LONGITUDE_MAPA"]) else "",
            axis=1
        )
    return df


def risk_label(value):
    s = normalize(value)
    if not s: return "Não informado"
    if "MUITO ALTO" in s: return "Muito alto"
    if "ALTO" in s: return "Alto"
    if "MEDIO" in s: return "Médio"
    if "BAIXO" in s: return "Baixo"
    return str(value).strip().capitalize()


def risk_color(value):
    label = risk_label(value)
    # Cores operacionais para leitura rápida; não substituem a classificação oficial.
    return {
        "Muito alto": [160, 0, 0, 220],
        "Alto": [230, 35, 35, 220],
        "Médio": [245, 155, 35, 220],
        "Baixo": [35, 155, 75, 220],
        "Não informado": [130, 130, 130, 180],
    }.get(label, [80, 115, 180, 200])


def excel_bytes(filtered: pd.DataFrame, uf: str) -> bytes:
    df = filtered.copy()
    name_col = "BAR_NM_NOME" if "BAR_NM_NOME" in df.columns else None
    def col(name, default=""):
        return df[name] if name in df.columns else pd.Series([default] * len(df), index=df.index)
    roteiro = pd.DataFrame({
        "Prioridade visual (risco)": col("CATEGORIA_RISCO").map(risk_label),
        "Dano potencial associado": col("DANO_POTENCIAL"),
        "Barragem": col(name_col) if name_col else "",
        "Identificador SNISB": col("BAR_CD_SNISB"),
        "Empreendedor": col("NM_EMPREENDEDOR"),
        "Órgão fiscalizador": col("BAR_CD_BAR_ENT_FISCALIZADORA"),
        "Município": col("ING_NM_MUNICIPIO"),
        "UF": col("ING_SG_UFMUNICIPIO", uf),
        "Latitude": col("LATITUDE_MAPA"),
        "Longitude": col("LONGITUDE_MAPA"),
        "Link Google Maps": col("URL_MAPA"),
        "Uso principal": col("USO_PRINCIPAL"),
        "Altura (m) - base da fundação": col("BAR_NU_ALT_MAX_BASE_FUNDACAO"),
        "Altura (m) - nível do terreno": col("BAR_NU_ALT_MAX_NIVEL_TERRENO"),
        "Nível de perigo informado": col("NIVEL_PERIGO"),
        "Possui PAE": col("POSSUI_PAE"),
        "Possui plano de segurança": col("POSSUI_PLANO_SEGURANCA"),
        "Última inspeção informada": col("INS_DT_INSPECAO"),
        "Tipo de inspeção": col("TP_INSPECAO"),
        "Observações da equipe de campo": [""] * len(df),
        "Resultado da visita": [""] * len(df),
    })
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        roteiro.to_excel(writer, sheet_name="Roteiro de campo", index=False)
        df.to_excel(writer, sheet_name="Base filtrada", index=False)
        legenda = pd.DataFrame([
            ["Muito alto", "Vermelho escuro", "Prioridade gerencial visual; validar com fonte oficial."],
            ["Alto", "Vermelho", "Prioridade gerencial visual; validar com fonte oficial."],
            ["Médio", "Laranja", "Prioridade gerencial visual; validar com fonte oficial."],
            ["Baixo", "Verde", "Prioridade gerencial visual; validar com fonte oficial."],
            ["Não informado", "Cinza", "Categoria ausente ou não reconhecida na fonte."],
        ], columns=["Categoria", "Cor no mapa", "Observação"])
        legenda.to_excel(writer, sheet_name="Legenda", index=False)
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cells in ws.columns:
                max_len = min(max((len(str(c.value)) if c.value is not None else 0 for c in cells), default=10) + 2, 42)
                ws.column_dimensions[cells[0].column_letter].width = max(12, max_len)
    return output.getvalue()


st.set_page_config(page_title="Fiscalização de Barragens | SNISB", page_icon="🗺️", layout="wide")
st.title("Painel de Fiscalização de Barragens — SNISB")
st.caption("Ferramenta de apoio ao planejamento da fiscalização do exercício profissional. A categoria de risco é informativa e não representa avaliação estrutural do CREA.")

with st.sidebar:
    st.header("Consulta")
    uf = st.selectbox("Unidade da Federação", UF_OPTIONS, index=UF_OPTIONS.index("CE"))
    st.caption("O Ceará é o estado-piloto. O painel pode consultar outras UFs.")
    refresh = st.button("Atualizar dados da API", use_container_width=True)
    if refresh:
        load_data.clear()

try:
    with st.spinner(f"Consultando barragens do SNISB para {uf}..."):
        df_all = load_data(uf)
except Exception as exc:
    st.error(f"Não foi possível consultar a API do SNISB: {exc}")
    st.info("Verifique a conexão e tente novamente. A camada de origem é o serviço ArcGIS REST do SNISB.")
    st.stop()

if df_all.empty:
    st.warning(f"Nenhuma barragem foi retornada para {uf}.")
    st.stop()

risk_col = "CATEGORIA_RISCO" if "CATEGORIA_RISCO" in df_all.columns else None
dpa_col = "DANO_POTENCIAL" if "DANO_POTENCIAL" in df_all.columns else None
municipio_col = "ING_NM_MUNICIPIO" if "ING_NM_MUNICIPIO" in df_all.columns else None
uso_col = "USO_PRINCIPAL" if "USO_PRINCIPAL" in df_all.columns else None
df_all["RISCO_EXIBICAO"] = df_all[risk_col].map(risk_label) if risk_col else "Não informado"
df_all["COR_MAPA"] = df_all[risk_col].map(risk_color) if risk_col else [[130,130,130,180]] * len(df_all)

with st.sidebar:
    risks = ["Todos"] + sorted(df_all["RISCO_EXIBICAO"].dropna().unique().tolist())
    selected_risks = st.multiselect("Categoria de risco", risks, default=["Todos"])
    if dpa_col:
        dpas = ["Todos"] + sorted(df_all[dpa_col].fillna("Não informado").astype(str).unique().tolist())
        selected_dpas = st.multiselect("Dano potencial associado", dpas, default=["Todos"])
    else:
        selected_dpas = ["Todos"]
    if municipio_col:
        municipios = ["Todos"] + sorted(df_all[municipio_col].dropna().astype(str).unique().tolist())
        selected_municipios = st.multiselect("Município", municipios, default=["Todos"])
    else:
        selected_municipios = ["Todos"]
    if uso_col:
        usos = ["Todos"] + sorted(df_all[uso_col].dropna().astype(str).unique().tolist())
        selected_usos = st.multiselect("Uso principal", usos, default=["Todos"])
    else:
        selected_usos = ["Todos"]

filtered = df_all.copy()
if "Todos" not in selected_risks:
    filtered = filtered[filtered["RISCO_EXIBICAO"].isin(selected_risks)]
if dpa_col and "Todos" not in selected_dpas:
    filtered = filtered[filtered[dpa_col].fillna("Não informado").astype(str).isin(selected_dpas)]
if municipio_col and "Todos" not in selected_municipios:
    filtered = filtered[filtered[municipio_col].astype(str).isin(selected_municipios)]
if uso_col and "Todos" not in selected_usos:
    filtered = filtered[filtered[uso_col].astype(str).isin(selected_usos)]

located = filtered.dropna(subset=["LATITUDE_MAPA", "LONGITUDE_MAPA"]).copy()
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Barragens no filtro", f"{len(filtered):,}".replace(",", "."))
c2.metric("Com coordenadas", f"{len(located):,}".replace(",", "."))
c3.metric("Risco alto ou muito alto", int(filtered["RISCO_EXIBICAO"].isin(["Alto", "Muito alto"]).sum()))
c4.metric("Dano potencial alto", int(filtered[dpa_col].map(normalize).str.contains("ALTO", na=False).sum()) if dpa_col else "N/D")
c5.metric("Sem categoria de risco", int((filtered["RISCO_EXIBICAO"] == "Não informado").sum()))

tab_map, tab_data, tab_export, tab_about = st.tabs(["🗺️ Mapa", "📋 Dados", "📥 Exportar Excel", "ℹ️ Orientações"])

with tab_map:
    st.subheader("Localização das barragens por categoria de risco")
    st.markdown("🔴 **Alto** / 🔴 **Muito alto** · 🟠 **Médio** · 🟢 **Baixo** · ⚪ **Não informado**")
    if located.empty:
        st.warning("Não há coordenadas válidas nos registros selecionados.")
    else:
        map_data = located.copy()
        map_data["COR_R"] = map_data["COR_MAPA"].map(lambda c: c[0])
        map_data["COR_G"] = map_data["COR_MAPA"].map(lambda c: c[1])
        map_data["COR_B"] = map_data["COR_MAPA"].map(lambda c: c[2])
        map_data["COR_A"] = map_data["COR_MAPA"].map(lambda c: c[3])
        map_data["NOME_EXIBICAO"] = map_data.get("BAR_NM_NOME", pd.Series(["Barragem"]*len(map_data), index=map_data.index)).fillna("Barragem")
        map_data["MUNICIPIO_EXIBICAO"] = map_data.get("ING_NM_MUNICIPIO", pd.Series([""]*len(map_data), index=map_data.index)).fillna("")
        map_data["RISCO_EXIBICAO"] = map_data["RISCO_EXIBICAO"].astype(str)
        layer = pdk.Layer(
            "ScatterplotLayer",
            data=map_data,
            get_position="[LONGITUDE_MAPA, LATITUDE_MAPA]",
            get_fill_color="[COR_R, COR_G, COR_B, COR_A]",
            get_line_color=[40, 40, 40, 180],
            get_radius=1000,
            radius_min_pixels=4,
            radius_max_pixels=11,
            pickable=True,
            auto_highlight=True,
        )
        view = pdk.ViewState(
            latitude=float(map_data["LATITUDE_MAPA"].median()),
            longitude=float(map_data["LONGITUDE_MAPA"].median()),
            zoom=6,
            pitch=0,
        )
        tooltip = {"html": "<b>{NOME_EXIBICAO}</b><br/>Município: {MUNICIPIO_EXIBICAO}<br/>Risco: {RISCO_EXIBICAO}<br/>Dano potencial: {DANO_POTENCIAL}<br/>SNISB: {BAR_CD_SNISB}", "style": {"backgroundColor": "#222", "color": "white"}}
        st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view, tooltip=tooltip), use_container_width=True)
        st.caption(f"Exibindo {len(located):,} pontos com coordenadas válidas. As cores seguem o campo de categoria de risco recebido da API.")
with tab_data:
    st.subheader("Registros selecionados")
    show_cols = [c for c in ["BAR_CD_SNISB", "BAR_NM_NOME", "ING_NM_MUNICIPIO", "ING_SG_UFMUNICIPIO", "CATEGORIA_RISCO", "DANO_POTENCIAL", "USO_PRINCIPAL", "NM_EMPREENDEDOR", "LATITUDE_MAPA", "LONGITUDE_MAPA", "URL_MAPA"] if c in filtered.columns]
    st.dataframe(filtered[show_cols], use_container_width=True, hide_index=True)
with tab_export:
    st.subheader("Exportação para planejamento de fiscalização in loco")
    st.write("O arquivo Excel respeita os filtros selecionados e contém uma aba **Roteiro de campo**, com coordenadas, link clicável para abrir a localização no Google Maps e campos para registrar observações e resultado da visita. Também inclui a base filtrada e uma legenda das cores.")
    st.warning("A classificação de risco serve para priorização gerencial. A definição da fiscalização deve considerar a diretriz do Confea, os indícios relativos ao exercício profissional e o planejamento do CREA.")
    st.download_button(
        "⬇️ Baixar roteiro de fiscalização em Excel",
        data=excel_bytes(filtered, uf),
        file_name=f"roteiro_fiscalizacao_barragens_{uf.lower()}_{datetime.now():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )
    csv = filtered.drop(columns=["COR_MAPA"], errors="ignore").to_csv(index=False).encode("utf-8-sig")
    st.download_button("Baixar dados filtrados em CSV", data=csv, file_name=f"barragens_snisb_{uf.lower()}.csv", mime="text/csv")
with tab_about:
    st.subheader("Uso recomendado")
    st.markdown("""
- Use a categoria de risco e o dano potencial associado como **critérios de priorização do planejamento**, não como conclusão sobre irregularidade profissional.
- Para fiscalização em campo, confira endereço, coordenadas e condições de acesso antes do deslocamento.
- A planilha exportada inclui uma ficha simples para anotações da equipe.
- A API pode ter campos vazios ou desatualizados; registre a data de extração e confirme informações relevantes diretamente na fonte.
- O CREA fiscaliza o exercício profissional. A avaliação de segurança estrutural e as medidas correspondentes cabem aos órgãos competentes.
""")
st.caption(f"Fonte: SNISB / serviço ArcGIS REST · Consulta executada em {datetime.now():%d/%m/%Y %H:%M}.")

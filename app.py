# -*- coding: utf-8 -*-
"""Painel nacional de barragens do SNISB para planejamento de fiscalização profissional."""
from __future__ import annotations

from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
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


def roteiro_dataframe(filtered: pd.DataFrame, uf: str) -> pd.DataFrame:
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
    for assignment_col in ("Equipe responsável", "Agente fiscal responsável"):
        if assignment_col in df.columns:
            roteiro.insert(0, assignment_col, df[assignment_col].values)
    return roteiro


def format_workbook(filtered: pd.DataFrame, uf: str, include_group_sheets: bool = True) -> bytes:
    roteiro = roteiro_dataframe(filtered, uf)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        roteiro.to_excel(writer, sheet_name="Roteiro de campo", index=False)
        filtered.to_excel(writer, sheet_name="Base filtrada", index=False)
        legenda = pd.DataFrame([
            ["Muito alto", "Vermelho escuro", "Prioridade gerencial visual; validar com fonte oficial."],
            ["Alto", "Vermelho", "Prioridade gerencial visual; validar com fonte oficial."],
            ["Médio", "Laranja", "Prioridade gerencial visual; validar com fonte oficial."],
            ["Baixo", "Verde", "Prioridade gerencial visual; validar com fonte oficial."],
            ["Não informado", "Cinza", "Categoria ausente ou não reconhecida na fonte."],
        ], columns=["Categoria", "Cor no mapa", "Observação"])
        legenda.to_excel(writer, sheet_name="Legenda", index=False)
        if include_group_sheets:
            for group_col in ("Equipe responsável", "Agente fiscal responsável"):
                if group_col in filtered.columns:
                    for group_name, group_df in filtered.groupby(group_col, dropna=False):
                        sheet_name = re.sub(r'[\\/*?:\[\]]', '', str(group_name))[:24] or "Grupo"
                        sheet_name = ("Roteiro " + sheet_name)[:31]
                        roteiro_dataframe(group_df, uf).to_excel(writer, sheet_name=sheet_name, index=False)
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cells in ws.columns:
                max_len = min(max((len(str(c.value)) if c.value is not None else 0 for c in cells), default=10) + 2, 42)
                ws.column_dimensions[cells[0].column_letter].width = max(12, max_len)
            # Links do Google Maps ficam clicáveis no Excel.
            header = {cell.value: cell.column for cell in ws[1]}
            link_col = header.get("Link Google Maps")
            if link_col:
                for row in range(2, ws.max_row + 1):
                    cell = ws.cell(row, link_col)
                    if isinstance(cell.value, str) and cell.value.startswith("https://"):
                        cell.hyperlink = cell.value
                        cell.style = "Hyperlink"
    return output.getvalue()
        normativa = pd.DataFrame([
            ["Documento", "Decisão Plenária Confea nº PL-2516/2025"],
            ["Objeto", "Aprova a Nota Técnica para fiscalização de barragens pelo Sistema Confea/Crea"],
            ["Referência oficial", "https://normativos.confea.org.br/Ementas/Visualizar?id=82626"],
            ["Nota Técnica", "https://www.confea.org.br/midias/uploads-imce/GPF/Nota%20T%C3%A9cnica%20Barragens.pdf"],
            ["Finalidade do painel", "Apoiar o planejamento da fiscalização do exercício profissional"],
            ["Limite de uso", "Os dados do SNISB não comprovam, isoladamente, infração profissional nem segurança ou insegurança estrutural."]
        ], columns=["Item", "Descrição"])

        normativa.to_excel(writer, sheet_name="Base normativa", index=False)

def distribute_by_municipality(df: pd.DataFrame, names: list[str], assignment_col: str) -> pd.DataFrame:
    """Distribui municípios inteiros entre responsáveis, tentando equilibrar a quantidade de barragens."""
    result = df.copy()
    if not names:
        return result
    municipality_col = "ING_NM_MUNICIPIO"
    if municipality_col not in result.columns:
        result[assignment_col] = [names[i % len(names)] for i in range(len(result))]
        return result
    result[municipality_col] = result[municipality_col].fillna("Município não informado").astype(str)
    counts = result[municipality_col].value_counts().sort_values(ascending=False)
    loads = {name: 0 for name in names}
    assignment = {}
    for municipality, count in counts.items():
        target = min(names, key=lambda name: (loads[name], names.index(name)))
        assignment[municipality] = target
        loads[target] += int(count)
    result[assignment_col] = result[municipality_col].map(assignment)
    return result


st.set_page_config(page_title="Fiscalização de Barragens | SNISB", page_icon="🗺️", layout="wide")
st.title("Painel de Fiscalização de Barragens — SNISB")
st.caption("Ferramenta de apoio ao planejamento da fiscalização do exercício profissional. A categoria de risco é informativa e não representa avaliação estrutural do CREA.")
st.info(
    "Base normativa: Decisão Plenária Confea nº PL-2516/2025 — "
    "aprova a Nota Técnica para fiscalização de barragens pelo Sistema Confea/Crea."
)

col1, col2 = st.columns(2)

with col1:
    st.link_button(
        "📄 Consultar decisão PL-2516/2025",
        "https://normativos.confea.org.br/Ementas/Visualizar?id=82626",
        use_container_width=True
    )

with col2:
    st.link_button(
        "📘 Consultar Nota Técnica de Barragens",
        "https://www.confea.org.br/midias/uploads-imce/GPF/Nota%20T%C3%A9cnica%20Barragens.pdf",
        use_container_width=True
    )
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

tab_map, tab_data, tab_export, tab_norma, tab_about = st.tabs([
    "🗺️ Mapa",
    "📋 Dados",
    "📥 Exportar Excel",
    "📚 Base normativa",
    "ℹ️ Orientações"
])
with tab_norma:
    st.header("Base normativa da fiscalização de barragens")

    st.subheader("Decisão Plenária Confea nº PL-2516/2025")

    st.write(
        "A decisão aprova a Nota Técnica que estabelece critérios e "
        "procedimentos técnicos e operacionais para a fiscalização dos "
        "Creas em barragens destinadas à acumulação de água, disposição "
        "de rejeitos e acumulação de resíduos industriais."
    )

    st.markdown("""
    **Aplicação no planejamento da fiscalização:**

    1. Consultar os dados disponíveis no SNISB.
    2. Planejar as ações de fiscalização do exercício profissional.
    3. Verificar a responsabilidade técnica e as atividades profissionais
       relacionadas ao empreendimento.
    4. Solicitar documentos e providências quando necessário.
    5. Registrar os fatos constatados e os elementos comprobatórios
       da fiscalização.

    **Atenção:** a classificação de risco e o dano potencial associado
    são informações para subsidiar o planejamento. Não constituem,
    isoladamente, prova de infração profissional nem avaliação da
    segurança estrutural da barragem.
    """)

    st.link_button(
        "Acessar decisão oficial no Confea",
        "https://normativos.confea.org.br/Ementas/Visualizar?id=82626"
    )

    st.link_button(
        "Abrir Nota Técnica oficial",
        "https://www.confea.org.br/midias/uploads-imce/GPF/Nota%20T%C3%A9cnica%20Barragens.pdf"
    )
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
    st.subheader("Exportação e distribuição dos roteiros de fiscalização")
    st.write("Filtre os dados no menu lateral e escolha se deseja uma planilha única, distribuir por equipes ou atribuir municípios a agentes fiscais. A distribuição mantém cada município com um único responsável e tenta equilibrar a quantidade de barragens.")
    st.warning("A categoria de risco é um critério de planejamento, não prova de infração profissional nem avaliação estrutural do CREA.")
    modo = st.radio("Como deseja organizar a exportação?", ["Planilha única", "Distribuir por equipes", "Distribuir por agente fiscal"], horizontal=True)
    export_df = filtered.drop(columns=["COR_MAPA"], errors="ignore").copy()
    nomes = []
    if modo == "Distribuir por equipes":
        quantidade = st.number_input("Quantidade de equipes", min_value=1, max_value=30, value=3, step=1)
        nomes = [f"Equipe {i}" for i in range(1, int(quantidade) + 1)]
        export_df = distribute_by_municipality(export_df, nomes, "Equipe responsável")
    elif modo == "Distribuir por agente fiscal":
        nomes_texto = st.text_area("Nome dos agentes fiscais (um por linha)", placeholder="Ex.:\nAgente 1\nAgente 2\nAgente 3", height=120)
        nomes = list(dict.fromkeys([n.strip() for n in nomes_texto.splitlines() if n.strip()]))
        if nomes:
            export_df = distribute_by_municipality(export_df, nomes, "Agente fiscal responsável")
            resumo = export_df.groupby("Agente fiscal responsável").size().rename("Barragens atribuídas").reset_index()
            st.caption("Prévia da distribuição")
            st.dataframe(resumo, use_container_width=True, hide_index=True)
        else:
            st.info("Informe os nomes dos agentes para habilitar a distribuição por responsável.")
    excel_data = format_workbook(export_df, uf)
    st.download_button(
        "⬇️ Baixar Excel consolidado com roteiros separados",
        data=excel_data,
        file_name=f"roteiro_fiscalizacao_barragens_{uf.lower()}_{datetime.now():%Y%m%d}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        disabled=(modo == "Distribuir por agente fiscal" and not nomes),
    )
    if modo != "Planilha única" and nomes:
        zip_buffer = BytesIO()
        assignment_col = "Equipe responsável" if modo == "Distribuir por equipes" else "Agente fiscal responsável"
        with ZipFile(zip_buffer, "w", ZIP_DEFLATED) as zf:
            for nome in nomes:
                individual = export_df[export_df[assignment_col] == nome].copy()
                zf.writestr(f"{re.sub(r'[^A-Za-z0-9_-]+', '_', nome)}.xlsx", format_workbook(individual, uf, include_group_sheets=False))
        st.download_button(
            "📦 Baixar um Excel por equipe/agente (ZIP)",
            data=zip_buffer.getvalue(),
            file_name=f"roteiros_por_responsavel_{uf.lower()}_{datetime.now():%Y%m%d}.zip",
            mime="application/zip",
            use_container_width=True,
        )
    csv = export_df.to_csv(index=False).encode("utf-8-sig")
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

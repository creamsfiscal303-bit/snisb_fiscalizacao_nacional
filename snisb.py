from __future__ import annotations

import io
import time
from datetime import datetime
from typing import Any

import pandas as pd
import requests

SERVICE_URL = "https://www.snirh.gov.br/arcgis/rest/services/IG/SNISB/FeatureServer/0"
QUERY_URL = f"{SERVICE_URL}/query"
LOTE_IDS = 500
TIMEOUT = 60
TENTATIVAS = 4
PAUSA = 0.1

ESTADOS = {
    "AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas",
    "BA": "Bahia", "CE": "Ceará", "DF": "Distrito Federal", "ES": "Espírito Santo",
    "GO": "Goiás", "MA": "Maranhão", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul",
    "MG": "Minas Gerais", "PA": "Pará", "PB": "Paraíba", "PR": "Paraná",
    "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
    "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia",
    "RR": "Roraima", "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe",
    "TO": "Tocantins",
}

def _request(params: dict[str, Any]) -> dict[str, Any]:
    last_error = None
    for attempt in range(1, TENTATIVAS + 1):
        try:
            response = requests.get(
                QUERY_URL, params=params, timeout=TIMEOUT,
                headers={"User-Agent": "Painel-Nacional-Fiscalizacao-Barragens/1.0"}
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise RuntimeError(str(data["error"]))
            return data
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt < TENTATIVAS:
                time.sleep(attempt)
    raise RuntimeError(f"Falha ao consultar ArcGIS após {TENTATIVAS} tentativas: {last_error}")

def buscar_barragens(uf: str) -> pd.DataFrame:
    """Obtém todos os IDs da UF e baixa a camada em lotes de objectIds."""
    if uf not in ESTADOS:
        raise ValueError(f"UF inválida: {uf}")

    result = _request({
        "where": f"ING_SG_UFMUNICIPIO = '{uf}'",
        "returnIdsOnly": "true",
        "returnGeometry": "false",
        "f": "json",
    })
    ids = sorted(set(result.get("objectIds") or []))
    if not ids:
        return pd.DataFrame()

    rows = []
    for start in range(0, len(ids), LOTE_IDS):
        batch = ids[start:start + LOTE_IDS]
        data = _request({
            "objectIds": ",".join(str(x) for x in batch),
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "json",
        })
        for feature in data.get("features", []):
            attrs = feature.get("attributes") or {}
            geom = feature.get("geometry") or {}
            if attrs.get("BAR_NU_LONGITUDE") in (None, "") and geom.get("x") is not None:
                attrs["BAR_NU_LONGITUDE"] = geom["x"]
            if attrs.get("BAR_NU_LATITUDE") in (None, "") and geom.get("y") is not None:
                attrs["BAR_NU_LATITUDE"] = geom["y"]
            rows.append(attrs)
        time.sleep(PAUSA)

    df = pd.DataFrame(rows)
    if "ING_SG_UFMUNICIPIO" in df.columns:
        df = df[df["ING_SG_UFMUNICIPIO"].astype(str).str.upper().eq(uf)].copy()

    return df.reset_index(drop=True)

def preparar_dados(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        return df
    date_fields = ["BAR_DT_CADASTRO", "AUT_DT_EMISSAO_PUB", "INS_DT_INSPECAO"]
    for col in date_fields:
        if col in df.columns:
            numeric = pd.to_numeric(df[col], errors="coerce")
            converted = pd.to_datetime(numeric, unit="ms", errors="coerce", utc=True)
            df[col] = converted.dt.strftime("%Y-%m-%d").where(converted.notna(), "")
    for col in ["BAR_NU_LATITUDE", "BAR_NU_LONGITUDE"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["FONTE_DADOS"] = SERVICE_URL
    df["DATA_EXTRAÇÃO"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return df

def preparar_triagem(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    def s(col):
        if col in out:
            return out[col].fillna("").astype(str).str.strip()
        return pd.Series([""] * len(out), index=out.index)
    out["TRIAGEM_RISCO_ALTO"] = s("CATEGORIA_RISCO").str.casefold().eq("alto")
    out["TRIAGEM_DANO_POTENCIAL_ALTO"] = s("DANO_POTENCIAL").str.casefold().eq("alto")
    out["PENDENCIA_EMPREENDEDOR_NAO_INFORMADO"] = s("NM_EMPREENDEDOR").eq("")
    out["PENDENCIA_DATA_INSPECAO_NAO_INFORMADA"] = s("INS_DT_INSPECAO").eq("")
    out["PENDENCIA_PLANO_SEGURANCA_NAO_INFORMADO"] = s("POSSUI_PLANO_SEGURANCA").eq("")
    out["PENDENCIA_PAE_NAO_INFORMADO"] = s("POSSUI_PAE").eq("")
    lat = pd.to_numeric(s("BAR_NU_LATITUDE"), errors="coerce")
    lon = pd.to_numeric(s("BAR_NU_LONGITUDE"), errors="coerce")
    out["PENDENCIA_COORDENADAS"] = lat.isna() | lon.isna()

    def motivos(row):
        labels = [
            ("TRIAGEM_RISCO_ALTO", "Categoria de risco alta — conferir"),
            ("TRIAGEM_DANO_POTENCIAL_ALTO", "Dano potencial alto — conferir"),
            ("PENDENCIA_EMPREENDEDOR_NAO_INFORMADO", "Empreendedor não informado"),
            ("PENDENCIA_DATA_INSPECAO_NAO_INFORMADA", "Data de inspeção não informada"),
            ("PENDENCIA_PLANO_SEGURANCA_NAO_INFORMADO", "Informação de plano de segurança ausente"),
            ("PENDENCIA_PAE_NAO_INFORMADO", "Informação de PAE ausente"),
            ("PENDENCIA_COORDENADAS", "Coordenadas ausentes"),
        ]
        selected = [label for field, label in labels if bool(row.get(field, False))]
        return "; ".join(selected) if selected else "Sem alerta automático; conferir demais dados"
    out["MOTIVOS_PARA_CONFERENCIA"] = out.apply(motivos, axis=1)
    out["OBSERVACAO_TRIAGEM"] = (
        "Indicador automatizado de apoio. Não constitui conclusão de irregularidade."
    )
    return out

def gerar_csv(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")

def gerar_excel(df: pd.DataFrame, triagem: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    resumo = pd.DataFrame([
        {"Indicador": "Total de barragens", "Valor": len(df)},
        {"Indicador": "Categoria de risco alta", "Valor": int(triagem.get("TRIAGEM_RISCO_ALTO", pd.Series(dtype=bool)).sum())},
        {"Indicador": "Dano potencial alto", "Valor": int(triagem.get("TRIAGEM_DANO_POTENCIAL_ALTO", pd.Series(dtype=bool)).sum())},
        {"Indicador": "Extraído em", "Valor": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    ])
    if "ING_NM_MUNICIPIO" in df.columns:
        por_municipio = df.groupby("ING_NM_MUNICIPIO", dropna=False).size().reset_index(name="QUANTIDADE")
        por_municipio = por_municipio.sort_values("QUANTIDADE", ascending=False)
    else:
        por_municipio = pd.DataFrame(columns=["ING_NM_MUNICIPIO", "QUANTIDADE"])
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        resumo.to_excel(writer, sheet_name="Resumo", index=False)
        df.to_excel(writer, sheet_name="Barragens", index=False)
        triagem.to_excel(writer, sheet_name="Triagem", index=False)
        por_municipio.to_excel(writer, sheet_name="Por_Municipio", index=False)
        for ws in writer.book.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col_cells in ws.columns:
                letter = col_cells[0].column_letter
                max_len = max((len(str(c.value or "")) for c in col_cells[:1000]), default=10)
                ws.column_dimensions[letter].width = min(max(max_len + 2, 12), 42)
    return buffer.getvalue()

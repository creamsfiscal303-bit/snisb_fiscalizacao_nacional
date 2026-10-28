# Painel Nacional de Fiscalização de Barragens — SNISB

Aplicação Streamlit para consulta da camada pública de barragens do SNISB, com filtro por UF, indicadores de triagem, mapa e exportação para Excel/CSV.

## Escopo da primeira versão
- Consulta à camada oficial ArcGIS REST do SNISB.
- Seleção de qualquer UF brasileira.
- Paginação por lotes de IDs para contornar o limite de registros por requisição.
- Tratamento de datas e coordenadas.
- Mapa e filtros por município, categoria de risco e dano potencial.
- Exportação de dados filtrados para Excel e CSV.
- Estrutura inicial para cruzamento posterior com cadastro de empresas, profissionais e ARTs.

## Executar localmente no Windows

```powershell
py -m venv .venv
.venv\Scripts\activate
py -m pip install -r requirements.txt
streamlit run app.py
```

Se `py` não funcionar, use `python` no lugar.

## Publicar no GitHub e Streamlit Community Cloud

1. Crie um repositório GitHub, por exemplo `painel-fiscalizacao-barragens`.
2. Envie `app.py`, `snisb.py`, `requirements.txt` e `README.md`.
3. No Streamlit Community Cloud, conecte sua conta GitHub.
4. Escolha o repositório e a branch principal.
5. Informe `app.py` como arquivo principal e faça o deploy.

## Fonte de dados

Camada pública SNISB (ArcGIS REST):
https://www.snirh.gov.br/arcgis/rest/services/IG/SNISB/FeatureServer/0

## Observações importantes

- O SNISB é uma fonte de dados cadastrais; campos vazios devem ser tratados como pendências de conferência, não como prova de irregularidade.
- A aplicação não substitui as atribuições dos órgãos fiscalizadores da segurança de barragens.
- A integração com bases internas de empresas, profissionais e ARTs ainda precisa ser implementada usando fontes autorizadas e credenciais apropriadas, se necessárias.
- Não inserir credenciais, tokens ou dados pessoais restritos no repositório público.
- A correspondência por nome não comprova que uma ART se refere à barragem; objeto, atividade, período e documentos devem ser verificados.

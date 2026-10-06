"""
🍺 Caixa da Rua — vendas de bebidas e conveniência
==================================================
Streamlit + PostgreSQL para vendas ambulantes: estoque, compras em fardo,
frente de caixa (PIX / dinheiro / cartão / fiado), perdas, kits e fechamento.

Como rodar
----------
1. pip install "streamlit>=1.50" psycopg2-binary pandas
2. export DATABASE_URL="postgresql://usuario:senha@host:5432/banco"
   (ou DATABASE_URL em .streamlit/secrets.toml)
3. streamlit run app_bebidas.py
4. No primeiro acesso, clique em "Criar tabelas e cardápio inicial"
   (o script SQL completo está no final deste arquivo: SCHEMA_SQL).

Regras de negócio
-----------------
- Todo estoque é guardado em UNIDADES. Fardos/caixas são convertidos na compra.
- preco_custo = custo por unidade. Na compra, o custo pode ser o custo médio ponderado
  (padrão) ou o último preço pago.
- Venda grava o preço e o custo do momento em itens_venda, então o lucro histórico
  não muda quando você reajusta preços depois.
- Kit vendido: cada item sai do estoque individualmente e o preço do kit é rateado
  entre os itens proporcionalmente ao preço avulso (o total da venda fecha no preço do kit).
- Lucro líquido = lucro das vendas - custo das perdas.
- Fiado entra no faturamento na data da venda e fica "a receber" até ser marcado como recebido.
- Dinheiro esperado no fechamento = fundo de troco + vendas em dinheiro + fiados recebidos em dinheiro
  - retiradas do dia - compras pagas com dinheiro do caixa.
"""

import math
import os

import altair as alt
import pandas as pd
import psycopg2
import psycopg2.extensions
import psycopg2.pool
import streamlit as st

st.set_page_config(page_title="Caixa da Rua", page_icon="🍺", layout="wide")

# --------------------------------------------------------------------------
# CONFIGURAÇÕES
# --------------------------------------------------------------------------
FUSO = "America/Santarem"  # "hoje" e "agora" seguem o horário local, não o do servidor
FORMAS = {"PIX": "💠 PIX", "DINHEIRO": "💵 Dinheiro", "CARTAO": "💳 Cartão", "FIADO": "📒 Fiado"}
ICONES = {"Água": "💧", "Refrigerante": "🥤", "Suco": "🧃", "Energético": "⚡", "Cerveja": "🍺",
          "Destilado": "🥃", "Cigarro": "🚬", "Gelo": "🧊", "Conveniência": "🛍️"}
CATEGORIAS_PADRAO = list(ICONES)
MOTIVOS_PERDA = ["Quebra", "Lata furada / vazou", "Consumo próprio", "Vencido / estragado", "Outro"]
MARGEM_BAIXA = 15.0  # abaixo disso o produto aparece em vermelho

# NUMERIC do PostgreSQL chega como Decimal; convertemos para float
psycopg2.extensions.register_type(
    psycopg2.extensions.new_type(
        psycopg2.extensions.DECIMAL.values, "DEC2FLOAT",
        lambda valor, cursor: float(valor) if valor is not None else None))


# --------------------------------------------------------------------------
# BANCO DE DADOS (pool de conexões, try/except/finally e rollback)
# --------------------------------------------------------------------------
def obter_dsn():
    try:
        return st.secrets["DATABASE_URL"]
    except Exception:
        return os.environ.get("DATABASE_URL")


@st.cache_resource(show_spinner=False)
def obter_pool():
    """Pool criado uma única vez e reaproveitado: evita reconectar a cada clique no celular."""
    dsn = obter_dsn()
    if not dsn:
        raise RuntimeError("Defina a variável de ambiente DATABASE_URL (ou DATABASE_URL em secrets.toml).")
    return psycopg2.pool.ThreadedConnectionPool(1, 5, dsn, connect_timeout=10)


def pegar_conexao(verificar=False):
    """Pega uma conexão do pool. Com verificar=True, testa se ela ainda está viva."""
    pool = obter_pool()
    for _ in range(3):
        conn = pool.getconn()
        if not conn.closed and verificar:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conn.rollback()
            except Exception:
                pool.putconn(conn, close=True)
                continue
        if conn.closed:
            pool.putconn(conn, close=True)
            continue
        return pool, conn
    raise RuntimeError("Não foi possível obter uma conexão com o banco.")


def _rollback(conn):
    try:
        if conn is not None:
            conn.rollback()
    except Exception:
        pass


def consultar(sql, params=None):
    """SELECT -> DataFrame. Tenta de novo uma vez se a conexão tiver caído. Em erro, devolve DataFrame vazio."""
    for tentativa in range(2):
        pool = conn = None
        try:
            pool, conn = pegar_conexao(verificar=(tentativa == 1))
            with conn.cursor() as cur:
                cur.execute(f"SET LOCAL TIME ZONE '{FUSO}'")
                cur.execute(sql, params)
                colunas = [c.name for c in cur.description]
                linhas = cur.fetchall()
            conn.commit()
            return pd.DataFrame(linhas, columns=colunas)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as erro:
            _rollback(conn)
            if conn is not None:
                pool.putconn(conn, close=True)
                conn = None
            if tentativa == 1:
                st.error(f"Sem conexão com o banco de dados: {erro}")
                return pd.DataFrame()
        except Exception as erro:
            _rollback(conn)
            st.error(f"Não foi possível ler o banco de dados: {erro}")
            return pd.DataFrame()
        finally:
            if conn is not None:
                pool.putconn(conn)
    return pd.DataFrame()


def executar_transacao(funcao):
    """
    Roda `funcao(cursor)` em UMA transação: ou grava tudo, ou nada (rollback).
    Retorna (True, resultado) ou (False, mensagem_de_erro).
    """
    pool = conn = None
    try:
        pool, conn = pegar_conexao(verificar=True)
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL TIME ZONE '{FUSO}'")
            resultado = funcao(cur)
        conn.commit()
        return True, resultado
    except Exception as erro:
        _rollback(conn)
        return False, str(erro)
    finally:
        if conn is not None:
            pool.putconn(conn)


# --------------------------------------------------------------------------
# AUXILIARES
# --------------------------------------------------------------------------
def brl(v):
    t = f"{float(v):,.2f}"
    return "R$ " + t.replace(",", "X").replace(".", ",").replace("X", ".")


def avisar(msg):
    st.session_state["_aviso"] = msg


def mostrar_aviso():
    msg = st.session_state.pop("_aviso", None)
    if msg:
        st.toast(msg, icon="✅")


def opcoes_de(df, col_nome, col_id="id"):
    return {str(n): int(i) for n, i in zip(df[col_nome], df[col_id])}


def margem_pct(venda, custo):
    return (venda - custo) / venda * 100 if venda else 0.0


def periodo_sql(coluna, periodo):
    """Filtro de período (só recebe nomes de coluna fixos do código, nunca texto do usuário)."""
    return {"Hoje": f"{coluna}::date = CURRENT_DATE",
            "7 dias": f"{coluna}::date >= CURRENT_DATE - 6",
            "30 dias": f"{coluna}::date >= CURRENT_DATE - 29",
            "Tudo": "TRUE"}[periodo]


def icone(cat):
    return ICONES.get(cat, "📦")


def carregar_produtos(somente_ativos=True):
    return consultar(
        "SELECT id, nome, categoria, preco_custo, preco_venda, estoque_unidades, estoque_minimo, ativo "
        "FROM produtos " + ("WHERE ativo " if somente_ativos else "") + "ORDER BY categoria, nome")


def carregar_kits(somente_ativos=False):
    """Kits com custo atual, preço avulso dos itens e quantos kits ainda dá para montar com o estoque."""
    return consultar(
        f"""
        SELECT c.id, c.nome, c.preco_venda, c.ativo,
               COALESCE(SUM(ci.quantidade * p.preco_custo), 0) AS custo,
               COALESCE(SUM(ci.quantidade * p.preco_venda), 0) AS preco_avulso,
               COALESCE(MIN(p.estoque_unidades / ci.quantidade), 0) AS disponivel,
               COALESCE(STRING_AGG(ci.quantidade::text || 'x ' || p.nome, ' + ' ORDER BY p.nome), '') AS itens
        FROM combos c
        LEFT JOIN combo_itens ci ON ci.combo_id = c.id
        LEFT JOIN produtos p ON p.id = ci.produto_id
        {"WHERE c.ativo" if somente_ativos else ""}
        GROUP BY c.id, c.nome, c.preco_venda, c.ativo
        ORDER BY c.nome
        """)


def grafico_barras(df, x, y, cor, titulo, formato=",.0f"):
    return (alt.Chart(df).mark_bar(color=cor, cornerRadiusEnd=4)
            .encode(x=alt.X(f"{x}:Q", title=titulo),
                    y=alt.Y(f"{y}:N", sort="-x", title=None),
                    tooltip=[alt.Tooltip(f"{y}:N", title="Produto"), alt.Tooltip(f"{x}:Q", title=titulo, format=formato)])
            .properties(width="container", height=max(150, 34 * len(df))))


# --------------------------------------------------------------------------
# ABA 1 — PAINEL
# --------------------------------------------------------------------------
def aba_painel():
    baixos = consultar("SELECT COUNT(*) AS n FROM produtos WHERE ativo AND estoque_unidades <= estoque_minimo")
    if not baixos.empty and int(baixos.loc[0, "n"]) > 0:
        st.warning(f"⚠️ {int(baixos.loc[0, 'n'])} produto(s) com estoque baixo. Veja a aba Estoque.")

    periodo = st.radio("Período", ["Hoje", "7 dias", "30 dias", "Tudo"], horizontal=True, key="painel_periodo")

    v = consultar(f"""SELECT COALESCE(SUM(valor_total), 0) AS fat, COALESCE(SUM(lucro_total), 0) AS lucro,
                             COUNT(*) AS n FROM vendas WHERE {periodo_sql('data_venda', periodo)}""")
    p = consultar(f"SELECT COALESCE(SUM(quantidade * custo_unitario), 0) AS valor FROM perdas "
                  f"WHERE {periodo_sql('data_perda', periodo)}")
    f = consultar("SELECT COALESCE(SUM(valor_total), 0) AS valor FROM vendas "
                  "WHERE forma_pagamento = 'FIADO' AND pago_em IS NULL")
    if v.empty or p.empty or f.empty:
        return
    fat, lucro_bruto, n = float(v.loc[0, "fat"]), float(v.loc[0, "lucro"]), int(v.loc[0, "n"])
    perdas = float(p.loc[0, "valor"])
    lucro_liq = lucro_bruto - perdas

    c1, c2, c3 = st.columns(3)
    c1.metric("Faturamento", brl(fat), border=True)
    c2.metric("Lucro líquido", brl(lucro_liq), border=True,
              help=f"Lucro das vendas ({brl(lucro_bruto)}) menos perdas ({brl(perdas)}).")
    c3.metric("Margem média", f"{(lucro_bruto / fat * 100) if fat else 0:.1f}%", border=True,
              help="Lucro das vendas dividido pelo faturamento.")
    c4, c5 = st.columns(2)
    c4.metric("Nº de vendas", n, border=True)
    c5.metric("Fiado a receber", brl(f.loc[0, "valor"]), border=True, help="Soma de todos os fiados em aberto.")

    janelas = consultar(
        """
        SELECT COALESCE(SUM(valor_total) FILTER (WHERE data_venda::date = CURRENT_DATE), 0) AS hoje,
               COALESCE(SUM(valor_total) FILTER (WHERE data_venda::date >= date_trunc('week', CURRENT_DATE)::date), 0) AS semana,
               COALESCE(SUM(valor_total) FILTER (WHERE data_venda::date >= date_trunc('month', CURRENT_DATE)::date), 0) AS mes
        FROM vendas
        """)
    if not janelas.empty:
        a, b, c = st.columns(3)
        a.metric("Vendas hoje", brl(janelas.loc[0, "hoje"]), border=True)
        b.metric("Na semana", brl(janelas.loc[0, "semana"]), border=True, help="Desde a segunda-feira.")
        c.metric("No mês", brl(janelas.loc[0, "mes"]), border=True)

    st.subheader("Formas de pagamento")
    pag = consultar(f"SELECT forma_pagamento AS forma, SUM(valor_total) AS valor FROM vendas "
                    f"WHERE {periodo_sql('data_venda', periodo)} GROUP BY 1")
    if pag.empty or float(pag["valor"].sum()) == 0:
        st.info("Sem vendas no período.")
    else:
        pag["Forma"] = pag["forma"].map(lambda k: FORMAS.get(k, k))
        rosca = (alt.Chart(pag).mark_arc(innerRadius=60)
                 .encode(theta="valor:Q",
                         color=alt.Color("Forma:N", legend=alt.Legend(orient="bottom", title=None)),
                         tooltip=["Forma:N", alt.Tooltip("valor:Q", title="Valor", format=",.2f")])
                 .properties(width="container", height=280))
        st.altair_chart(rosca)

    top = consultar(
        f"""
        SELECT p.nome, SUM(i.quantidade) AS unidades,
               SUM((i.preco_unitario - i.custo_unitario) * i.quantidade) AS lucro
        FROM itens_venda i
        JOIN vendas v ON v.id = i.venda_id
        JOIN produtos p ON p.id = i.produto_id
        WHERE {periodo_sql('v.data_venda', periodo)}
        GROUP BY p.nome
        """)
    st.subheader("🏆 Mais vendidos")
    if top.empty:
        st.info("Sem vendas no período.")
    else:
        st.altair_chart(grafico_barras(top.nlargest(8, "unidades"), "unidades", "nome", "#0ea5e9", "Unidades"))
        st.subheader("💰 Mais lucrativos")
        st.altair_chart(grafico_barras(top.nlargest(8, "lucro"), "lucro", "nome", "#16a34a", "Lucro (R$)", ",.2f"))


# --------------------------------------------------------------------------
# ABA 2 — ESTOQUE E COMPRAS
# --------------------------------------------------------------------------
def aba_estoque():
    produtos = carregar_produtos(somente_ativos=False)
    t_lista, t_compra, t_novo = st.tabs(["📋 Produtos", "📥 Registrar compra", "➕ Novo produto"])

    # ---------------- lista ----------------
    with t_lista:
        if produtos.empty:
            st.info("Nenhum produto cadastrado.")
        else:
            c1, c2 = st.columns(2)
            cat = c1.selectbox("Categoria", ["Todas"] + sorted(produtos["categoria"].unique()), key="est_cat")
            so_alerta = c2.toggle("Só estoque baixo", key="est_alerta")
            df = produtos if cat == "Todas" else produtos[produtos["categoria"] == cat]

            def situacao(r):
                if not r["ativo"]:
                    return "⚪ Inativo"
                if r["estoque_unidades"] <= 0:
                    return "🔴 Sem estoque"
                if r["estoque_unidades"] <= r["estoque_minimo"]:
                    return "🟡 Baixo"
                return "🟢 OK"

            df = df.assign(Situação=df.apply(situacao, axis=1),
                           **{"Margem (%)": [margem_pct(v, c) for v, c in zip(df["preco_venda"], df["preco_custo"])]})
            if so_alerta:
                df = df[df["Situação"].isin(["🔴 Sem estoque", "🟡 Baixo"])]
            tabela = df.rename(columns={"nome": "Produto", "categoria": "Categoria", "preco_custo": "Custo",
                                        "preco_venda": "Venda", "estoque_unidades": "Estoque",
                                        "estoque_minimo": "Mínimo"})[
                ["Produto", "Categoria", "Custo", "Venda", "Margem (%)", "Estoque", "Mínimo", "Situação"]]

            def colorir(linha):
                cor = {"🔴": "background-color:#fee2e2;color:#991b1b",
                       "🟡": "background-color:#fef3c7;color:#92400e"}.get(linha["Situação"][:1], "")
                return [cor] * len(linha)

            st.dataframe(tabela.style.apply(colorir, axis=1).format(
                {"Custo": "R$ {:.2f}", "Venda": "R$ {:.2f}", "Margem (%)": "{:.1f}%"}), hide_index=True)

            with st.expander("✏️ Editar produto / corrigir contagem"):
                opc = opcoes_de(produtos, "nome")
                nome_ed = st.selectbox("Produto", list(opc), key="ed_prod")
                p = produtos[produtos["id"] == opc[nome_ed]].iloc[0]
                pid = int(p["id"])
                with st.form(f"form_edit_{pid}"):
                    c1, c2 = st.columns(2)
                    novo_nome = c1.text_input("Nome", value=p["nome"])
                    nova_cat = c2.text_input("Categoria", value=p["categoria"])
                    c3, c4 = st.columns(2)
                    custo = c3.number_input("Custo por unidade (R$)", min_value=0.0, value=float(p["preco_custo"]),
                                            step=0.1, format="%.4f")
                    venda = c4.number_input("Preço de venda (R$)", min_value=0.0, value=float(p["preco_venda"]),
                                            step=0.5, format="%.2f")
                    c5, c6 = st.columns(2)
                    estoque = c5.number_input("Estoque atual (unidades)", min_value=0, value=int(p["estoque_unidades"]), step=1)
                    minimo = c6.number_input("Estoque mínimo", min_value=0, value=int(p["estoque_minimo"]), step=1)
                    ativo = st.checkbox("Produto ativo (aparece na frente de caixa)", value=bool(p["ativo"]))
                    salvar = st.form_submit_button("Salvar alterações", type="primary")
                if salvar:
                    ok, res = executar_transacao(lambda cur: cur.execute(
                        """UPDATE produtos SET nome=%s, categoria=%s, preco_custo=%s, preco_venda=%s,
                                  estoque_unidades=%s, estoque_minimo=%s, ativo=%s WHERE id=%s""",
                        (novo_nome.strip(), nova_cat.strip(), custo, venda, int(estoque), int(minimo), ativo, pid)))
                    if ok:
                        avisar(f"{novo_nome.strip()} atualizado.")
                        st.rerun()
                    else:
                        st.error(f"Nada foi salvo. Motivo: {res}")

    # ---------------- compra (conversor de embalagens) ----------------
    with t_compra:
        ativos = produtos[produtos["ativo"]] if not produtos.empty else produtos
        if ativos.empty:
            st.info("Cadastre um produto para registrar compras.")
        else:
            opc = opcoes_de(ativos, "nome")
            nome = st.selectbox("O que você comprou?", list(opc), key="compra_prod")
            prod = ativos[ativos["id"] == opc[nome]].iloc[0]
            pid = int(prod["id"])
            c1, c2, c3 = st.columns(3)
            qtd_emb = c1.number_input("Quantas caixas/fardos?", min_value=1, value=1, step=1, key="compra_qtd")
            upe = c2.number_input("Unidades por caixa/fardo", min_value=1, value=12, step=1, key="compra_upe",
                                  help="Ex.: caixa de cerveja com 12 ou 24 latas; fardo de água com 12. Para compra avulsa, use 1.")
            valor_emb = c3.number_input("Valor pago por caixa/fardo (R$)", min_value=0.0, value=0.0, step=1.0,
                                        format="%.2f", key="compra_valor")
            pago_com = st.radio("Quem pagou?", ["CAIXA", "BOLSO"], horizontal=True, key="compra_pago",
                                format_func=lambda x: "💵 Dinheiro do caixa" if x == "CAIXA" else "👤 Do meu bolso (vira aporte)")
            metodo = st.radio("Como calcular o novo custo?", ["Custo médio", "Último preço pago"], horizontal=True,
                              key="compra_metodo")

            entrada = int(qtd_emb) * int(upe)
            total = float(qtd_emb) * valor_emb
            custo_un = total / entrada if entrada else 0.0
            est_atual, custo_atual = int(prod["estoque_unidades"]), float(prod["preco_custo"])
            if metodo == "Custo médio" and est_atual > 0:
                custo_final = (est_atual * custo_atual + total) / (est_atual + entrada)
            else:
                custo_final = custo_un

            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Entram no estoque", f"{entrada} un", border=True)
            m2.metric("Custo por unidade", brl(custo_un), border=True)
            m3.metric("Novo custo do produto", brl(custo_final), border=True)
            m4.metric("Margem no preço atual", f"{margem_pct(float(prod['preco_venda']), custo_final):.1f}%", border=True)
            st.caption(f"Total da compra: {brl(total)}. Estoque vai de {est_atual} para {est_atual + entrada} unidades.")

            if st.button("Registrar compra", type="primary", disabled=valor_emb <= 0, key="compra_btn"):
                def registrar(cur):
                    cur.execute("SELECT estoque_unidades, preco_custo FROM produtos WHERE id = %s FOR UPDATE", (pid,))
                    est, custo_db = cur.fetchone()
                    novo = round((est * custo_db + total) / (est + entrada), 4) \
                        if (metodo == "Custo médio" and est > 0) else round(custo_un, 4)
                    cur.execute("UPDATE produtos SET estoque_unidades = estoque_unidades + %s, preco_custo = %s WHERE id = %s",
                                (entrada, novo, pid))
                    cur.execute("""INSERT INTO compras (produto_id, qtd_embalagens, unidades_por_embalagem, valor_total, pago_com)
                                   VALUES (%s, %s, %s, %s, %s)""", (pid, int(qtd_emb), int(upe), total, pago_com))
                    if pago_com == "BOLSO":
                        cur.execute("INSERT INTO movimentacoes_caixa (tipo, valor, descricao) VALUES ('APORTE', %s, %s)",
                                    (total, f"Compra: {int(qtd_emb)}x {nome} ({entrada} un)"))

                ok, res = executar_transacao(registrar)
                if ok:
                    avisar(f"Compra registrada: +{entrada} unidades de {nome}.")
                    st.rerun()
                else:
                    st.error(f"Nada foi salvo. Motivo: {res}")

    # ---------------- novo produto ----------------
    with t_novo:
        categorias = sorted(set(CATEGORIAS_PADRAO) | set(produtos["categoria"] if not produtos.empty else []))
        with st.form("form_novo_produto", clear_on_submit=True):
            c1, c2 = st.columns(2)
            nome = c1.text_input("Nome do produto")
            cat = c2.selectbox("Categoria", categorias)
            outra = st.text_input("Ou digite uma categoria nova (opcional)")
            c3, c4 = st.columns(2)
            custo = c3.number_input("Custo por unidade (R$)", min_value=0.0, step=0.1, format="%.4f")
            venda = c4.number_input("Preço de venda (R$)", min_value=0.0, step=0.5, format="%.2f")
            c5, c6 = st.columns(2)
            estoque = c5.number_input("Estoque inicial (unidades)", min_value=0, step=1)
            minimo = c6.number_input("Estoque mínimo", min_value=0, step=1)
            enviar = st.form_submit_button("Cadastrar produto", type="primary")
        if enviar:
            if not nome.strip():
                st.error("Informe o nome do produto.")
            else:
                ok, res = executar_transacao(lambda cur: cur.execute(
                    """INSERT INTO produtos (nome, categoria, preco_custo, preco_venda, estoque_unidades, estoque_minimo)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (nome.strip(), outra.strip() or cat, custo, venda, int(estoque), int(minimo))))
                if ok:
                    avisar(f"{nome.strip()} cadastrado.")
                    st.rerun()
                else:
                    st.error(f"Nada foi salvo. Motivo: {res}")


# --------------------------------------------------------------------------
# ABA 3 — FRENTE DE CAIXA
# --------------------------------------------------------------------------
def aba_caixa():
    sub_vender, sub_perdas, sub_fiados = st.tabs(["🛒 Vender", "💥 Perdas", "📒 Fiados"])
    with sub_vender:
        vender()
    with sub_perdas:
        perdas()
    with sub_fiados:
        fiados()


def vender():
    produtos = carregar_produtos()
    kits = carregar_kits(somente_ativos=True)
    if produtos.empty:
        st.info("Cadastre produtos na aba Estoque.")
        return

    # A "versão" muda depois de cada venda: novos widgets nascem zerados (limpa o carrinho)
    ver = st.session_state.setdefault("_cx_ver", 0)
    k_prod = lambda i: f"cx_{ver}_p{i}"   # noqa: E731
    k_kit = lambda i: f"cx_{ver}_k{i}"    # noqa: E731

    # Os valores dos widgets já estão no session_state antes de eles serem desenhados,
    # então dá para mostrar o total no topo.
    carrinho = {int(r.id): int(st.session_state.get(k_prod(r.id), 0) or 0) for r in produtos.itertuples()}
    carrinho = {i: q for i, q in carrinho.items() if q > 0}
    carrinho_kits = {}
    if not kits.empty:
        carrinho_kits = {int(r.id): int(st.session_state.get(k_kit(r.id), 0) or 0) for r in kits.itertuples()}
        carrinho_kits = {i: q for i, q in carrinho_kits.items() if q > 0}

    preco_prod = dict(zip(produtos["id"].astype(int), produtos["preco_venda"]))
    preco_kit = dict(zip(kits["id"].astype(int), kits["preco_venda"])) if not kits.empty else {}
    total = sum(q * preco_prod[i] for i, q in carrinho.items()) + sum(q * preco_kit[i] for i, q in carrinho_kits.items())
    itens_total = sum(carrinho.values()) + sum(carrinho_kits.values())
    st.metric(f"🛒 Carrinho ({itens_total} itens)", brl(total), border=True)

    categorias = list(dict.fromkeys(produtos["categoria"]))
    rotulos = [f"{icone(c)} {c}" for c in categorias] + (["🎁 Kits"] if not kits.empty else [])
    abas = st.tabs(rotulos)
    for aba, cat in zip(abas, categorias):
        with aba:
            for r in produtos[produtos["categoria"] == cat].itertuples():
                est = int(r.estoque_unidades)
                qtd = st.number_input(f"{r.nome} · {brl(r.preco_venda)} · estoque {est}", min_value=0, step=1,
                                      key=k_prod(r.id), disabled=(est <= 0))
                if qtd > est:
                    st.caption(f":red[Só há {est} em estoque.]")
    if not kits.empty:
        with abas[-1]:
            for r in kits.itertuples():
                disp = int(r.disponivel)
                qtd = st.number_input(f"{r.nome} · {brl(r.preco_venda)} · dá para {disp}", min_value=0, step=1,
                                      key=k_kit(r.id), disabled=(disp <= 0))
                st.caption(r.itens)
                if qtd > disp:
                    st.caption(f":red[Estoque só dá para {disp} kit(s).]")

    # ---- resumo e fechamento da venda ----
    st.divider()
    if itens_total == 0:
        st.caption("Escolha os itens acima. O total aparece no topo.")
        return
    linhas = [{"Item": p, "Qtd": q, "Subtotal": q * preco_prod[i]}
              for i, q in carrinho.items() for p in [produtos.loc[produtos["id"] == i, "nome"].iloc[0]]]
    linhas += [{"Item": "🎁 " + kits.loc[kits["id"] == i, "nome"].iloc[0], "Qtd": q, "Subtotal": q * preco_kit[i]}
               for i, q in carrinho_kits.items()]
    st.dataframe(pd.DataFrame(linhas), hide_index=True,
                 column_config={"Subtotal": st.column_config.NumberColumn(format="R$ %.2f")})

    forma = st.radio("Forma de pagamento", list(FORMAS), format_func=FORMAS.get, horizontal=True, key="cx_forma")
    cliente = ""
    if forma == "FIADO":
        cliente = st.text_input("Nome do cliente", key=f"cx_cliente_{ver}")
    if forma == "DINHEIRO":
        recebido = st.number_input("Valor recebido (R$)", min_value=0.0, step=5.0, format="%.2f", key=f"cx_receb_{ver}")
        if recebido >= total > 0:
            st.info(f"Troco: **{brl(recebido - total)}**")
    obs = st.text_input("Observação (opcional)", key=f"cx_obs_{ver}")

    if st.button("✅ Confirmar venda", type="primary", key="cx_confirmar"):
        if forma == "FIADO" and not cliente.strip():
            st.error("Informe o nome do cliente para vender fiado.")
            return

        def confirmar(cur):
            # 1) kits: itens e preço de cada um
            componentes = {}
            preco_kit_db = {}
            if carrinho_kits:
                cur.execute("""SELECT c.id, c.preco_venda, ci.produto_id, ci.quantidade
                               FROM combos c JOIN combo_itens ci ON ci.combo_id = c.id WHERE c.id = ANY(%s)""",
                            (list(carrinho_kits),))
                for kid, kpreco, pid, q in cur.fetchall():
                    preco_kit_db[kid] = kpreco
                    componentes.setdefault(kid, []).append((pid, q))

            # 2) quanto de cada produto a venda consome (avulso + dentro dos kits)
            demanda = dict(carrinho)
            for kid, n in carrinho_kits.items():
                for pid, q in componentes.get(kid, []):
                    demanda[pid] = demanda.get(pid, 0) + n * q

            # 3) trava os produtos (ordem fixa evita deadlock) e confere o estoque
            cur.execute("""SELECT id, nome, preco_custo, preco_venda, estoque_unidades FROM produtos
                           WHERE id = ANY(%s) ORDER BY id FOR UPDATE""", (list(demanda),))
            dados = {pid: (nome, custo, preco, est) for pid, nome, custo, preco, est in cur.fetchall()}
            faltam = [f"{dados[i][0]} (tem {dados[i][3]}, precisa {q})" for i, q in demanda.items() if dados[i][3] < q]
            if faltam:
                raise ValueError("Estoque insuficiente: " + "; ".join(faltam))

            # 4) linhas da venda: (produto, qtd, preço unitário, custo unitário, kit)
            itens, valor_total = [], 0.0
            for pid, q in carrinho.items():
                _, custo, preco, _ = dados[pid]
                itens.append((pid, q, preco, custo, None))
                valor_total += q * preco
            for kid, n in carrinho_kits.items():
                ref = sum(q * dados[pid][2] for pid, q in componentes[kid])  # preço avulso dos itens
                for pid, q in componentes[kid]:
                    _, custo, preco, _ = dados[pid]
                    unit = preco_kit_db[kid] * preco / ref if ref else preco_kit_db[kid] / sum(x for _, x in componentes[kid])
                    itens.append((pid, n * q, round(unit, 4), custo, kid))
                valor_total += n * preco_kit_db[kid]
            custo_total = sum(q * c for _, q, _, c, _ in itens)

            # 5) grava a venda, os itens e baixa o estoque
            cur.execute("""INSERT INTO vendas (forma_pagamento, valor_total, lucro_total, observacao, cliente)
                           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                        (forma, round(valor_total, 2), round(valor_total - custo_total, 2),
                         obs.strip() or None, cliente.strip() or None))
            venda_id = cur.fetchone()[0]
            for pid, q, unit, custo, kid in itens:
                cur.execute("""INSERT INTO itens_venda (venda_id, produto_id, quantidade, preco_unitario, custo_unitario, combo_id)
                               VALUES (%s, %s, %s, %s, %s, %s)""", (venda_id, pid, q, unit, custo, kid))
            for pid, q in demanda.items():
                cur.execute("UPDATE produtos SET estoque_unidades = estoque_unidades - %s WHERE id = %s", (q, pid))
            return valor_total

        ok, res = executar_transacao(confirmar)
        if ok:
            st.session_state["_cx_ver"] = ver + 1  # zera o carrinho
            avisar(f"Venda de {brl(res)} registrada ({FORMAS[forma]}).")
            st.rerun()
        else:
            st.error(f"Venda NÃO registrada. Motivo: {res}")


def perdas():
    produtos = carregar_produtos()
    if produtos.empty:
        st.info("Cadastre produtos primeiro.")
        return
    opc = opcoes_de(produtos, "nome")
    with st.form("form_perda", clear_on_submit=True):
        nome = st.selectbox("Produto", list(opc))
        c1, c2 = st.columns(2)
        qtd = c1.number_input("Quantidade", min_value=1, value=1, step=1)
        motivo = c2.selectbox("Motivo", MOTIVOS_PERDA)
        enviar = st.form_submit_button("Registrar perda", type="primary")
    if enviar:
        pid = opc[nome]

        def registrar(cur):
            cur.execute("SELECT estoque_unidades, preco_custo FROM produtos WHERE id = %s FOR UPDATE", (pid,))
            est, custo = cur.fetchone()
            if qtd > est:
                raise ValueError(f"Só há {est} unidades de {nome} em estoque.")
            cur.execute("UPDATE produtos SET estoque_unidades = estoque_unidades - %s WHERE id = %s", (int(qtd), pid))
            cur.execute("INSERT INTO perdas (produto_id, quantidade, motivo, custo_unitario) VALUES (%s, %s, %s, %s)",
                        (pid, int(qtd), motivo, custo))

        ok, res = executar_transacao(registrar)
        if ok:
            avisar(f"Perda registrada: {int(qtd)}x {nome} ({motivo}).")
            st.rerun()
        else:
            st.error(f"Nada foi salvo. Motivo: {res}")

    hist = consultar(
        """SELECT pe.data_perda AS "Data", p.nome AS "Produto", pe.quantidade AS "Qtd", pe.motivo AS "Motivo",
                  pe.quantidade * pe.custo_unitario AS "Prejuízo"
           FROM perdas pe JOIN produtos p ON p.id = pe.produto_id ORDER BY pe.data_perda DESC LIMIT 15""")
    st.subheader("Últimas perdas")
    if hist.empty:
        st.caption("Nenhuma perda registrada.")
    else:
        st.dataframe(hist, hide_index=True, column_config={
            "Data": st.column_config.DatetimeColumn(format="DD/MM HH:mm"),
            "Prejuízo": st.column_config.NumberColumn(format="R$ %.2f")})


def fiados():
    abertos = consultar("""SELECT id, data_venda, cliente, valor_total FROM vendas
                           WHERE forma_pagamento = 'FIADO' AND pago_em IS NULL ORDER BY data_venda""")
    if abertos.empty:
        st.success("Nenhum fiado em aberto.")
        return
    st.metric("Total a receber", brl(abertos["valor_total"].sum()), border=True)
    rotulos = {f"{r.data_venda:%d/%m} · {r.cliente or 'Sem nome'} · {brl(r.valor_total)}": int(r.id)
               for r in abertos.itertuples()}
    escolha = st.selectbox("Qual fiado foi pago?", list(rotulos), key="fiado_sel")
    forma = st.radio("Recebido em", ["PIX", "DINHEIRO", "CARTAO"], horizontal=True,
                     format_func=FORMAS.get, key="fiado_forma")
    if st.button("Marcar como recebido", type="primary", key="fiado_btn"):
        ok, res = executar_transacao(lambda cur: cur.execute(
            """UPDATE vendas SET pago_em = NOW(), forma_recebimento = %s
               WHERE id = %s AND forma_pagamento = 'FIADO' AND pago_em IS NULL""", (forma, rotulos[escolha])))
        if ok:
            avisar("Fiado recebido.")
            st.rerun()
        else:
            st.error(f"Nada foi salvo. Motivo: {res}")


# --------------------------------------------------------------------------
# ABA 4 — PRECIFICAÇÃO E KITS
# --------------------------------------------------------------------------
def aba_precos():
    produtos = carregar_produtos()
    st.subheader("Margem por produto")
    if produtos.empty:
        st.info("Cadastre produtos para ver as margens.")
    else:
        alvo = st.slider("Margem desejada (%)", 5, 60, 30, key="margem_alvo")
        df = produtos[["id", "nome", "categoria", "preco_custo", "preco_venda"]].copy()
        df["Lucro (R$)"] = df["preco_venda"] - df["preco_custo"]
        df["Margem (%)"] = [margem_pct(v, c) for v, c in zip(df["preco_venda"], df["preco_custo"])]
        df["Preço sugerido"] = [math.ceil(c / (1 - alvo / 100) * 2) / 2 for c in df["preco_custo"]]  # arredonda p/ R$ 0,50
        df["Saúde"] = df["Margem (%)"].map(lambda m: "🔴" if m < MARGEM_BAIXA else "🟡" if m < alvo else "🟢")
        df = df.rename(columns={"nome": "Produto", "categoria": "Categoria", "preco_custo": "Custo",
                                "preco_venda": "Preço de venda"})
        editado = st.data_editor(
            df[["id", "Produto", "Categoria", "Custo", "Preço de venda", "Lucro (R$)", "Margem (%)", "Preço sugerido", "Saúde"]],
            hide_index=True, key="editor_precos",
            disabled=["id", "Produto", "Categoria", "Custo", "Lucro (R$)", "Margem (%)", "Preço sugerido", "Saúde"],
            column_config={"id": None,
                           "Custo": st.column_config.NumberColumn(format="R$ %.2f"),
                           "Preço de venda": st.column_config.NumberColumn(format="R$ %.2f", min_value=0.0, step=0.5),
                           "Lucro (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
                           "Margem (%)": st.column_config.NumberColumn(format="%.1f%%"),
                           "Preço sugerido": st.column_config.NumberColumn(format="R$ %.2f")})
        st.caption("Edite a coluna “Preço de venda” e salve. 🔴 margem abaixo de "
                   f"{MARGEM_BAIXA:.0f}% · 🟡 abaixo da meta · 🟢 na meta. Lucro = preço de venda − custo.")
        antigos = dict(zip(df["id"].astype(int), df["Preço de venda"]))
        mudancas = [(float(r["Preço de venda"]), int(r["id"])) for r in editado.to_dict("records")
                    if abs(float(r["Preço de venda"]) - antigos[int(r["id"])]) > 0.004]
        if st.button(f"Salvar preços ({len(mudancas)} alterado(s))", disabled=not mudancas, key="salvar_precos"):
            def salvar(cur):
                for preco, pid in mudancas:
                    cur.execute("UPDATE produtos SET preco_venda = %s WHERE id = %s", (preco, pid))

            ok, res = executar_transacao(salvar)
            if ok:
                avisar("Preços atualizados.")
                st.rerun()
            else:
                st.error(f"Nada foi salvo. Motivo: {res}")

    # ---------------- kits ----------------
    st.subheader("🎁 Kits e baldes")
    kv = st.session_state.setdefault("_kit_ver", 0)
    if not produtos.empty:
        opc = opcoes_de(produtos, "nome")
        nome_kit = st.text_input("Nome do kit", placeholder="Balde 5 cervejas + gelo", key=f"kit_nome_{kv}")
        escolhidos = st.multiselect("Itens do kit", list(opc), key=f"kit_itens_{kv}")
        qtds = {n: st.number_input(f"Quantidade de {n}", min_value=1, value=1, step=1, key=f"kit_q_{kv}_{opc[n]}")
                for n in escolhidos}
        preco_kit = st.number_input("Preço do kit (R$)", min_value=0.0, step=1.0, format="%.2f", key=f"kit_preco_{kv}")

        info = produtos.set_index("nome")
        custo = sum(q * float(info.loc[n, "preco_custo"]) for n, q in qtds.items())
        avulso = sum(q * float(info.loc[n, "preco_venda"]) for n, q in qtds.items())
        lucro = preco_kit - custo
        a, b, c, d = st.columns(4)
        a.metric("Custo do kit", brl(custo), border=True)
        b.metric("Preço avulso dos itens", brl(avulso), border=True)
        c.metric("Lucro do kit", brl(lucro), border=True)
        d.metric("Margem do kit", f"{margem_pct(preco_kit, custo):.1f}%", border=True,
                 delta=f"desconto de {brl(avulso - preco_kit)}" if preco_kit and avulso > preco_kit else None)
        if preco_kit and preco_kit < custo:
            st.error("Atenção: o preço do kit está abaixo do custo. Você teria prejuízo em cada venda.")

        if st.button("Salvar kit", type="primary", key="kit_salvar",
                     disabled=not (nome_kit.strip() and qtds and preco_kit > 0)):
            def salvar_kit(cur):
                cur.execute("INSERT INTO combos (nome, preco_venda) VALUES (%s, %s) RETURNING id", (nome_kit.strip(), preco_kit))
                kid = cur.fetchone()[0]
                for n, q in qtds.items():
                    cur.execute("INSERT INTO combo_itens (combo_id, produto_id, quantidade) VALUES (%s, %s, %s)",
                                (kid, opc[n], int(q)))

            ok, res = executar_transacao(salvar_kit)
            if ok:
                st.session_state["_kit_ver"] = kv + 1  # limpa o formulário
                avisar(f"Kit {nome_kit.strip()} salvo.")
                st.rerun()
            else:
                st.error(f"Nada foi salvo. Motivo: {res}")

    kits = carregar_kits()
    if kits.empty:
        st.caption("Nenhum kit cadastrado ainda.")
        return
    kits["lucro"] = kits["preco_venda"] - kits["custo"]
    kits["margem"] = [margem_pct(p, c) for p, c in zip(kits["preco_venda"], kits["custo"])]
    st.markdown("**Kits cadastrados**")
    tabela = kits.rename(columns={"nome": "Kit", "preco_venda": "Preço", "custo": "Custo", "lucro": "Lucro",
                                  "margem": "Margem (%)", "disponivel": "Dá para", "itens": "Itens"})
    tabela["Ativo"] = kits["ativo"].map({True: "✅", False: "⏸️"})
    st.dataframe(tabela[["Kit", "Itens", "Preço", "Custo", "Lucro", "Margem (%)", "Dá para", "Ativo"]], hide_index=True,
                 column_config={"Preço": st.column_config.NumberColumn(format="R$ %.2f"),
                                "Custo": st.column_config.NumberColumn(format="R$ %.2f"),
                                "Lucro": st.column_config.NumberColumn(format="R$ %.2f"),
                                "Margem (%)": st.column_config.NumberColumn(format="%.1f%%")})
    opc_kit = opcoes_de(kits, "nome")
    sel = st.selectbox("Ativar ou pausar um kit", list(opc_kit), key="kit_toggle_sel")
    ativo_atual = bool(kits.loc[kits["id"] == opc_kit[sel], "ativo"].iloc[0])
    if st.button("⏸️ Pausar kit" if ativo_atual else "▶️ Reativar kit", key="kit_toggle_btn"):
        ok, res = executar_transacao(lambda cur: cur.execute(
            "UPDATE combos SET ativo = NOT ativo WHERE id = %s", (opc_kit[sel],)))
        if ok:
            avisar(f"Kit {sel}: {'pausado' if ativo_atual else 'reativado'}.")
            st.rerun()
        else:
            st.error(f"Nada foi salvo. Motivo: {res}")


# --------------------------------------------------------------------------
# ABA 5 — FECHAMENTO DE CAIXA E SÓCIOS
# --------------------------------------------------------------------------
def soma(df, col_chave, chave, col_valor="total"):
    if df.empty:
        return 0.0
    linha = df[df[col_chave] == chave]
    return float(linha[col_valor].sum()) if not linha.empty else 0.0


def aba_fechamento():
    hoje = consultar("SELECT CURRENT_DATE AS d")
    if hoje.empty:
        return
    st.subheader("Conciliação do dia")
    dia = st.date_input("Dia", value=hoje.loc[0, "d"], format="DD/MM/YYYY", key="fech_dia")

    vendas_dia = consultar("SELECT forma_pagamento AS forma, SUM(valor_total) AS total, COUNT(*) AS n "
                           "FROM vendas WHERE data_venda::date = %s GROUP BY 1", (dia,))
    fiado_rec = consultar("SELECT forma_recebimento AS forma, SUM(valor_total) AS total FROM vendas "
                          "WHERE forma_pagamento = 'FIADO' AND pago_em::date = %s GROUP BY 1", (dia,))
    movs_dia = consultar("SELECT tipo, SUM(valor) AS total FROM movimentacoes_caixa "
                         "WHERE data_movimento::date = %s GROUP BY 1", (dia,))
    compras_caixa = consultar("SELECT COALESCE(SUM(valor_total), 0) AS total FROM compras "
                              "WHERE pago_com = 'CAIXA' AND data_compra::date = %s", (dia,))

    v_pix, v_din, v_car = (soma(vendas_dia, "forma", k) for k in ("PIX", "DINHEIRO", "CARTAO"))
    v_fiado = soma(vendas_dia, "forma", "FIADO")
    f_pix, f_din, f_car = (soma(fiado_rec, "forma", k) for k in ("PIX", "DINHEIRO", "CARTAO"))
    retiradas = soma(movs_dia, "tipo", "RETIRADA")
    gasto_compras = float(compras_caixa.loc[0, "total"]) if not compras_caixa.empty else 0.0

    fundo = st.number_input("Fundo de troco (dinheiro que já estava no bolso ao abrir)", min_value=0.0, step=10.0,
                            format="%.2f", key="fech_fundo")
    esp_din = fundo + v_din + f_din - retiradas - gasto_compras
    esp_pix = v_pix + f_pix
    esp_car = v_car + f_car

    a, b, c = st.columns(3)
    a.metric("💵 Dinheiro esperado no bolso", brl(esp_din), border=True,
             help=f"Fundo {brl(fundo)} + vendas {brl(v_din)} + fiados recebidos {brl(f_din)} "
                  f"− retiradas {brl(retiradas)} − compras do caixa {brl(gasto_compras)}.")
    b.metric("💠 PIX esperado na conta", brl(esp_pix), border=True, help="Vendas em PIX + fiados recebidos em PIX.")
    c.metric("💳 Cartão esperado (maquininha)", brl(esp_car), border=True)
    if v_fiado:
        st.caption(f"Vendido no fiado hoje (não entra no caixa): {brl(v_fiado)}.")

    st.markdown("**Conferência**")
    c1, c2 = st.columns(2)
    contado = c1.number_input("Dinheiro contado no bolso (R$)", min_value=0.0, step=1.0, format="%.2f", key="fech_contado")
    pix_conf = c2.number_input("PIX conferido no app do banco (R$)", min_value=0.0, step=1.0, format="%.2f", key="fech_pix")
    for rotulo, contado_v, esperado in (("Dinheiro", contado, esp_din), ("PIX", pix_conf, esp_pix)):
        dif = round(contado_v - esperado, 2)
        if contado_v == 0 and esperado != 0:
            st.caption(f"{rotulo}: informe o valor conferido para comparar.")
        elif abs(dif) < 0.01:
            st.success(f"{rotulo}: bateu certinho.")
        elif dif > 0:
            st.warning(f"{rotulo}: sobrando {brl(dif)}. Confira troco dado a menos ou venda sem registro.")
        else:
            st.error(f"{rotulo}: faltando {brl(-dif)}. Confira troco, vendas sem registro ou retirada não lançada.")

    # ---------------- aportes e retiradas ----------------
    st.subheader("Aportes e retiradas")
    with st.form("form_mov", clear_on_submit=True):
        c1, c2 = st.columns(2)
        tipo = c1.radio("Tipo", ["APORTE", "RETIRADA"], horizontal=True,
                        format_func=lambda t: "💸 Aporte (pus dinheiro)" if t == "APORTE" else "🏦 Retirada / sangria")
        valor = c2.number_input("Valor (R$)", min_value=0.01, value=50.0, step=5.0, format="%.2f")
        descricao = st.text_input("Descrição", placeholder="Ex.: troco inicial, retirada de lucro, compra de gelo")
        enviar = st.form_submit_button("Registrar", type="primary")
    if enviar:
        ok, res = executar_transacao(lambda cur: cur.execute(
            "INSERT INTO movimentacoes_caixa (tipo, valor, descricao) VALUES (%s, %s, %s)",
            (tipo, valor, descricao.strip() or None)))
        if ok:
            avisar(f"{'Aporte' if tipo == 'APORTE' else 'Retirada'} de {brl(valor)} registrado(a).")
            st.rerun()
        else:
            st.error(f"Nada foi salvo. Motivo: {res}")

    tot = consultar("SELECT tipo, SUM(valor) AS total FROM movimentacoes_caixa GROUP BY 1")
    lucro = consultar("SELECT (SELECT COALESCE(SUM(lucro_total), 0) FROM vendas) - "
                      "(SELECT COALESCE(SUM(quantidade * custo_unitario), 0) FROM perdas) AS lucro")
    aportes, retirado = soma(tot, "tipo", "APORTE"), soma(tot, "tipo", "RETIRADA")
    lucro_liq = float(lucro.loc[0, "lucro"]) if not lucro.empty else 0.0
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Aportes (total)", brl(aportes), border=True)
    m2.metric("Retiradas (total)", brl(retirado), border=True)
    m3.metric("Lucro líquido acumulado", brl(lucro_liq), border=True)
    m4.metric("Lucro ainda não retirado", brl(lucro_liq - retirado), border=True,
              help="Lucro líquido acumulado menos tudo que já foi retirado.")

    extrato = consultar("""SELECT data_movimento AS "Data", tipo AS "Tipo", valor AS "Valor", descricao AS "Descrição"
                           FROM movimentacoes_caixa ORDER BY data_movimento DESC, id DESC LIMIT 30""")
    if not extrato.empty:
        st.dataframe(extrato, hide_index=True, column_config={
            "Data": st.column_config.DatetimeColumn(format="DD/MM HH:mm"),
            "Valor": st.column_config.NumberColumn(format="R$ %.2f")})


# --------------------------------------------------------------------------
# EXECUÇÃO PRINCIPAL
# --------------------------------------------------------------------------
def banco_pronto():
    df = consultar("SELECT to_regclass('public.combos') IS NOT NULL AND to_regclass('public.compras') IS NOT NULL "
                   "AND to_regclass('public.itens_venda') IS NOT NULL AS pronto")
    if df.empty:
        st.stop()  # conexão falhou; o erro já foi mostrado
    return bool(df.loc[0, "pronto"])


def main():
    st.title("🍺 Caixa da Rua")
    mostrar_aviso()

    if not banco_pronto():
        st.warning("O banco ainda não tem as tabelas do app.")
        st.caption("O script só cria o que falta e só insere produtos que ainda não existem.")
        if st.button("Criar tabelas e cardápio inicial", type="primary"):
            ok, res = executar_transacao(lambda cur: cur.execute(SCHEMA_SQL))
            if ok:
                avisar("Banco criado com o cardápio inicial.")
                st.rerun()
            else:
                st.error(f"Não foi possível criar as tabelas: {res}")
        st.stop()

    t1, t2, t3, t4, t5 = st.tabs(["📊 Painel", "📦 Estoque", "🛒 Caixa", "🏷️ Preços e kits", "💼 Fechamento"])
    with t1:
        aba_painel()
    with t2:
        aba_estoque()
    with t3:
        aba_caixa()
    with t4:
        aba_precos()
    with t5:
        aba_fechamento()


# --------------------------------------------------------------------------
# SCRIPT SQL: ESTRUTURA + CARDÁPIO INICIAL (idempotente)
# Também pode ser rodado direto no psql / pgAdmin / DBeaver.
# Preços, custos e estoques do cardápio são EXEMPLOS: ajuste na aba Estoque.
# --------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS produtos (
    id                SERIAL PRIMARY KEY,
    nome              VARCHAR(120) NOT NULL UNIQUE,
    categoria         VARCHAR(40)  NOT NULL,
    preco_custo       NUMERIC(10,4) NOT NULL DEFAULT 0 CHECK (preco_custo >= 0),   -- por UNIDADE
    preco_venda       NUMERIC(10,2) NOT NULL DEFAULT 0 CHECK (preco_venda >= 0),
    estoque_unidades  INTEGER NOT NULL DEFAULT 0 CHECK (estoque_unidades >= 0),
    estoque_minimo    INTEGER NOT NULL DEFAULT 0 CHECK (estoque_minimo >= 0),
    ativo             BOOLEAN NOT NULL DEFAULT TRUE
);

-- Kits / baldes (extra ao desenho original: necessário para o montador de combos)
CREATE TABLE IF NOT EXISTS combos (
    id           SERIAL PRIMARY KEY,
    nome         VARCHAR(120) NOT NULL UNIQUE,
    preco_venda  NUMERIC(10,2) NOT NULL CHECK (preco_venda >= 0),
    ativo        BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS combo_itens (
    id          SERIAL PRIMARY KEY,
    combo_id    INTEGER NOT NULL REFERENCES combos(id) ON DELETE CASCADE,
    produto_id  INTEGER NOT NULL REFERENCES produtos(id),
    quantidade  INTEGER NOT NULL CHECK (quantidade > 0),
    UNIQUE (combo_id, produto_id)
);

-- cliente / pago_em / forma_recebimento: controle de fiado
CREATE TABLE IF NOT EXISTS vendas (
    id                 SERIAL PRIMARY KEY,
    data_venda         TIMESTAMP NOT NULL DEFAULT NOW(),
    forma_pagamento    VARCHAR(10) NOT NULL CHECK (forma_pagamento IN ('PIX', 'DINHEIRO', 'CARTAO', 'FIADO')),
    valor_total        NUMERIC(10,2) NOT NULL CHECK (valor_total >= 0),
    lucro_total        NUMERIC(10,2) NOT NULL DEFAULT 0,
    observacao         TEXT,
    cliente            VARCHAR(100),
    pago_em            TIMESTAMP,
    forma_recebimento  VARCHAR(10) CHECK (forma_recebimento IN ('PIX', 'DINHEIRO', 'CARTAO'))
);

-- preço e custo ficam congelados no momento da venda; combo_id marca itens vendidos dentro de um kit
CREATE TABLE IF NOT EXISTS itens_venda (
    id              SERIAL PRIMARY KEY,
    venda_id        INTEGER NOT NULL REFERENCES vendas(id) ON DELETE CASCADE,
    produto_id      INTEGER NOT NULL REFERENCES produtos(id),
    quantidade      INTEGER NOT NULL CHECK (quantidade > 0),
    preco_unitario  NUMERIC(12,4) NOT NULL CHECK (preco_unitario >= 0),
    custo_unitario  NUMERIC(10,4) NOT NULL CHECK (custo_unitario >= 0),
    combo_id        INTEGER REFERENCES combos(id)
);

CREATE TABLE IF NOT EXISTS movimentacoes_caixa (
    id              SERIAL PRIMARY KEY,
    data_movimento  TIMESTAMP NOT NULL DEFAULT NOW(),
    tipo            VARCHAR(10) NOT NULL CHECK (tipo IN ('APORTE', 'RETIRADA')),
    valor           NUMERIC(10,2) NOT NULL CHECK (valor > 0),
    descricao       TEXT
);

-- custo_unitario: custo no dia da perda, para calcular o prejuízo
CREATE TABLE IF NOT EXISTS perdas (
    id              SERIAL PRIMARY KEY,
    data_perda      TIMESTAMP NOT NULL DEFAULT NOW(),
    produto_id      INTEGER NOT NULL REFERENCES produtos(id),
    quantidade      INTEGER NOT NULL CHECK (quantidade > 0),
    motivo          VARCHAR(40) NOT NULL,
    custo_unitario  NUMERIC(10,4) NOT NULL DEFAULT 0
);

-- Compras de fardos/caixas (conversor de embalagens): entra no estoque em unidades
CREATE TABLE IF NOT EXISTS compras (
    id                      SERIAL PRIMARY KEY,
    data_compra             TIMESTAMP NOT NULL DEFAULT NOW(),
    produto_id              INTEGER NOT NULL REFERENCES produtos(id),
    qtd_embalagens          INTEGER NOT NULL CHECK (qtd_embalagens > 0),
    unidades_por_embalagem  INTEGER NOT NULL CHECK (unidades_por_embalagem > 0),
    valor_total             NUMERIC(10,2) NOT NULL CHECK (valor_total >= 0),
    pago_com                VARCHAR(10) NOT NULL CHECK (pago_com IN ('CAIXA', 'BOLSO'))
);

CREATE INDEX IF NOT EXISTS idx_vendas_data ON vendas (data_venda);
CREATE INDEX IF NOT EXISTS idx_itens_venda_venda ON itens_venda (venda_id);
CREATE INDEX IF NOT EXISTS idx_itens_venda_produto ON itens_venda (produto_id);

-- ---------------- Cardápio inicial (custo e preço por UNIDADE) ----------------
INSERT INTO produtos (nome, categoria, preco_custo, preco_venda, estoque_unidades, estoque_minimo) VALUES
    ('Água mineral 500ml',          'Água',          0.75,  2.00,  48, 24),
    ('Água mineral com gás 500ml',  'Água',          1.10,  3.00,  24, 12),
    ('Água mineral 1,5L',           'Água',          1.60,  4.00,  24, 12),
    ('Refrigerante lata 350ml',     'Refrigerante',  2.70,  5.00,  48, 24),
    ('Refrigerante 600ml',          'Refrigerante',  3.80,  7.00,  24, 12),
    ('Refrigerante 2L',             'Refrigerante',  6.50, 11.00,  12,  6),
    ('Suco lata 335ml',             'Suco',          2.60,  5.00,  24, 12),
    ('Energético lata 250ml',       'Energético',    5.50, 10.00,  24, 12),
    ('Energético 473ml',            'Energético',    7.50, 13.00,  12,  6),
    ('Cerveja lata 350ml',          'Cerveja',       3.40,  5.50,  96, 48),
    ('Cerveja long neck 355ml',     'Cerveja',       4.20,  8.00,  48, 24),
    ('Cerveja latão 473ml',         'Cerveja',       4.30,  7.00,  48, 24),
    ('Cerveja garrafa 600ml',       'Cerveja',       6.50, 12.00,  24, 12),
    ('Dose de cachaça',             'Destilado',     1.50,  5.00,  40, 20),
    ('Vodka 1L',                    'Destilado',    28.00, 55.00,   6,  3),
    ('Cachaça 900ml',               'Destilado',    14.00, 28.00,   6,  3),
    ('Cigarro maço',                'Cigarro',       7.50, 10.00,   8, 10),
    ('Cigarro avulso',              'Cigarro',       0.40,  1.00,  40, 20),
    ('Saco de gelo 5kg',            'Gelo',          4.00, 10.00,   4, 10),
    ('Isqueiro',                    'Conveniência',  2.00,  5.00,  20, 10),
    ('Carvão 3kg',                  'Conveniência',  9.00, 18.00,  10,  5),
    ('Copo descartável 200ml',      'Conveniência',  0.06,  0.30, 200, 100),
    ('Balde plástico',              'Conveniência',  6.00, 12.00,   6,  3)
ON CONFLICT (nome) DO NOTHING;

INSERT INTO combos (nome, preco_venda) VALUES ('Balde 5 cervejas + gelo', 42.00)
ON CONFLICT (nome) DO NOTHING;

INSERT INTO combo_itens (combo_id, produto_id, quantidade)
SELECT c.id, p.id, v.qtd
FROM (VALUES
    ('Balde 5 cervejas + gelo', 'Cerveja lata 350ml', 5),
    ('Balde 5 cervejas + gelo', 'Saco de gelo 5kg',    1),
    ('Balde 5 cervejas + gelo', 'Balde plástico',      1)
) AS v(kit, produto, qtd)
JOIN combos c ON c.nome = v.kit
JOIN produtos p ON p.nome = v.produto
ON CONFLICT (combo_id, produto_id) DO NOTHING;
"""


main()
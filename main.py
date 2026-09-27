import os
import sqlite3
from contextlib import contextmanager
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Depends, Header, Query
from pydantic import BaseModel, Field

# ---------------- Configuração ----------------
DB_PATH = os.getenv("DB_PATH", "catalogo.db")
API_KEY = os.getenv("API_KEY", "minha-chave-secreta-123")
PRECIFICACAO_URL = os.getenv("PRECIFICACAO_URL", "http://localhost:8001")
OPENFOODFACTS_URL = "https://world.openfoodfacts.org/api/v0/product/{ean}.json"

app = FastAPI(
    title="API Catálogo",
    description="CRUD de produtos com persistência em SQLite. Importa dados do "
                "Open Food Facts pelo código de barras (EAN) e calcula o preço de "
                "venda consumindo a API de Precificação.",
    version="1.0.0",
)


# ---------------- Banco de dados (SQLite) ----------------
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # permite acessar colunas por nome
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ean TEXT,
                descricao TEXT NOT NULL,
                marca TEXT,
                categoria TEXT,
                custo REAL NOT NULL,
                quantidade INTEGER NOT NULL DEFAULT 0
            )
            """
        )


init_db()  # cria a tabela assim que o app sobe


# ---------------- Modelos (o contrato de entrada e saída) ----------------
class ProdutoIn(BaseModel):
    ean: Optional[str] = Field(None, description="Código de barras (EAN)")
    descricao: str = Field(..., min_length=1, description="Descrição do produto")
    marca: Optional[str] = None
    categoria: Optional[str] = None
    custo: float = Field(..., gt=0, description="Custo do produto (> 0)")
    quantidade: int = Field(0, ge=0, description="Quantidade em estoque")


class ProdutoUpdate(BaseModel):
    ean: Optional[str] = None
    descricao: Optional[str] = Field(None, min_length=1)
    marca: Optional[str] = None
    categoria: Optional[str] = None
    custo: Optional[float] = Field(None, gt=0)
    quantidade: Optional[int] = Field(None, ge=0)


class ProdutoOut(BaseModel):
    id: int
    ean: Optional[str]
    descricao: str
    marca: Optional[str]
    categoria: Optional[str]
    custo: float
    quantidade: int


class ImportarIn(BaseModel):
    custo: float = Field(..., gt=0, description="Custo do produto (> 0)")
    quantidade: int = Field(0, ge=0)


# ---------------- Autenticação (API key) ----------------
def validar_api_key(x_api_key: Optional[str] = Header(None, description="Chave de acesso")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida ou ausente")


# ---------------- Integrações externas ----------------
def buscar_openfoodfacts(ean: str) -> Optional[dict]:
    """Consulta o Open Food Facts pelo EAN. Retorna dados tratados ou None se não achar."""
    try:
        resp = requests.get(
            OPENFOODFACTS_URL.format(ean=ean),
            headers={"User-Agent": "MVP-Catalogo/1.0 (estudante)"},
            timeout=10,
        )
        resp.raise_for_status()
        dados = resp.json()
    except requests.RequestException:
        raise HTTPException(status_code=502, detail="Falha ao consultar o Open Food Facts")

    if dados.get("status") != 1 or "product" not in dados:
        return None

    produto = dados["product"]
    categorias = produto.get("categories") or ""
    primeira_categoria = categorias.split(",")[0].strip() if categorias else None
    return {
        "descricao": produto.get("product_name") or produto.get("generic_name") or f"Produto {ean}",
        "marca": produto.get("brands"),
        "categoria": primeira_categoria,
    }


def calcular_preco(custo: float, categoria: Optional[str]) -> dict:
    """Chama a API de Precificação para obter o preço de venda."""
    try:
        resp = requests.post(
            f"{PRECIFICACAO_URL}/precificar",
            json={"custo": custo, "categoria": categoria},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        raise HTTPException(
            status_code=502,
            detail="Não consegui falar com a API de Precificação. Ela está rodando na porta 8001?",
        )


# ---------------- Helper ----------------
def _buscar(conn, produto_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM produtos WHERE id = ?", (produto_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Produto {produto_id} não encontrado")
    return row


# ---------------- Rotas ----------------
@app.get("/health", tags=["status"], summary="Verifica se a API está no ar")
def health():
    return {"status": "ok"}


@app.get("/produtos", response_model=list[ProdutoOut], tags=["produtos"],
         summary="Lista produtos (com filtro, ordenação e paginação)")
def listar_produtos(
    categoria: Optional[str] = Query(None, description="Filtra por categoria"),
    busca: Optional[str] = Query(None, description="Busca no texto da descrição"),
    ordenar_por: str = Query("id", pattern="^(id|descricao|custo|quantidade)$"),
    ordem: str = Query("asc", pattern="^(asc|desc)$"),
    limite: int = Query(10, ge=1, le=100, description="Itens por página"),
    offset: int = Query(0, ge=0, description="Quantos itens pular"),
):
    sql = "SELECT * FROM produtos WHERE 1=1"
    params: list = []
    if categoria:
        sql += " AND lower(categoria) = ?"
        params.append(categoria.strip().lower())
    if busca:
        sql += " AND lower(descricao) LIKE ?"
        params.append(f"%{busca.strip().lower()}%")
    sql += f" ORDER BY {ordenar_por} {ordem.upper()} LIMIT ? OFFSET ?"
    params.extend([limite, offset])
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/produtos/{produto_id}", response_model=ProdutoOut, tags=["produtos"],
         summary="Consulta um produto")
def obter_produto(produto_id: int):
    with get_db() as conn:
        return dict(_buscar(conn, produto_id))


@app.post("/produtos", response_model=ProdutoOut, status_code=201, tags=["produtos"],
          summary="Cadastra um produto", dependencies=[Depends(validar_api_key)])
def criar_produto(produto: ProdutoIn):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO produtos (ean, descricao, marca, categoria, custo, quantidade) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (produto.ean, produto.descricao, produto.marca, produto.categoria,
             produto.custo, produto.quantidade),
        )
        return dict(_buscar(conn, cur.lastrowid))


@app.put("/produtos/{produto_id}", response_model=ProdutoOut, tags=["produtos"],
         summary="Atualiza um produto", dependencies=[Depends(validar_api_key)])
def atualizar_produto(produto_id: int, dados: ProdutoUpdate):
    campos = dados.model_dump(exclude_unset=True)  # só o que veio no corpo
    if not campos:
        raise HTTPException(status_code=400, detail="Nenhum campo enviado para atualizar")
    with get_db() as conn:
        _buscar(conn, produto_id)  # 404 se não existir
        set_clause = ", ".join(f"{c} = ?" for c in campos)
        conn.execute(f"UPDATE produtos SET {set_clause} WHERE id = ?",
                     list(campos.values()) + [produto_id])
        return dict(_buscar(conn, produto_id))


@app.delete("/produtos/{produto_id}", tags=["produtos"],
            summary="Remove um produto", dependencies=[Depends(validar_api_key)])
def remover_produto(produto_id: int):
    with get_db() as conn:
        _buscar(conn, produto_id)
        conn.execute("DELETE FROM produtos WHERE id = ?", (produto_id,))
    return {"removido": produto_id}


@app.post("/produtos/importar/{ean}", response_model=ProdutoOut, status_code=201,
          tags=["produtos"], summary="Importa um produto do Open Food Facts pelo EAN",
          dependencies=[Depends(validar_api_key)])
def importar_produto(ean: str, dados: ImportarIn):
    info = buscar_openfoodfacts(ean)
    if info is None:
        raise HTTPException(status_code=404,
                            detail=f"EAN {ean} não encontrado no Open Food Facts")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO produtos (ean, descricao, marca, categoria, custo, quantidade) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ean, info["descricao"], info["marca"], info["categoria"],
             dados.custo, dados.quantidade),
        )
        return dict(_buscar(conn, cur.lastrowid))


@app.get("/produtos/{produto_id}/preco", tags=["preço"],
         summary="Calcula o preço de venda (consome a API de Precificação)")
def preco_produto(produto_id: int):
    with get_db() as conn:
        row = _buscar(conn, produto_id)
    resultado = calcular_preco(custo=row["custo"], categoria=row["categoria"])
    return {"produto_id": produto_id, "descricao": row["descricao"], **resultado}
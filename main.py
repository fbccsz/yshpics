import os
import io
import uuid
import hmac
import hashlib
import shutil
import zipfile
from datetime import datetime, timedelta
from typing import List

from fastapi import FastAPI, Request, HTTPException, Depends, File, UploadFile, Form
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from sqlalchemy.orm import sessionmaker, Session
from PIL import Image
import mercadopago

# Importações dos nossos arquivos
from models import Pedido, Foto, Cliente, Album, ItemPedido, engine, Fotografo
from pagamento_pix import gerar_cobranca_pix

app = FastAPI()

# Chave secreta para assinar cookies de sessão. Defina SESSION_SECRET no .env em produção.
SESSION_SECRET = os.getenv("SESSION_SECRET", os.urandom(32).hex())

def _hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode("utf-8")).hexdigest()

def _assinar_sessao(fotografo_id: int) -> str:
    msg = str(fotografo_id).encode()
    sig = hmac.new(SESSION_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return f"{fotografo_id}.{sig}"

def _verificar_sessao(token: str) -> "int | None":
    try:
        fid, sig = token.split(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), fid.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return int(fid)
    except Exception:
        pass
    return None

# ==========================================
# HELPERS DE AUTENTICAÇÃO (Cookie-based)
# ==========================================

def get_fotografo_logado(request: Request, db: Session) -> "Fotografo | None":
    """Retorna o fotógrafo logado ou None se não houver sessão válida."""
    token = request.cookies.get("sessao_admin")
    if not token:
        return None
    fotografo_id = _verificar_sessao(token)
    if fotografo_id is None:
        return None
    return db.query(Fotografo).filter(Fotografo.id == fotografo_id).first()
DIRETORIO_ALTA_RES = "./fotos_alta_res_seguras"
DIRETORIO_BAIXA_RES = "./static/fotos_baixa_res"
os.makedirs(DIRETORIO_ALTA_RES, exist_ok=True)
os.makedirs(DIRETORIO_BAIXA_RES, exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# ROTAS DO SAAS E CHECKOUT
# ==========================================

@app.post("/comprar/{foto_id}")
def comprar_foto(foto_id: int, nome: str, email: str, qualidade: str = 'alta', db: Session = Depends(get_db)):
    """Rota direta para o Guest Checkout sem carrinho complexo."""
    
    foto = db.query(Foto).filter(Foto.id == foto_id).first()
    if not foto:
        return {"sucesso": False, "erro": "Foto não encontrada"}
        
    fotografo = foto.album.fotografo
    if not fotografo.mp_access_token:
        return {"sucesso": False, "erro": "Fotógrafo não configurado para receber"}

    # Define o preço baseado na escolha (alta ou baixa) e calcula a comissão
    valor_venda = foto.preco_alta if qualidade == 'alta' else foto.preco_baixa
    sua_comissao = round(valor_venda * 0.10, 2) if fotografo.plano_atual == "starter" else 0.0

    # Registra cliente e pedido
    cliente = db.query(Cliente).filter(Cliente.email == email).first()
    if not cliente:
        cliente = Cliente(nome=nome, email=email)
        db.add(cliente)
        db.flush()

    novo_pedido = Pedido(
        cliente_id=cliente.id,
        fotografo_id=fotografo.id,
        valor_total=valor_venda,
        taxa_plataforma=sua_comissao
    )
    db.add(novo_pedido)
    db.flush()
    
    # Registra o item
    novo_item = ItemPedido(pedido_id=novo_pedido.id, foto_id=foto.id, qualidade=qualidade, preco_cobrado=valor_venda)
    db.add(novo_item)
    db.commit()

    # Chama o PIX
    pix = gerar_cobranca_pix(
        valor_pedido=valor_venda,
        email_cliente=cliente.email,
        nome_cliente=cliente.nome,
        id_pedido_interno=novo_pedido.id,
        token_fotografo=fotografo.mp_access_token,
        taxa_plataforma=sua_comissao
    )

    if not pix["sucesso"]:
        novo_pedido.status_pagamento = "Cancelado"
        db.commit()
        return {"sucesso": False, "erro": "Falha ao gerar o PIX no Mercado Pago"}

    novo_pedido.pix_txid = pix["txid"]
    novo_pedido.pix_copia_cola = pix["copia_cola"]
    novo_pedido.pix_qr_code_base64 = pix["qr_code_img"]
    db.commit()

    return {"sucesso": True, "pedido_id": novo_pedido.id}

@app.post("/webhook/mercadopago")
async def mercado_pago_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400)

    if payload.get("type") == "payment":
        payment_id = payload.get("data", {}).get("id")
        if payment_id:
            # Encontra o pedido pelo TXID que salvamos
            pedido = db.query(Pedido).filter(Pedido.pix_txid == str(payment_id)).first()
            if pedido and pedido.status_pagamento != "Pago":
                
                # Valida usando o SDK do Fotógrafo dono do pedido
                sdk_fotografo = mercadopago.SDK(pedido.fotografo.mp_access_token)
                payment_info = sdk_fotografo.payment().get(payment_id)
                
                if payment_info["status"] == 200 and payment_info["response"]["status"] == "approved":
                    pedido.status_pagamento = "Pago"
                    db.commit()
                    print(f"\n💰 SUCESSO! Pedido {pedido.id} foi pago.")
                    
    return {"status": "recebido com sucesso"}

# ==========================================
# ROTAS DE VISUALIZAÇÃO E TELAS
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request, db: Session = Depends(get_db)):
    albuns = db.query(Album).order_by(Album.data_evento.desc()).all()
    return templates.TemplateResponse("home.html", {"request": request, "albuns": albuns})

# ==========================================
# AUTENTICAÇÃO (Login / Cadastro / Logout)
# ==========================================

@app.get("/cadastro", response_class=HTMLResponse)
async def tela_cadastro(request: Request):
    return templates.TemplateResponse("cadastro.html", {"request": request})

@app.post("/cadastro")
async def processar_cadastro(request: Request, nome: str = Form(...), email: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    existente = db.query(Fotografo).filter(Fotografo.email == email).first()
    if existente:
        return templates.TemplateResponse("cadastro.html", {"request": request, "erro": "Este e-mail já está em uso."})

    novo_fotografo = Fotografo(
        nome=nome,
        email=email,
        senha_hash=_hash_senha(senha),
        plano_atual="starter"
    )
    db.add(novo_fotografo)
    db.commit()
    return RedirectResponse(url="/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
async def tela_login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def processar_login(request: Request, email: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    fotografo = db.query(Fotografo).filter(Fotografo.email == email).first()
    if not fotografo or not hmac.compare_digest(fotografo.senha_hash, _hash_senha(senha)):
        return templates.TemplateResponse("login.html", {"request": request, "erro": "E-mail ou senha incorretos."})
    resposta = RedirectResponse(url="/admin", status_code=303)
    resposta.set_cookie("sessao_admin", _assinar_sessao(fotografo.id), httponly=True, samesite="lax")
    return resposta

@app.get("/logout")
async def fazer_logout():
    resposta = RedirectResponse(url="/login", status_code=303)
    resposta.delete_cookie("sessao_admin")
    return resposta


# --- SCHEMAS ---
class ItemPedidoIn(BaseModel):
    foto_id: int
    qualidade: str

class CriarPedidoIn(BaseModel):
    itens: List[ItemPedidoIn]
    nome_cliente: str
    email_cliente: str

@app.post("/criar-pedido")
async def criar_pedido(dados: CriarPedidoIn, db: Session = Depends(get_db)):
    """Cria um pedido com múltiplos itens e gera o PIX."""
    if not dados.itens:
        return {"sucesso": False, "erro": "Nenhum item no pedido"}

    primeira_foto = db.query(Foto).filter(Foto.id == dados.itens[0].foto_id).first()
    if not primeira_foto:
        return {"sucesso": False, "erro": "Foto não encontrada"}

    fotografo = primeira_foto.album.fotografo
    if not fotografo.mp_access_token:
        return {"sucesso": False, "erro": "Fotógrafo não configurado para receber"}

    valor_total = 0.0
    fotos_itens = []
    for item in dados.itens:
        foto = db.query(Foto).filter(Foto.id == item.foto_id).first()
        if not foto:
            return {"sucesso": False, "erro": f"Foto {item.foto_id} não encontrada"}
        preco = foto.preco_alta if item.qualidade == 'alta' else foto.preco_baixa
        valor_total += preco
        fotos_itens.append((foto, item.qualidade, preco))

    valor_total = round(valor_total, 2)
    sua_comissao = round(valor_total * 0.10, 2) if fotografo.plano_atual == "starter" else 0.0

    cliente = db.query(Cliente).filter(Cliente.email == dados.email_cliente).first()
    if not cliente:
        cliente = Cliente(nome=dados.nome_cliente, email=dados.email_cliente)
        db.add(cliente)
        db.flush()

    novo_pedido = Pedido(
        cliente_id=cliente.id,
        fotografo_id=fotografo.id,
        valor_total=valor_total,
        taxa_plataforma=sua_comissao
    )
    db.add(novo_pedido)
    db.flush()

    for foto, qualidade, preco in fotos_itens:
        item_db = ItemPedido(pedido_id=novo_pedido.id, foto_id=foto.id, qualidade=qualidade, preco_cobrado=preco)
        db.add(item_db)
    db.commit()

    pix = gerar_cobranca_pix(
        valor_pedido=valor_total,
        email_cliente=cliente.email,
        nome_cliente=cliente.nome,
        id_pedido_interno=novo_pedido.id,
        token_fotografo=fotografo.mp_access_token,
        taxa_plataforma=sua_comissao
    )

    if not pix["sucesso"]:
        novo_pedido.status_pagamento = "Cancelado"
        db.commit()
        return {"sucesso": False, "erro": "Falha ao gerar o PIX no Mercado Pago"}

    novo_pedido.pix_txid = pix["txid"]
    novo_pedido.pix_copia_cola = pix["copia_cola"]
    novo_pedido.pix_qr_code_base64 = pix["qr_code_img"]
    db.commit()

    return {"sucesso": True, "pedido_id": novo_pedido.id}

@app.get("/pagamento/{pedido_id}", response_class=HTMLResponse)
async def tela_pagamento(request: Request, pedido_id: int, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido:
        raise HTTPException(status_code=404)
        
    if datetime.utcnow() > pedido.data_pedido + timedelta(minutes=30):
        raise HTTPException(status_code=403, detail="Expirado.")

    if pedido.status_pagamento == "Pago":
        return templates.TemplateResponse("sucesso.html", {"request": request, "pedido_id": pedido.id, "qtd_fotos": len(pedido.itens)})

    return templates.TemplateResponse("pagamento.html", {
        "request": request,
        "pedido_id": pedido.id,
        "valor_total": f"{pedido.valor_total:.2f}".replace('.', ','),
        "copia_cola": pedido.pix_copia_cola,
        "qr_code_base64": pedido.pix_qr_code_base64
    })

@app.get("/api/status-pagamento/{pedido_id}")
async def verificar_status_pagamento(pedido_id: int, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido:
        raise HTTPException(status_code=404)
    return {"status": pedido.status_pagamento}

@app.get("/sucesso/{pedido_id}", response_class=HTMLResponse)
async def tela_sucesso(request: Request, pedido_id: int, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido or pedido.status_pagamento != "Pago":
        raise HTTPException(status_code=403)
    return templates.TemplateResponse("sucesso.html", {"request": request, "pedido_id": pedido.id, "qtd_fotos": len(pedido.itens)})

@app.get("/baixar-zip/{pedido_id}")
async def baixar_fotos_zip(pedido_id: int, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido or pedido.status_pagamento != "Pago":
        raise HTTPException(status_code=403)

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for item in pedido.itens:
            foto = item.foto
            caminho_real = os.path.join(DIRETORIO_ALTA_RES, foto.caminho_alta_res) if item.qualidade == "alta" else foto.caminho_baixa_res.lstrip('/')
            nome_arq = f"original_{foto.id}.jpg" if item.qualidade == "alta" else f"web_{foto.id}.jpg"
            if os.path.exists(caminho_real):
                zf.write(caminho_real, arcname=nome_arq)

    memory_file.seek(0)
    return StreamingResponse(
        memory_file, media_type="application/zip", 
        headers={"Content-Disposition": f"attachment; filename=yshpics_pedido_{pedido_id}.zip"}
    )

# ==========================================
# ADMIN E UPLOAD
# ==========================================
@app.get("/admin", response_class=HTMLResponse)
async def tela_admin(request: Request, db: Session = Depends(get_db)):
    fotografo = get_fotografo_logado(request, db)
    if not fotografo:
        return RedirectResponse(url="/login", status_code=303)

    meus_albuns = db.query(Album).filter(Album.fotografo_id == fotografo.id).order_by(Album.data_evento.desc()).all()
    pedidos_pagos = db.query(Pedido).filter(Pedido.fotografo_id == fotografo.id, Pedido.status_pagamento == "Pago").all()

    total_vendido = sum(p.valor_total for p in pedidos_pagos)
    minhas_taxas = sum(p.taxa_plataforma for p in pedidos_pagos)
    lucro_limpo = total_vendido - minhas_taxas

    return templates.TemplateResponse("admin.html", {
        "request": request,
        "fotografo": fotografo,
        "albuns": meus_albuns,
        "lucro": f"{lucro_limpo:.2f}".replace('.', ','),
        "vendas": len(pedidos_pagos)
    })

@app.post("/api/configurar-mp")
async def configurar_mp(request: Request, mp_token: str = Form(...), db: Session = Depends(get_db)):
    fotografo = get_fotografo_logado(request, db)
    if fotografo:
        fotografo.mp_access_token = mp_token
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/api/upload")
async def processar_upload(
    request: Request,
    titulo_album: str = Form(...),
    preco_baixa: float = Form(...),
    preco_alta: float = Form(...),
    fotos: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    fotografo = get_fotografo_logado(request, db)
    if not fotografo:
        raise HTTPException(status_code=401, detail="Não autenticado")

    hash_album = str(uuid.uuid4())[:8]
    novo_album = Album(titulo=titulo_album, hash_url=hash_album, fotografo_id=fotografo.id)
    db.add(novo_album)
    db.flush()

    fotos_cadastradas = 0
    for arquivo in fotos:
        if not arquivo.filename: continue
        extensao = arquivo.filename.split(".")[-1]
        nome_base = str(uuid.uuid4())
        nome_alta, nome_baixa = f"{nome_base}_original.{extensao}", f"{nome_base}_vitrine.jpg"
        
        caminho_alta = os.path.join(DIRETORIO_ALTA_RES, nome_alta)
        caminho_baixa = os.path.join(DIRETORIO_BAIXA_RES, nome_baixa)

        with open(caminho_alta, "wb") as buffer:
            shutil.copyfileobj(arquivo.file, buffer)

        try:
            img = Image.open(caminho_alta)
            if img.mode in ("RGBA", "P"): img = img.convert("RGB")
            img.thumbnail((800, 800))
            img.save(caminho_baixa, "JPEG", quality=70)
        except Exception: continue

        nova_foto = Foto(
            album_id=novo_album.id, caminho_baixa_res=f"/static/fotos_baixa_res/{nome_baixa}",
            caminho_alta_res=nome_alta, preco_baixa=preco_baixa, preco_alta=preco_alta
        )
        db.add(nova_foto)
        fotos_cadastradas += 1

    db.commit()
    return {"sucesso": True, "mensagem": f"{fotos_cadastradas} fotos processadas!", "link_album": f"/{novo_album.hash_url}"}

@app.get("/{hash_url}", response_class=HTMLResponse)
async def ver_album(request: Request, hash_url: str, db: Session = Depends(get_db)):
    if hash_url == "favicon.ico":
        raise HTTPException(status_code=404)

    album = db.query(Album).filter(Album.hash_url == hash_url).first()
    if not album:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse("index.html", {
        "request": request, "titulo_album": album.titulo, "fotos": album.fotos
    })
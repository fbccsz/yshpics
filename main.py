import os
import io
import uuid
import shutil
import zipfile
import secrets
from datetime import datetime, timedelta
from typing import List

from fastapi import FastAPI, Request, HTTPException, Depends, File, UploadFile, Form
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqlalchemy.orm import sessionmaker, Session
from PIL import Image
import mercadopago

# Importações dos nossos arquivos
from models import Pedido, Foto, Cliente, Album, ItemPedido, engine
from pagamento_pix import gerar_cobranca_pix

app = FastAPI()
security = HTTPBasic()

# --- CONFIGURAÇÕES DE PASTAS E BANCO ---
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

# --- AUTENTICAÇÃO BÁSICA ADMIN ---
def verificar_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correto_user = secrets.compare_digest(credentials.username, "y")
    correto_pass = secrets.compare_digest(credentials.password, "sh")
    if not (correto_user and correto_pass):
        raise HTTPException(status_code=401, detail="Acesso Negado")
    return credentials.username

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

@app.get("/{hash_url}", response_class=HTMLResponse)
async def ver_album(request: Request, hash_url: str, db: Session = Depends(get_db)):
    if hash_url in ["favicon.ico", "admin", "api"]:
        raise HTTPException(status_code=404)
        
    album = db.query(Album).filter(Album.hash_url == hash_url).first()
    if not album:
        raise HTTPException(status_code=404)
    
    return templates.TemplateResponse("index.html", {
        "request": request, "titulo_album": album.titulo, "fotos": album.fotos 
    })

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
async def tela_admin(request: Request, admin: str = Depends(verificar_admin)):
    return templates.TemplateResponse("admin.html", {"request": request})

@app.post("/api/upload")
async def processar_upload(
    titulo_album: str = Form(...), 
    preco_baixa: float = Form(...),
    preco_alta: float = Form(...),
    fotos: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    admin: str = Depends(verificar_admin)
):
    # ATENÇÃO: Temporário. Assumindo que o fotógrafo ID 1 é o dono do upload (ajustaremos isso com o painel real depois)
    fotografo = db.query(Fotografo).first()
    if not fotografo:
        return {"sucesso": False, "mensagem": "Nenhum fotógrafo cadastrado no banco."}

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
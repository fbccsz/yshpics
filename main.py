import os
import io
import uuid
import hmac
import hashlib
import shutil
import zipfile
import smtplib
import tempfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import FastAPI, Request, HTTPException, Depends, File, UploadFile, Form
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from sqlalchemy.orm import sessionmaker, Session
from PIL import Image
import mercadopago

# Importações dos nossos arquivos
from models import Pedido, Foto, Cliente, Album, ItemPedido, PlataformaConfig, engine, Fotografo
from pagamento_pix import gerar_cobranca_pix

# Reconhecimento facial — importação opcional
try:
    import face_recognition as _fr
    FACE_RECOGNITION_DISPONIVEL = True
except ImportError:
    _fr = None
    FACE_RECOGNITION_DISPONIVEL = False

app = FastAPI()

# Chave secreta para assinar cookies de sessão. Defina SESSION_SECRET no .env em produção.
SESSION_SECRET = os.getenv("SESSION_SECRET", os.urandom(32).hex())
SESSION_DURACAO_DIAS = 7
DOWNLOAD_DURACAO_DIAS = 7

# E-mail do dono da plataforma — define acesso ao painel master em /owner
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "")

# URL base pública (usada em links de e-mail e OG tags)
BASE_URL = os.getenv("BASE_URL", "https://yshpics.com")

# Configuração SMTP para notificações automáticas
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

# Comissão e regras de preço
COMISSAO_STARTER = 0.10          # 10% para plano starter
COMISSAO_MINIMA = 0.50           # R$0,50 — abaixo disso não tentamos o split
PRECO_MINIMO = 1.00              # R$1,00 — preço mínimo por foto

def _enviar_email_download(email_cliente: str, nome_cliente: str, token_download: str, qtd_fotos: int):
    """Envia e-mail com o link de download ao cliente após confirmação do pagamento."""
    if not SMTP_HOST or not SMTP_USER:
        return
    try:
        link = f"{BASE_URL}/baixar/{token_download}"
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "✅ Suas fotos estão prontas! — yshpics"
        msg["From"] = f"yshpics <{SMTP_FROM}>"
        msg["To"] = email_cliente

        html = f"""
        <div style="font-family:Inter,sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#1f2937;">
          <h2 style="color:#3b82f6;font-size:1.5rem;margin:0 0 8px;">Pagamento confirmado! 🎉</h2>
          <p style="margin:0 0 20px;color:#6b7280;">Olá, <strong>{nome_cliente}</strong>! Suas <strong>{qtd_fotos} foto{'s' if qtd_fotos != 1 else ''}</strong> em alta resolução estão prontas para download.</p>
          <a href="{link}" style="display:inline-block;background:#3b82f6;color:#fff;font-weight:700;padding:14px 32px;border-radius:12px;text-decoration:none;font-size:1rem;">
            Baixar minhas fotos (ZIP)
          </a>
          <p style="margin:24px 0 0;font-size:0.8rem;color:#9ca3af;">O link expira em 7 dias. Caso precise de ajuda, responda este e-mail.</p>
        </div>
        """
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, email_cliente, msg.as_string())
    except Exception as exc:
        print(f"⚠️  Falha ao enviar e-mail para {email_cliente}: {exc}")

def calcular_comissao(valor_total: float, plano: str) -> float:
    """Retorna a comissão da plataforma; 0 se o plano for pro ou o valor for pequeno demais."""
    if plano != "starter":
        return 0.0
    comissao = round(valor_total * COMISSAO_STARTER, 2)
    return comissao if comissao >= COMISSAO_MINIMA else 0.0

def _hash_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode("utf-8")).hexdigest()

def _assinar_sessao(fotografo_id: int) -> str:
    expira = int((datetime.utcnow() + timedelta(days=SESSION_DURACAO_DIAS)).timestamp())
    payload = f"{fotografo_id}.{expira}"
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verificar_sessao(token: str) -> "int | None":
    try:
        partes = token.split(".")
        if len(partes) != 3:
            return None
        fid, expira, sig = partes
        payload = f"{fid}.{expira}"
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if datetime.utcnow().timestamp() > int(expira):
            return None
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

def get_owner(request: Request, db: Session) -> "Fotografo | None":
    """Retorna o fotógrafo logado somente se for o dono da plataforma."""
    fotografo = get_fotografo_logado(request, db)
    if fotografo and OWNER_EMAIL and fotografo.email == OWNER_EMAIL:
        return fotografo
    return None
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
    sua_comissao = calcular_comissao(valor_venda, fotografo.plano_atual)

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
        return {"sucesso": False, "erro": pix.get("erro", "Falha ao gerar o PIX no Mercado Pago")}

    # Ajusta comissão registrada ao que foi realmente aplicado
    if not pix.get("split_aplicado", False):
        novo_pedido.taxa_plataforma = 0.0

    novo_pedido.pix_txid = pix["txid"]
    novo_pedido.pix_copia_cola = pix["copia_cola"]
    novo_pedido.pix_qr_code_base64 = pix["qr_code_img"]
    novo_pedido.pix_expiracao = pix.get("expiracao")
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
            if pedido and pedido.status_pagamento == "Pendente":
                
                # Valida usando o SDK do Fotógrafo dono do pedido
                sdk_fotografo = mercadopago.SDK(pedido.fotografo.mp_access_token)
                payment_info = sdk_fotografo.payment().get(payment_id)
                
                if payment_info["status"] == 200:
                    mp_status = payment_info["response"]["status"]
                    if mp_status == "approved":
                        pedido.status_pagamento = "Pago"
                        db.commit()
                        print(f"\n💰 SUCESSO! Pedido {pedido.id} foi pago.")
                        # Notifica o cliente por e-mail com o link de download
                        _enviar_email_download(
                            email_cliente=pedido.cliente.email,
                            nome_cliente=pedido.cliente.nome or "Cliente",
                            token_download=pedido.token_download,
                            qtd_fotos=len(pedido.itens),
                        )
                    elif mp_status in ("cancelled", "expired"):
                        pedido.status_pagamento = "Expirado"
                        db.commit()
                        print(f"\n⏰ Pedido {pedido.id} expirado/cancelado no MP.")
                    
    return {"status": "recebido com sucesso"}

# ==========================================
# ROTAS DE VISUALIZAÇÃO E TELAS
# ==========================================

@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request, db: Session = Depends(get_db)):
    albuns = db.query(Album).order_by(Album.data_evento.desc()).all()
    fotografo = get_fotografo_logado(request, db)
    return templates.TemplateResponse("home.html", {"request": request, "albuns": albuns, "fotografo": fotografo, "now": datetime.utcnow()})

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
async def tela_login(request: Request, db: Session = Depends(get_db)):
    fotografo = get_fotografo_logado(request, db)
    if fotografo:
        destino = "/owner" if (OWNER_EMAIL and fotografo.email == OWNER_EMAIL) else "/admin"
        return RedirectResponse(url=destino, status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def processar_login(request: Request, email: str = Form(...), senha: str = Form(...), db: Session = Depends(get_db)):
    fotografo = db.query(Fotografo).filter(Fotografo.email == email).first()
    if not fotografo or not hmac.compare_digest(fotografo.senha_hash, _hash_senha(senha)):
        return templates.TemplateResponse("login.html", {"request": request, "erro": "E-mail ou senha incorretos."})
    destino = "/owner" if (OWNER_EMAIL and fotografo.email == OWNER_EMAIL) else "/admin"
    resposta = RedirectResponse(url=destino, status_code=303)
    resposta.set_cookie(
        "sessao_admin", _assinar_sessao(fotografo.id),
        httponly=True, samesite="lax",
        max_age=SESSION_DURACAO_DIAS * 86400
    )
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
    sua_comissao = calcular_comissao(valor_total, fotografo.plano_atual)

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
        return {"sucesso": False, "erro": pix.get("erro", "Falha ao gerar o PIX no Mercado Pago")}

    # Ajusta comissão registrada ao que foi realmente aplicado
    if not pix.get("split_aplicado", False):
        novo_pedido.taxa_plataforma = 0.0

    novo_pedido.pix_txid = pix["txid"]
    novo_pedido.pix_copia_cola = pix["copia_cola"]
    novo_pedido.pix_qr_code_base64 = pix["qr_code_img"]
    novo_pedido.pix_expiracao = pix.get("expiracao")
    db.commit()

    return {"sucesso": True, "pedido_id": novo_pedido.id}

@app.get("/pagamento/{pedido_id}", response_class=HTMLResponse)
async def tela_pagamento(request: Request, pedido_id: int, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido:
        raise HTTPException(status_code=404)

    # Determina expiração (usa pix_expiracao se disponível, senão fallback para 30 min após criação)
    expiracao = pedido.pix_expiracao or (pedido.data_pedido + timedelta(minutes=30))
    if pedido.status_pagamento == "Pendente" and datetime.utcnow() > expiracao:
        pedido.status_pagamento = "Expirado"
        db.commit()

    if pedido.status_pagamento == "Expirado":
        return templates.TemplateResponse("pagamento.html", {
            "request": request,
            "pedido_id": pedido.id,
            "valor_total": f"{pedido.valor_total:.2f}".replace('.', ','),
            "copia_cola": None,
            "qr_code_base64": None,
            "expirado": True,
            "expiracao_iso": None,
        })

    if pedido.status_pagamento == "Pago":
        return templates.TemplateResponse("sucesso.html", {
            "request": request,
            "pedido_id": pedido.id,
            "qtd_fotos": len(pedido.itens),
            "download_token": pedido.token_download,
        })

    expiracao_iso = expiracao.strftime("%Y-%m-%dT%H:%M:%S") if expiracao else None
    return templates.TemplateResponse("pagamento.html", {
        "request": request,
        "pedido_id": pedido.id,
        "valor_total": f"{pedido.valor_total:.2f}".replace('.', ','),
        "copia_cola": pedido.pix_copia_cola,
        "qr_code_base64": pedido.pix_qr_code_base64,
        "expirado": False,
        "expiracao_iso": expiracao_iso,
    })

@app.get("/api/status-pagamento/{pedido_id}")
async def verificar_status_pagamento(pedido_id: int, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido:
        raise HTTPException(status_code=404)
    # Auto-expirar se o tempo passou e ainda está Pendente
    if pedido.status_pagamento == "Pendente":
        expiracao = pedido.pix_expiracao or (pedido.data_pedido + timedelta(minutes=30))
        if datetime.utcnow() > expiracao:
            pedido.status_pagamento = "Expirado"
            db.commit()
    return {"status": pedido.status_pagamento}

@app.post("/api/regenerar-pix/{pedido_id}")
async def regenerar_pix(pedido_id: int, db: Session = Depends(get_db)):
    """Regenera o PIX de um pedido expirado ou cancelado."""
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido:
        raise HTTPException(status_code=404)
    if pedido.status_pagamento not in ("Expirado", "Cancelado"):
        return {"sucesso": False, "erro": "Este pedido não pode ser regenerado."}

    fotografo = pedido.fotografo
    if not fotografo.mp_access_token:
        return {"sucesso": False, "erro": "Fotógrafo não configurado para receber."}

    pix = gerar_cobranca_pix(
        valor_pedido=pedido.valor_total,
        email_cliente=pedido.cliente.email,
        nome_cliente=pedido.cliente.nome,
        id_pedido_interno=pedido.id,
        token_fotografo=fotografo.mp_access_token,
        taxa_plataforma=pedido.taxa_plataforma,
    )

    if not pix["sucesso"]:
        return {"sucesso": False, "erro": pix.get("erro", "Falha ao regenerar o PIX.")}

    pedido.status_pagamento = "Pendente"
    pedido.pix_txid = pix["txid"]
    pedido.pix_copia_cola = pix["copia_cola"]
    pedido.pix_qr_code_base64 = pix["qr_code_img"]
    pedido.pix_expiracao = pix.get("expiracao")
    db.commit()

    expiracao_iso = pedido.pix_expiracao.strftime("%Y-%m-%dT%H:%M:%S") if pedido.pix_expiracao else None
    return {
        "sucesso": True,
        "copia_cola": pedido.pix_copia_cola,
        "qr_code_base64": pedido.pix_qr_code_base64,
        "expiracao_iso": expiracao_iso,
    }

@app.get("/sucesso/{pedido_id}", response_class=HTMLResponse)
async def tela_sucesso(request: Request, pedido_id: int, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido or pedido.status_pagamento != "Pago":
        raise HTTPException(status_code=403)
    return templates.TemplateResponse("sucesso.html", {
        "request": request,
        "pedido_id": pedido.id,
        "qtd_fotos": len(pedido.itens),
        "download_token": pedido.token_download,
    })

@app.get("/baixar/{token}")
async def baixar_fotos_zip(token: str, db: Session = Depends(get_db)):
    pedido = db.query(Pedido).filter(Pedido.token_download == token).first()
    if not pedido or pedido.status_pagamento != "Pago":
        raise HTTPException(status_code=403)

    # Verifica expiração do link de download (7 dias após a criação do pedido)
    if datetime.utcnow() > pedido.data_pedido + timedelta(days=DOWNLOAD_DURACAO_DIAS):
        raise HTTPException(status_code=410, detail="Link de download expirado.")

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
        headers={"Content-Disposition": f"attachment; filename=yshpics_pedido_{pedido.id}.zip"}
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
        "total_vendido": f"{total_vendido:.2f}".replace('.', ','),
        "taxa_cobrada": f"{minhas_taxas:.2f}".replace('.', ','),
        "vendas": len(pedidos_pagos),
        "is_owner": bool(OWNER_EMAIL and fotografo.email == OWNER_EMAIL),
        "preco_minimo": f"{PRECO_MINIMO:.2f}".replace('.', ','),
        "preco_minimo_num": PRECO_MINIMO,
        "comissao_percentual": int(COMISSAO_STARTER * 100),
        "comissao_minima": f"{COMISSAO_MINIMA:.2f}".replace('.', ','),
        "venda_minima_com_taxa": f"{COMISSAO_MINIMA / COMISSAO_STARTER:.2f}".replace('.', ','),
    })

@app.post("/api/configurar-mp")
async def configurar_mp(request: Request, mp_token: str = Form(...), db: Session = Depends(get_db)):
    fotografo = get_fotografo_logado(request, db)
    if fotografo:
        fotografo.mp_access_token = mp_token
        db.commit()
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/api/excluir-album")
async def excluir_album_proprio(
    request: Request,
    album_id: int = Form(...),
    db: Session = Depends(get_db),
):
    fotografo = get_fotografo_logado(request, db)
    if not fotografo:
        raise HTTPException(status_code=401, detail="Não autenticado")
    album = db.query(Album).filter(Album.id == album_id, Album.fotografo_id == fotografo.id).first()
    if not album:
        raise HTTPException(status_code=404, detail="Álbum não encontrado")
    foto_ids = [f.id for f in album.fotos]
    if foto_ids:
        db.query(ItemPedido).filter(ItemPedido.foto_id.in_(foto_ids)).delete(synchronize_session=False)
    for foto in album.fotos:
        try:
            caminho_alta = os.path.join(DIRETORIO_ALTA_RES, foto.caminho_alta_res)
            if os.path.exists(caminho_alta):
                os.remove(caminho_alta)
            caminho_baixa = foto.caminho_baixa_res.lstrip('/')
            if os.path.exists(caminho_baixa):
                os.remove(caminho_baixa)
        except Exception:
            pass
        db.delete(foto)
    db.delete(album)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/api/upload")
async def processar_upload(
    request: Request,
    titulo_album: str = Form(...),
    preco_baixa: float = Form(...),
    preco_alta: float = Form(...),
    categoria: Optional[str] = Form(None),
    cidade: Optional[str] = Form(None),
    data_evento: Optional[str] = Form(None),
    fotos: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    fotografo = get_fotografo_logado(request, db)
    if not fotografo:
        raise HTTPException(status_code=401, detail="Não autenticado")

    if preco_baixa < PRECO_MINIMO or preco_alta < PRECO_MINIMO:
        raise HTTPException(status_code=400, detail=f"Preço mínimo por foto é R${PRECO_MINIMO:.2f}".replace('.', ','))

    # Parse event date; fall back to today if missing or invalid
    data_evento_dt = datetime.utcnow()
    if data_evento:
        try:
            data_evento_dt = datetime.strptime(data_evento, "%Y-%m-%d")
        except ValueError:
            pass

    hash_album = str(uuid.uuid4())[:8]
    novo_album = Album(titulo=titulo_album, hash_url=hash_album, fotografo_id=fotografo.id, categoria=categoria or None, cidade=cidade or None, data_evento=data_evento_dt)
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

# ==========================================
# PAINEL DO DONO DA PLATAFORMA
# ==========================================

@app.get("/owner", response_class=HTMLResponse)
async def painel_dono(request: Request, db: Session = Depends(get_db)):
    owner = get_owner(request, db)
    if not owner:
        return RedirectResponse(url="/login", status_code=303)

    todos_fotografos = db.query(Fotografo).order_by(Fotografo.id.desc()).all()
    todos_albuns = db.query(Album).order_by(Album.data_evento.desc()).all()
    todos_pedidos = db.query(Pedido).order_by(Pedido.data_pedido.desc()).all()

    # Filtra métricas a partir do último reset (se houver)
    config = db.query(PlataformaConfig).first()
    metricas_reset_em = config.metricas_reset_em if config else None
    if metricas_reset_em:
        pedidos_pagos = [p for p in todos_pedidos if p.status_pagamento == "Pago" and p.data_pedido >= metricas_reset_em]
    else:
        pedidos_pagos = [p for p in todos_pedidos if p.status_pagamento == "Pago"]

    receita_total = sum(p.taxa_plataforma for p in pedidos_pagos)
    volume_total = sum(p.valor_total for p in pedidos_pagos)

    return templates.TemplateResponse("owner_admin.html", {
        "request": request,
        "owner": owner,
        "fotografos": todos_fotografos,
        "albuns": todos_albuns,
        "pedidos": todos_pedidos[:30],
        "pedidos_pagos": len(pedidos_pagos),
        "receita_total": f"{receita_total:.2f}".replace('.', ','),
        "volume_total": f"{volume_total:.2f}".replace('.', ','),
        "metricas_reset_em": metricas_reset_em.strftime("%d/%m/%Y %H:%M") if metricas_reset_em else None,
    })

@app.post("/owner/upload")
async def owner_upload(
    request: Request,
    fotografo_id: int = Form(...),
    titulo_album: str = Form(...),
    preco_baixa: float = Form(...),
    preco_alta: float = Form(...),
    categoria: Optional[str] = Form(None),
    cidade: Optional[str] = Form(None),
    fotos: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    owner = get_owner(request, db)
    if not owner:
        raise HTTPException(status_code=401, detail="Não autenticado")

    fotografo = db.query(Fotografo).filter(Fotografo.id == fotografo_id).first()
    if not fotografo:
        raise HTTPException(status_code=404, detail="Fotógrafo não encontrado")

    if preco_baixa < PRECO_MINIMO or preco_alta < PRECO_MINIMO:
        raise HTTPException(status_code=400, detail=f"Preço mínimo por foto é R${PRECO_MINIMO:.2f}".replace('.', ','))

    hash_album = str(uuid.uuid4())[:8]
    novo_album = Album(titulo=titulo_album, hash_url=hash_album, fotografo_id=fotografo.id, categoria=categoria or None, cidade=cidade or None)
    db.add(novo_album)
    db.flush()

    fotos_cadastradas = 0
    for arquivo in fotos:
        if not arquivo.filename:
            continue
        extensao = arquivo.filename.split(".")[-1]
        nome_base = str(uuid.uuid4())
        nome_alta = f"{nome_base}_original.{extensao}"
        nome_baixa = f"{nome_base}_vitrine.jpg"

        caminho_alta = os.path.join(DIRETORIO_ALTA_RES, nome_alta)
        caminho_baixa = os.path.join(DIRETORIO_BAIXA_RES, nome_baixa)

        with open(caminho_alta, "wb") as buffer:
            shutil.copyfileobj(arquivo.file, buffer)

        try:
            img = Image.open(caminho_alta)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.thumbnail((800, 800))
            img.save(caminho_baixa, "JPEG", quality=70)
        except Exception:
            continue

        nova_foto = Foto(
            album_id=novo_album.id,
            caminho_baixa_res=f"/static/fotos_baixa_res/{nome_baixa}",
            caminho_alta_res=nome_alta,
            preco_baixa=preco_baixa,
            preco_alta=preco_alta,
        )
        db.add(nova_foto)
        fotos_cadastradas += 1

    db.commit()
    return {"sucesso": True, "mensagem": f"{fotos_cadastradas} fotos processadas!", "link_album": f"/{novo_album.hash_url}"}

@app.post("/owner/alterar-plano")
async def owner_alterar_plano(
    request: Request,
    fotografo_id: int = Form(...),
    novo_plano: str = Form(...),
    db: Session = Depends(get_db),
):
    owner = get_owner(request, db)
    if not owner:
        raise HTTPException(status_code=401)
    if novo_plano not in ("starter", "pro"):
        raise HTTPException(status_code=400, detail="Plano inválido")
    fotografo = db.query(Fotografo).filter(Fotografo.id == fotografo_id).first()
    if not fotografo:
        raise HTTPException(status_code=404)
    fotografo.plano_atual = novo_plano
    db.commit()
    return RedirectResponse(url="/owner", status_code=303)

@app.post("/owner/resetar-metricas")
async def owner_resetar_metricas(request: Request, db: Session = Depends(get_db)):
    """Reseta as métricas do painel master a partir deste momento."""
    owner = get_owner(request, db)
    if not owner:
        raise HTTPException(status_code=401)
    config = db.query(PlataformaConfig).first()
    if not config:
        config = PlataformaConfig()
        db.add(config)
    config.metricas_reset_em = datetime.utcnow()
    db.commit()
    return RedirectResponse(url="/owner", status_code=303)

@app.post("/owner/excluir-album")
async def owner_excluir_album(
    request: Request,
    album_id: int = Form(...),
    db: Session = Depends(get_db),
):
    owner = get_owner(request, db)
    if not owner:
        raise HTTPException(status_code=401)
    album = db.query(Album).filter(Album.id == album_id).first()
    if not album:
        raise HTTPException(status_code=404)
    # Remove fotos do disco e do banco
    for foto in album.fotos:
        for item in db.query(ItemPedido).filter(ItemPedido.foto_id == foto.id).all():
            db.delete(item)
        try:
            caminho_alta = os.path.join(DIRETORIO_ALTA_RES, foto.caminho_alta_res)
            if os.path.exists(caminho_alta):
                os.remove(caminho_alta)
            caminho_baixa = foto.caminho_baixa_res.lstrip('/')
            if os.path.exists(caminho_baixa):
                os.remove(caminho_baixa)
        except Exception:
            pass
        db.delete(foto)
    db.delete(album)
    db.commit()
    return RedirectResponse(url="/owner", status_code=303)


@app.post("/owner/excluir-fotografo")
async def owner_excluir_fotografo(
    request: Request,
    fotografo_id: int = Form(...),
    db: Session = Depends(get_db),
):
    owner = get_owner(request, db)
    if not owner:
        raise HTTPException(status_code=401)
    if fotografo_id == owner.id:
        raise HTTPException(status_code=400, detail="Não é possível excluir sua própria conta.")
    fotografo = db.query(Fotografo).filter(Fotografo.id == fotografo_id).first()
    if not fotografo:
        raise HTTPException(status_code=404)

    # Remove todos os álbuns e fotos do fotógrafo
    foto_ids = []
    for album in list(fotografo.albuns):
        for foto in list(album.fotos):
            foto_ids.append(foto.id)
            try:
                caminho_alta = os.path.join(DIRETORIO_ALTA_RES, foto.caminho_alta_res)
                if os.path.exists(caminho_alta):
                    os.remove(caminho_alta)
                caminho_baixa = foto.caminho_baixa_res.lstrip('/')
                if os.path.exists(caminho_baixa):
                    os.remove(caminho_baixa)
            except OSError as e:
                print(f"⚠️  Erro ao remover arquivo da foto {foto.id}: {e}")
            db.delete(foto)
        db.delete(album)

    # Remove itens de pedido ligados às fotos deletadas
    if foto_ids:
        db.query(ItemPedido).filter(ItemPedido.foto_id.in_(foto_ids)).delete(synchronize_session=False)

    # Remove pedidos do fotógrafo
    for pedido in db.query(Pedido).filter(Pedido.fotografo_id == fotografo_id).all():
        db.delete(pedido)

    db.delete(fotografo)
    db.commit()
    return RedirectResponse(url="/owner", status_code=303)


@app.get("/{hash_url}", response_class=HTMLResponse)
async def ver_album(request: Request, hash_url: str, db: Session = Depends(get_db)):
    if hash_url == "favicon.ico":
        raise HTTPException(status_code=404)

    album = db.query(Album).filter(Album.hash_url == hash_url).first()
    if not album:
        raise HTTPException(status_code=404)

    capa_url = ""
    if album.fotos:
        capa_url = f"{BASE_URL}{album.fotos[0].caminho_baixa_res}"

    return templates.TemplateResponse("index.html", {
        "request": request,
        "titulo_album": album.titulo,
        "fotos": album.fotos,
        "album": album,
        "capa_url": capa_url,
        "base_url": BASE_URL,
    })


@app.post("/api/facial/{hash_url}")
async def reconhecimento_facial(hash_url: str, selfie: UploadFile = File(...), db: Session = Depends(get_db)):
    """Recebe uma selfie e retorna os IDs das fotos do álbum onde o rosto aparece."""
    if not FACE_RECOGNITION_DISPONIVEL:
        return {"sucesso": False, "erro": "Reconhecimento facial não disponível no momento."}

    album = db.query(Album).filter(Album.hash_url == hash_url).first()
    if not album:
        raise HTTPException(status_code=404)

    # Decodifica a selfie
    selfie_bytes = await selfie.read()
    try:
        selfie_img = _fr.load_image_file(io.BytesIO(selfie_bytes))
        selfie_encodings = _fr.face_encodings(selfie_img)
    except Exception:
        return {"sucesso": False, "erro": "Não foi possível processar a selfie."}

    if not selfie_encodings:
        return {"sucesso": False, "erro": "Nenhum rosto detectado na selfie. Tente uma foto frontal com boa iluminação."}

    selfie_encoding = selfie_encodings[0]
    fotos_encontradas = []

    for foto in album.fotos:
        caminho = foto.caminho_baixa_res.lstrip("/")
        if not os.path.exists(caminho):
            continue
        try:
            img = _fr.load_image_file(caminho)
            encodings = _fr.face_encodings(img)
            if encodings:
                results = _fr.compare_faces(encodings, selfie_encoding, tolerance=0.55)
                if True in results:
                    fotos_encontradas.append(foto.id)
        except Exception:
            continue

    return {"sucesso": True, "fotos_com_voce": fotos_encontradas, "total": len(fotos_encontradas)}
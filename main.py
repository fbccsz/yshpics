import secrets
from datetime import datetime, timedelta
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi import Depends

security = HTTPBasic()

def verificar_admin(credentials: HTTPBasicCredentials = Depends(security)):
    # TROQUE ESTES VALORES PELA SUA SENHA REAL DEPOIS
    correto_user = secrets.compare_digest(credentials.username, "y")
    correto_pass = secrets.compare_digest(credentials.password, "sh")
    
    if not (correto_user and correto_pass):
        raise HTTPException(
            status_code=401,
            detail="Acesso Negado à Área do Fotógrafo",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

from fastapi import FastAPI, Request, HTTPException
from sqlalchemy.orm import sessionmaker
import mercadopago
from fastapi.responses import FileResponse
import os
import uuid
from pydantic import BaseModel
from typing import List

from fastapi import File, UploadFile, Form
from fastapi.staticfiles import StaticFiles
from PIL import Image
import shutil

import zipfile
import io
from fastapi.responses import StreamingResponse

from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

# Importando do nosso banco e da nossa integração PIX
from models import Pedido, Foto, Cliente, Album, ItemPedido, engine
from pagamento_pix import gerar_cobranca_pix


app = FastAPI()

# 1. Primeiro declaramos os caminhos das pastas
DIRETORIO_ALTA_RES = "./fotos_alta_res_seguras"
DIRETORIO_BAIXA_RES = "./static/fotos_baixa_res"

# 2. Depois FORÇAMOS o sistema a criar as pastas (se elas ainda não existirem)
os.makedirs(DIRETORIO_ALTA_RES, exist_ok=True)
os.makedirs(DIRETORIO_BAIXA_RES, exist_ok=True)

# 3. E SÓ AGORA, com a pasta 'static' já existindo, nós liberamos o acesso público
app.mount("/static", StaticFiles(directory="static"), name="static")

# Conectando ao Banco de Dados
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
db = SessionLocal()

class ItemCarrinho(BaseModel):
    foto_id: int
    qualidade: str # 'baixa' ou 'alta'

class DadosPedido(BaseModel):
    itens: List[ItemCarrinho]
    nome_cliente: str
    email_cliente: str

# 2. A Rota que o JavaScript vai chamar
@app.post("/criar-pedido")
async def criar_pedido(dados: DadosPedido):
    if not dados.itens:
        raise HTTPException(status_code=400, detail="Nenhuma foto selecionada.")

    # Corrige e-mail e nome inválidos para o Mercado Pago
    nome_cliente = dados.nome_cliente.strip() or "Cliente"
    email_cliente = dados.email_cliente.strip()
    if not email_cliente or email_cliente == "sem@email.com":
        email_cliente = f"cliente{str(uuid.uuid4())[:8]}@email.com"

    cliente = Cliente(nome=nome_cliente, email=email_cliente)
    db.add(cliente)
    db.flush() # Salva temporariamente para pegar o ID

    novo_pedido = Pedido(cliente_id=cliente.id, status_pagamento="Pendente")
    db.add(novo_pedido)
    db.flush()

    valor_total = 0.0
    for item in dados.itens:
        foto_db = db.query(Foto).filter(Foto.id == item.foto_id).first()
        if foto_db:
            # Define o preço com base na escolha do cliente
            preco = foto_db.preco_alta if item.qualidade == 'alta' else foto_db.preco_baixa
            # Se por algum motivo o preço for Nulo no banco, assume 0
            if preco is None: 
                preco = 0.0
            valor_total += preco
            # Registra o item no pedido
            novo_item = ItemPedido(pedido_id=novo_pedido.id, foto_id=foto_db.id, qualidade=item.qualidade, preco_cobrado=preco)
            db.add(novo_item)

    valor_total = round(float(valor_total), 2)

    if valor_total <= 0:
        raise HTTPException(status_code=400, detail="O valor total do pedido não pode ser zero.")
    
    novo_pedido.valor_total = valor_total
    db.commit()

    # (MANTENHA AQUI O SEU CÓDIGO DA API DO MERCADO PAGO EXATAMENTE IGUAL)
    pix_info = gerar_cobranca_pix(
        valor_pedido=valor_total,
        email_cliente=cliente.email,
        nome_cliente=cliente.nome,
        id_pedido_interno=novo_pedido.id
    )
    
    if pix_info["sucesso"]:
        novo_pedido.pix_txid = pix_info["txid"]
        novo_pedido.pix_copia_cola = pix_info["copia_cola"]
        novo_pedido.pix_qr_code_base64 = pix_info["qr_code_img"]
        db.commit()
        return {"sucesso": True, "pedido_id": novo_pedido.id}
    else:
        novo_pedido.status_pagamento = "Cancelado"
        db.commit()
        return {"sucesso": False, "erro": "Falha ao gerar o PIX."}
    

@app.post("/webhook/mercadopago")
async def mercado_pago_webhook(request: Request):
    """
    Esta rota recebe as notificações (POST) automáticas do Mercado Pago.
    """
    try:
        # O Mercado Pago manda um JSON com os dados da notificação
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Payload inválido")

    # Filtramos para agir apenas quando for uma notificação de "pagamento"
    if payload.get("type") == "payment":
        payment_id = payload.get("data", {}).get("id")
        
        if payment_id:
            # 1. Consultamos a API do MP para ter certeza do status real (medida de segurança)
            payment_info = sdk.payment().get(payment_id)
            
            if payment_info["status"] == 200:
                status_real = payment_info["response"]["status"]
                
                # Se o PIX foi pago com sucesso, o status será "approved"
                if status_real == "approved":
                    
                    # 2. Buscamos o pedido no nosso banco de dados
                    # Lembre-se que salvamos o ID da transação na coluna 'pix_txid'
                    pedido = db.query(Pedido).filter(Pedido.pix_txid == str(payment_id)).first()
                    
                    if pedido and pedido.status_pagamento != "Pago":
                        # 3. Atualizamos o status e salvamos!
                        pedido.status_pagamento = "Pago"
                        db.commit()
                        
                        # --- A MÁGICA ACONTECE AQUI ---
                        print(f"\n💰 SUCESSO! Pedido {pedido.id} foi pago.")
                        print(f"Liberando fotos em alta resolução para o cliente {pedido.cliente_id}...\n")
                        # Aqui você chamaria a função que envia o email ou libera o link de download
                        
    # Retornamos 200 OK rapidamente para o Mercado Pago saber que recebemos o aviso
    return {"status": "recebido com sucesso"}

# Diretório secreto onde ficam as fotos em alta resolução no seu Linux
DIRETORIO_ALTA_RES = "/caminho/seguro/no/servidor/fotos_alta_res/"

@app.get("/download/{token_download}")
async def baixar_fotos(token_download: str):
    """
    Rota segura para download. O cliente acessa yshdev.me/download/TOKEN_AQUI
    """
    
    # 1. Busca no banco de dados quem é o dono desse token
    # (Imaginando que você criou uma coluna 'token_download' na tabela Pedidos)
    pedido = db.query(Pedido).filter(Pedido.token_download == token_download).first()
    
    # 2. Verifica se o token existe e se o pedido realmente está pago
    if not pedido:
        raise HTTPException(status_code=404, detail="Link inválido ou expirado.")
        
    if pedido.status_pagamento != "Pago":
        raise HTTPException(status_code=403, detail="Aguardando confirmação de pagamento.")

    # 3. Se passou nas verificações, o Python pega o arquivo da pasta bloqueada
    # Para simplificar, vamos imaginar que o pedido tem apenas 1 foto. 
    # (Para várias fotos, você geraria um .zip com a biblioteca 'zipfile' do Python)
    foto = pedido.fotos[0] 
    caminho_real_do_arquivo = os.path.join(DIRETORIO_ALTA_RES, foto.caminho_alta_res)
    
    if not os.path.exists(caminho_real_do_arquivo):
        raise HTTPException(status_code=404, detail="Arquivo original não encontrado.")

    # 4. O FastAPI devolve o arquivo diretamente como um anexo para download,
    # sem nunca revelar a pasta verdadeira onde ele está guardado.
    return FileResponse(
        path=caminho_real_do_arquivo, 
        filename=f"foto_alta_resolucao_{pedido.id}.jpg",
        media_type="image/jpeg"
    )

# Configura a pasta onde ficam os arquivos HTML
templates = Jinja2Templates(directory="templates")

@app.get("/pagamento/{pedido_id}", response_class=HTMLResponse)
async def tela_pagamento(request: Request, pedido_id: int):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if not pedido:
        raise HTTPException(status_code=404, detail="Pedido não encontrado.")
        
    # Verifica se já passou de 30 minutos
    if datetime.utcnow() > pedido.data_pedido + timedelta(minutes=30):
        raise HTTPException(status_code=403, detail="O tempo para este pagamento expirou. Gere um novo pedido.")

    if pedido.status_pagamento == "Pago":
        return templates.TemplateResponse("sucesso.html", {"request": request})

    return templates.TemplateResponse("pagamento.html", {
        "request": request,
        "pedido_id": pedido.id,
        "valor_total": f"{pedido.valor_total:.2f}".replace('.', ','),
        "copia_cola": pedido.pix_copia_cola,
        "qr_code_base64": pedido.pix_qr_code_base64
    })

@app.get("/api/status-pagamento/{pedido_id}")
async def verificar_status_pagamento(pedido_id: int):
    """
    Rota leve para o frontend consultar o estado do pedido.
    """
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    
    if not pedido:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
        
    return {"status": pedido.status_pagamento}

#Crie essa pasta no seu projeto e coloque algumas imagens de teste lá!
DIRETORIO_ALTA_RES = "./fotos_alta_res_seguras"

@app.get("/sucesso/{pedido_id}", response_class=HTMLResponse)
async def tela_sucesso(request: Request, pedido_id: int):
    """
    Exibe a tela final agradecendo a compra e oferecendo o botão de download.
    """
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    
    # Segurança em primeiro lugar: Se não estiver pago, não entra.
    if not pedido or pedido.status_pagamento != "Pago":
        raise HTTPException(status_code=403, detail="Acesso negado. Pagamento não confirmado.")

    return templates.TemplateResponse("sucesso.html", {
        "request": request,
        "pedido_id": pedido.id,
        "qtd_fotos": len(pedido.itens)
    })

@app.get("/baixar-zip/{pedido_id}")
async def baixar_fotos_zip(pedido_id: int):
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    
    if not pedido or pedido.status_pagamento != "Pago":
        raise HTTPException(status_code=403, detail="Acesso negado.")

    memory_file = io.BytesIO()
    
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Agora iteramos sobre os itens comprados, não as fotos diretamente
        for item in pedido.itens:
            foto = item.foto
            
            if item.qualidade == "alta":
                caminho_real = os.path.join(DIRETORIO_ALTA_RES, foto.caminho_alta_res)
                nome_arquivo = f"original_alta_{foto.id}.jpg"
            else:
                # Se comprou baixa, entregamos o arquivo de 800px da vitrine (ele não tem marca d'água física, é ideal para web)
                # O lstrip('/') remove a barra inicial para o os.path entender o caminho corretamente
                caminho_real = foto.caminho_baixa_res.lstrip('/') 
                nome_arquivo = f"web_baixa_{foto.id}.jpg"

            if os.path.exists(caminho_real):
                zf.write(caminho_real, arcname=nome_arquivo)

    memory_file.seek(0)
    return StreamingResponse(
        memory_file, media_type="application/zip", 
        headers={"Content-Disposition": f"attachment; filename=yshpics_pedido_{pedido_id}.zip"}
    )

@app.get("/dev/aprovar/{pedido_id}")
async def simular_pagamento_aprovado(pedido_id: int):
    """
    ROTA DE TESTE: Força a aprovação de um pedido no banco de dados.
    """
    pedido = db.query(Pedido).filter(Pedido.id == pedido_id).first()
    if pedido:
        pedido.status_pagamento = "Pago"
        db.commit()
        return {"mensagem": f"Pedido {pedido_id} aprovado à força! Olhe a tela do cliente."}
    return {"erro": "Pedido não encontrado"}

@app.get("/admin", response_class=HTMLResponse)
async def tela_admin(request: Request, admin: str = Depends(verificar_admin)):
    return templates.TemplateResponse("admin.html", {"request": request})

@app.post("/api/upload")
async def processar_upload(
    titulo_album: str = Form(...), 
    preco_baixa: float = Form(...),
    preco_alta: float = Form(...),
    fotos: List[UploadFile] = File(...),
    admin: str = Depends(verificar_admin) # Protegido!
):
    """Recebe as imagens, comprime, salva no cofre e cadastra no banco."""
    
    # 1. Cria o Álbum no banco de dados
    hash_album = str(uuid.uuid4())[:8] # Gera um código único curto, ex: 'a1b2c3d4'
    novo_album = Album(titulo=titulo_album, hash_url=hash_album)
    db.add(novo_album)
    db.commit()
    db.refresh(novo_album)

    fotos_cadastradas = 0

    # 2. Processa cada foto enviada
    for arquivo in fotos:
        if not arquivo.filename:
            continue
            
        # Gera nomes únicos para os arquivos para evitar conflitos
        extensao = arquivo.filename.split(".")[-1]
        nome_base = str(uuid.uuid4())
        nome_alta = f"{nome_base}_original.{extensao}"
        nome_baixa = f"{nome_base}_vitrine.jpg" # Força JPEG na web para ficar leve
        
        caminho_alta = os.path.join(DIRETORIO_ALTA_RES, nome_alta)
        caminho_baixa = os.path.join(DIRETORIO_BAIXA_RES, nome_baixa)

        # A. Salva a foto original no cofre
        with open(caminho_alta, "wb") as buffer:
            shutil.copyfileobj(arquivo.file, buffer)

        # B. Abre a imagem com o Pillow para comprimir e criar a versão da vitrine
        try:
            img = Image.open(caminho_alta)
            
            # Converte para RGB (evita erros se subirem PNG com fundo transparente)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
                
            # Reduz o tamanho máximo para 800px de largura/altura (inútil para impressão, ótimo para celular)
            img.thumbnail((800, 800))
            
            # Salva com qualidade reduzida (70%)
            img.save(caminho_baixa, "JPEG", quality=70)
        except Exception as e:
            print(f"Erro ao processar imagem {arquivo.filename}: {e}")
            continue

        # C. Salva no Banco de Dados
        nova_foto = Foto(
            album_id=novo_album.id,
            caminho_baixa_res=f"/static/fotos_baixa_res/{nome_baixa}",
            caminho_alta_res=nome_alta,
            preco_baixa=preco_baixa,
            preco_alta=preco_alta
        )
        db.add(nova_foto)
        fotos_cadastradas += 1

    db.commit()

    return {
        "sucesso": True, 
        "mensagem": f"{fotos_cadastradas} fotos processadas!",
        "link_album": f"/{novo_album.hash_url}" # Preparando para o futuro
    }

@app.get("/", response_class=HTMLResponse)
async def landing_page(request: Request):
    # Busca todos os álbuns do mais novo pro mais velho
    albuns = db.query(Album).order_by(Album.data_evento.desc()).all()
    
    return templates.TemplateResponse("home.html", {
        "request": request,
        "albuns": albuns
    })

@app.get("/{hash_url}", response_class=HTMLResponse)
async def ver_album(request: Request, hash_url: str):
    
    # Evita que o navegador quebre o site caçando o favicon
    if hash_url == "favicon.ico":
        raise HTTPException(status_code=404)
        
    album = db.query(Album).filter(Album.hash_url == hash_url).first()
    
    if not album:
        raise HTTPException(status_code=404, detail="Álbum não encontrado")
    
    return templates.TemplateResponse("index.html", {
        "request": request,
        "titulo_album": album.titulo,
        "fotos": album.fotos 
    })
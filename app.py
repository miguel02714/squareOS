from __future__ import annotations

# --- stdlib ---
import os
import platform
import re
import secrets
import string
import subprocess
import threading
from datetime import datetime, timedelta
from typing import Optional
import smtplib
from email.mime.text import MIMEText
from flask_cors import CORS # Linha nova
# --- Flask e extensões ---
from flask import (
    Flask, request, render_template, Blueprint, session, flash,
    redirect, url_for, send_from_directory, jsonify,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, login_user, login_required,
    logout_user, current_user, UserMixin
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

# --- SQLAlchemy helpers ---
from sqlalchemy import UniqueConstraint, Index, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sentence_transformers import SentenceTransformer, util
import torch

# =============================================================================
# Configurações do app
# =============================================================================
app = Flask(__name__)
execution_lock = threading.Lock()



app.config.update(
    SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", "sqlite:///nebulainteligence.db"),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SECRET_KEY=os.getenv("SECRET_KEY", "supersecreto123"),
    UPLOAD_FOLDER="user",
    SMTP_EMAIL=os.getenv("nebulaossac@gmail.com"),  # Email do Gmail
    SMTP_PASS=os.getenv("jjue lkzy sjjr bkns")     # App password do Gmail
)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "error"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "txt", "pdf", "md", "py", "js", "html", "css"}

# =============================================================================
# PRAGMA SQLite
# =============================================================================
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

# =============================================================================
# Models
# =============================================================================
class User(db.Model, UserMixin):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    senha = db.Column(db.String(255), nullable=False)
    maquinas_criadas = db.Column(db.Integer, default=0, nullable=False)
    cargo = db.Column(db.String(100), nullable=False, default=("user"))
    vip = db.Column(db.Boolean, default=False, nullable=False)
    vip_expira_em = db.Column(db.DateTime, nullable=True)
    apps_comprados = db.relationship("AppCompra", back_populates="user")
    total_logins = db.Column(db.Integer, default=0)

    maquinas = db.relationship("Maquinas", backref="dono", lazy=True, cascade="all, delete-orphan")
    __table_args__ = (Index("ix_users_email_unique", "email", unique=True),)

    def is_vip(self) -> bool:
        if not self.vip:
            return False
        if self.vip_expira_em and datetime.utcnow() > self.vip_expira_em:
            self.vip = False
            self.vip_expira_em = None
            try:
                db.session.add(self)
                db.session.commit()
            except Exception:
                db.session.rollback()
            return False
        return True


class Aplicativos(db.Model):
    __tablename__ = "aplicativos"
    id = db.Column(db.Integer, primary_key=True)
    nomeapp = db.Column(db.String(150), nullable=False)
    descricao = db.Column(db.String(1050), nullable=False)
    logoapp = db.Column(db.String(1050), nullable=False)
    # relação com compras
    compradores = db.relationship("AppCompra", back_populates="aplicativo")
class AppCompra(db.Model):
    """
    Tabela associativa que registra se o usuário comprou o app.
    """
    __tablename__ = "app_compras"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    app_id = db.Column(db.Integer, db.ForeignKey("aplicativos.id", ondelete="CASCADE"), nullable=False)
    autorizado = db.Column(db.Boolean, default=False)  # True se comprou/tem autorização

    user = db.relationship("User", back_populates="apps_comprados")
    aplicativo = db.relationship("Aplicativos", back_populates="compradores")
class Maquinas(db.Model):
    __tablename__ = "maquinas"
    id = db.Column(db.Integer, primary_key=True)
    maquina_nome = db.Column(db.String(150), nullable=False)
    maquina_senha = db.Column(db.String(255), nullable=False)
    maquina_dono_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    codigo = db.Column(db.String(6), nullable=False, unique=True, index=True)
    online = db.Column(db.Boolean, default=False, nullable=False)
    cpu = db.Column(db.String(64), nullable=True)
    ram = db.Column(db.Integer, nullable=True)
    disco = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("maquina_dono_id", "maquina_nome", name="uq_dono_nome"),
        Index("ix_maquinas_codigo_unique", "codigo", unique=True),
    )




with app.app_context():
    db.create_all()

# =============================================================================
# Helpers
# =============================================================================
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

def _cap(text: str, limit: int) -> str:
    return (text or "")[:limit]

def sanitize_name(s: str, *, limit: int = 150) -> str:
    s = _cap(s.strip(), limit)
    return re.sub(r"[^A-Za-z0-9 _\-\.áàâãéèêíïóôõöúçüÁÀÂÃÉÈÊÍÏÓÔÕÖÚÇÜ]", "", s)

def strong_code() -> str:
    return "".join(secrets.choice(string.digits) for _ in range(6))

def gerar_codigo_unico(max_tentativas: int = 40) -> str:
    for _ in range(max_tentativas):
        cod = strong_code()
        if not Maquinas.query.filter_by(codigo=cod).first():
            return cod
    raise RuntimeError("Não foi possível gerar código único.")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/loja")
@login_required
def loja():
    todos_apps = Aplicativos.query.all()
    compras_do_usuario = {compra.app_id: True for compra in current_user.apps_comprados}

    apps_para_template = []
    for app in todos_apps:
        apps_para_template.append({
            "id": app.id,
            "nomeapp": app.nomeapp,
            "descricao_curta": app.descricao[:80] + "..." if len(app.descricao) > 80 else app.descricao,
            "descricao": app.descricao,
            "logoapp": app.logoapp,
            "developer": "Black Store",
            "rating": 4.5,
            "screenshots": [
                "https://placehold.co/400x300/1a1a1a/e0e0e0?text=Screenshot+1",
                "https://placehold.co/400x300/1a1a1a/e0e0e0?text=Screenshot+2",
                "https://placehold.co/400x300/1a1a1a/e0e0e0?text=Screenshot+3"
            ]
        })

    return render_template("loja.html", user=current_user, apps=apps_para_template, compras=compras_do_usuario)



@app.route("/comprar_app/<int:app_id>", methods=["POST"])
@login_required
def comprar_app(app_id):
    user_id = current_user.id
    compra = AppCompra.query.filter_by(user_id=user_id, app_id=app_id).first()
    if not compra:
        compra = AppCompra(user_id=user_id, app_id=app_id, autorizado=True)
        db.session.add(compra)
    else:
        compra.autorizado = True
    db.session.commit()
    return redirect(url_for("loja"))
    return render_template("loja.html")
def _user_dir(uid: Optional[int] = None) -> str:
    if uid is None:
        if not current_user.is_authenticated:
            raise RuntimeError("Usuário não autenticado.")
        uid = current_user.id
    base = os.path.realpath(os.path.join(app.config["UPLOAD_FOLDER"], str(uid)))
    os.makedirs(base, exist_ok=True)
    os.makedirs(os.path.join(base, "uploads"), exist_ok=True)
    return base

def _safe_user_path(relative_path: str) -> str:
    base = _user_dir()
    alvo = os.path.realpath(os.path.join(base, "uploads", relative_path))
    if not alvo.startswith(os.path.join(base, "uploads")):
        raise ValueError("Caminho inválido.")
    return alvo

# =============================================================================
# Email de verificação
# =============================================================================
def enviar_email(codigo: str, destinatario: str) -> str:
    smtp_email = ("nebulaossac@gmail.com")  # ✅ correto
    smtp_pass = ("jjue lkzy sjjr bkns")    # ✅ correto
    if not smtp_email or not smtp_pass:
        print(f"[FALLBACK EMAIL] Para: {destinatario} | Código: {codigo}")
        return "FALLBACK_PRINTED"

    msg = MIMEText(f"Seu código de verificação NebulaOS é: {codigo}")
    msg['Subject'] = "Código de verificação NebulaOS"
    msg['From'] = smtp_email
    msg['To'] = destinatario

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_email, smtp_pass)
            server.sendmail(smtp_email, destinatario, msg.as_string())
        print(f"[EMAIL ENVIADO] Para: {destinatario} | Código: {codigo}")
        return "SENT"
    except Exception as e:
        print(f"[ERRO EMAIL] {e}")
        print(f"[FALLBACK EMAIL] Para: {destinatario} | Código: {codigo}")
        return "FALLBACK_PRINTED"
@app.route("/documentacao")
def documentacao():
    return render_template("documentacao.html")
# =============================================================================
# Login manager
# =============================================================================
@login_manager.user_loader
def load_user(user_id: str):
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None
@app.route("/codigoacesso")
def codigoacesso():
    return render_template("codigoacess.html")  # seu HTML de formulário

# Rota da landpage
@app.route("/landpage")
def landpage():
    return render_template("inicio.html")  # sua página inicial após acesso

# Verificação do código
@app.route("/codigoacessverifican", methods=['POST'])
def codigoacessverifican():
    codigo_acesso = "1520"
    codigo_user = request.form.get("codigo")
    
    if codigo_acesso == codigo_user:
        return redirect(url_for("login"))  # redireciona para landpage se correto
    else:
        return "Código inválido! Tente novamente."
    





import os
import subprocess
import threading
import json
import tempfile
from flask import request, jsonify
from flask_login import login_required

# Lock para evitar que múltiplas execuções ocorram simultaneamente
execution_lock = threading.Lock()

# --- IMPORTAÇÃO DO INTERPRETADOR ---
# O caminho para o módulo compiler.py é 'lineax.compiler' porque a pasta 'lineax'
# precisa ser um pacote Python.
try:
    from lineax.compiler import executar_codigo_lineax
except ImportError as e:
    # Se o interpretador não for encontrado, defina uma função de placeholder
    # para evitar erros, mas com uma mensagem clara para o desenvolvedor.
    print(f"Aviso: O módulo do interpretador Lineax (lineax.compiler) não foi encontrado. Erro: {e}")
    def executar_codigo_lineax(code):
        return [f"Erro: O módulo do interpretador Lineax (lineax.compiler) não foi encontrado."]
@app.route("/documentacao")
def documenacao():
    return render_template("documentacao.html")
 
# --- ROTA PARA EXECUTAR CÓDIGO ---
@app.route('/run-code', methods=['POST'])
def run_code():
    """
    Executa o código recebido do cliente, com base na linguagem especificada.
    Suporta Lineax, Python, e orienta para linguagens de front-end.
    """
    if not execution_lock.acquire(blocking=False):
        return jsonify({"output": "Aguarde, outra execução está em andamento."}), 429

    try:
        data = request.json
        code = data.get('code', '')
        language = data.get('language', 'plaintext')

        if not code or not language:
            return jsonify({'output': 'Erro: Código ou linguagem não fornecidos.'}), 400

        # --- LÓGICA DE EXECUÇÃO: LINGUAGEM LINEX (LX) ---
        if language in ['lineax', 'lx', 'sq']:
            try:
                # Chama a função do interpretador Lineax para processar o código
                # O nome da função está consistente com o nome importado acima
                output = executar_codigo_lineax(code)
                return jsonify({'output': '\n'.join(output)})
            except Exception as e:
                return jsonify({'output': f'Erro de execução do Lineax:\n{str(e)}'}), 400

        # --- LÓGICA DE EXECUÇÃO: LINGUAGEM PYTHON ---
        elif language == 'python':
            temp_filename = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.py', encoding='utf-8') as temp_file:
                    temp_file.write(code)
                    temp_filename = temp_file.name
                
                result = subprocess.run(
                    ['python3', temp_filename],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=True
                )
                return jsonify({'output': result.stdout})
            except subprocess.CalledProcessError as e:
                return jsonify({'output': f'Erro de execução:\n{e.stderr}'}), 400
            except subprocess.TimeoutExpired:
                return jsonify({'output': 'Erro: Tempo de execução excedido (15 segundos).'}), 400
            except FileNotFoundError:
                return jsonify({'output': 'Erro: O interpretador `python3` não foi encontrado.'}), 500
            except Exception as e:
                return jsonify({'output': f'Erro inesperado:\n{str(e)}'}), 500
            finally:
                if temp_filename and os.path.exists(temp_filename):
                    os.remove(temp_filename)

        # --- ORIENTAÇÃO PARA LINGUAGENS DE FRONT-END ---
        elif language in ['html', 'css', 'javascript']:
            return jsonify({"output": "Navegue para a aba de Visualização (Preview) para ver o resultado do seu código."})

        # --- CASO DE LINGUAGEM NÃO SUPORTADA ---
        else:
            return jsonify({'output': f'Linguagem "{language}" não suportada para execução.'})

    except Exception as e:
        return jsonify({'output': f'Erro interno do servidor: {str(e)}'}), 500
    finally:
        execution_lock.release()

# --- ROTA PARA ABRIR A IDE ---
@app.route("/iride", methods=["POST"])
@login_required
def iride():
    try:
        data = request.get_json()
        app_name = data.get("app")

        # Aqui você pode logar no banco, registrar o uso, etc.
        print(f"O usuário abriu o app: {app_name}")

        return jsonify(status="success", message=f"{app_name} aberto com sucesso!")
    except Exception as e:
        return jsonify(status="error", error=str(e))
    
@app.route('/run-terminal-command', methods=['POST'])
def run_terminal_command():
    import subprocess
    import shlex
    import os
    
    data = request.get_json(force=True)
    command = data.get('command', '').strip()

    if not command:
        return jsonify({'output': ''}), 200

    # Lógica aprimorada para o comando 'sqr install'
    if command.lower().startswith('sqr install'):
        try:
            package_name = command.split(' ')[2]
            return jsonify({'output': f"🎉 Parabéns! Você acabou de baixar o pacote '{package_name}'."}), 200
        except IndexError:
            return jsonify({'output': "Erro: Sintaxe incorreta. Use 'sqr install <nome_do_pacote>'."}), 400

    try:
        args = shlex.split(command)
        
        process = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
            cwd=os.getcwd()
        )
        
        output = process.stdout.strip()
        error = process.stderr.strip()

        if error:
            return jsonify({"output": error}), 500
        
        return jsonify({"output": output}), 200
        
    except subprocess.TimeoutExpired:
        return jsonify({"output": "Erro: O comando excedeu o tempo limite."}), 408
    except subprocess.CalledProcessError as e:
        return jsonify({"output": e.stderr.strip()}), 500
    except FileNotFoundError:
        return jsonify({"output": f"Erro: Comando '{args[0]}' não encontrado."}), 500
    except Exception as e:
        return jsonify({"output": f"Erro inesperado: {str(e)}"}), 500
@app.route("/ide")

def ide():
    """Renders the IDE page for the logged-in user."""
    user_dir = _user_dir()
    os.makedirs(user_dir, exist_ok=True)
    
    # You don't need to pass the file list here, as the frontend will fetch it.
    return render_template("ide.html")
@app.route("/list-files")
@login_required
def list_files_detailed():
    """Lista os arquivos da pasta do usuário com detalhes de linguagem."""
    user_dir = _user_dir()
    os.makedirs(user_dir, exist_ok=True)

    try:
        files = os.listdir(user_dir)
        file_list = [
            {
                "name": name,
                "language": "python" if name.endswith(".py") else "javascript"
            }
            for name in files
        ]
        return jsonify(file_list)
    except FileNotFoundError:
        return jsonify([]), 404
    except Exception as e:
        return jsonify({"output": f"Error listing files: {str(e)}"}), 500




@app.route("/")
def home():
    return render_template("login.html")

@app.route("/inicio")
@login_required
def inicio():
    maquinas = Maquinas.query.filter_by(maquina_dono_id=current_user.id).all()
    current_user.is_vip()
    return render_template("maquinas.html", maquinas=maquinas)

@app.route("/new_maquina")
@login_required
def new_maquina():
    return render_template("newMaquina.html")

@app.route("/criar_maquina", methods=["POST"])
@login_required
def criar_maquina():
    maquina_nome = sanitize_name(request.form.get("maquina") or "", limit=150)
    senha = _cap((request.form.get("senha") or ""), 255)
    cpu = _cap((request.form.get("cpu") or "").strip(), 64) or None
    ram, disco = None, None
    try:
        if request.form.get("ram"): ram = int(request.form.get("ram"))
        if request.form.get("disco"): disco = int(request.form.get("disco"))
    except ValueError:
        flash("Valores de RAM/Disco inválidos.", "error")
        return redirect(url_for("new_maquina"))

    if not maquina_nome or not senha or len(maquina_nome) < 3 or len(senha) < 8:
        flash("Nome ou senha inválidos.", "error")
        return redirect(url_for("new_maquina"))

    codigo = gerar_codigo_unico()
    try:
        nova = Maquinas(
            maquina_nome=maquina_nome,
            maquina_senha=generate_password_hash(senha),
            maquina_dono_id=current_user.id,
            codigo=codigo,
            cpu=cpu, ram=ram, disco=disco,
            online=False
        )
        db.session.add(nova)
        current_user.maquinas_criadas += 1
        db.session.commit()
        flash("Máquina criada!", "success")
        return redirect(url_for("inicio"))
    except IntegrityError:
        db.session.rollback()
        flash("Erro ao criar máquina.", "error")
        return redirect(url_for("new_maquina"))
@app.route("/entrar_maquina/<int:maquina_id>", methods=["GET","POST"])
@login_required
def entrar_maquina(maquina_id):
    maquina = Maquinas.query.filter_by(id=maquina_id, maquina_dono_id=current_user.id).first()
    if not maquina:
        flash("Máquina não encontrada.", "error")
        return redirect(url_for("inicio"))

    if request.method == "POST":
        senha = _cap((request.form.get("senha") or "").strip(), 255)
        if check_password_hash(maquina.maquina_senha, senha):
            session["codigo_maquina_atual"] = maquina.codigo

            # ✅ Marca a máquina como online
            maquina.online = True
            db.session.commit()

            flash(f"Você entrou na máquina {maquina.maquina_nome}.", "success")
            return redirect(url_for("area_de_trabalho"))

        flash("Senha incorreta.", "error")
        return redirect(url_for("entrar_maquina", maquina_id=maquina.id))

    return render_template("logs.html", maquina=maquina)
@app.route("/sair_maquina")
@login_required
def sair_maquina():
    codigo = session.pop("codigo_maquina_atual", None)
    if codigo:
        maquina = Maquinas.query.filter_by(codigo=codigo).first()
        if maquina:
            maquina.online = False
            db.session.commit()
    flash("Você saiu da máquina.", "info")
    return redirect(url_for("inicio"))

@app.route("/area_de_trabalho")
@login_required
def area_de_trabalho():
    codigo_maquina = session.get("codigo_maquina_atual", "")
    if not codigo_maquina:
        flash("Nenhuma máquina selecionada.", "error")
        return redirect(url_for("inicio"))
    
    maquina = Maquinas.query.filter_by(codigo=codigo_maquina, maquina_dono_id=current_user.id).first()
    if not maquina:
        flash("Máquina inválida.", "error")
        return redirect(url_for("inicio"))
    
    # Pega usuário atual
    user = User.query.filter_by(id=current_user.id).first()
    maquina.user_nome = user.nome  # adiciona atributo dinâmico

    return render_template("area_de_trabalho.html", m=maquina)

@app.route("/config_maquina/<int:maquina_id>", methods=["GET", "POST"])
@login_required
def config_maquina(maquina_id):
    maquina = Maquinas.query.filter_by(id=maquina_id, maquina_dono_id=current_user.id).first()
    if not maquina:
        flash("Máquina não encontrada.", "error")
        return redirect(url_for("inicio"))

    if request.method == "POST":
        maquina.maquina_nome = sanitize_name(request.form.get("maquina_nome") or maquina.maquina_nome, limit=150)

        nova_senha = (request.form.get("nova_senha") or "").strip()
        if nova_senha:
            if len(nova_senha) < 8:
                flash("Senha nova muito curta.", "error")
                return redirect(url_for("config_maquina", maquina_id=maquina.id))
            maquina.maquina_senha = generate_password_hash(nova_senha)

        maquina.cpu = _cap((request.form.get("cpu") or ""), 64) or None
        try:
            maquina.ram = int(request.form.get("ram")) if request.form.get("ram") else None
            maquina.disco = int(request.form.get("disco")) if request.form.get("disco") else None
        except ValueError:
            flash("RAM/Disco inválidos.", "error")
            return redirect(url_for("config_maquina", maquina_id=maquina.id))

        maquina.online = bool(request.form.get("online") == "1")

        try:
            db.session.add(maquina)
            db.session.commit()
            flash("Configurações salvas.", "success")
        except Exception:
            db.session.rollback()
            flash("Erro ao salvar configurações.", "error")

        return redirect(url_for("config_maquina", maquina_id=maquina.id))

    return render_template("config_maquina.html", maquina=maquina)


    return redirect(url_for("inicio"))
@app.route('/run_command', methods=['POST'])
def run_command():
    # Verifica autenticação
    if not current_user.is_authenticated:
        return jsonify({'error': 'Autenticação necessária.'}), 401

    data = request.get_json(silent=True)
    if not data or 'command' not in data:
        return jsonify({'error': 'Formato de comando inválido. Esperado JSON com chave "command".'}), 400

    command_from_user = data['command'].strip()
    command_parts = command_from_user.lower().split()

    # Verifica permissões para comandos administrativos
    if current_user.cargo not in ["admin", "admin_supremer", "programador_central"]:
        return jsonify({'error': f'Acesso negado. Seu cargo "{current_user.cargo}" não tem permissão.'}), 403

    # --- Execução de código via lineax ---
    if command_parts[0] == "exec_code":
        codigo = " ".join(command_parts[1:])
        import subprocess, tempfile, os, sys

        with tempfile.NamedTemporaryFile(mode="w+", suffix="lx", delete=False) as temp:
            temp.write(codigo)
            temp_filename = temp.name

        try:
            result = subprocess.run(
              [sys.executable, os.path.join("lineax", "compiler.py"), temp_filename],
              stdout=subprocess.PIPE,
              stderr=subprocess.PIPE,
              text=True,
              timeout=5
)
            output = result.stdout.splitlines()
            error = result.stderr.splitlines()
        except subprocess.TimeoutExpired:
            output = []
            error = ["❌ Execução excedeu o tempo limite!"]
        finally:
            os.remove(temp_filename)

        return jsonify({"output": output, "error": error})

    # --- Comando set_cargo ---
    elif len(command_parts) >= 3 and command_parts[0] == "set_cargo":
        email_alvo = command_parts[1]
        novo_cargo = command_parts[2]
        cargos_permitidos = ["user", "testador", "admin", "admin_supremer", "programador_teste", "programador_central"] 

        if novo_cargo not in cargos_permitidos:
            return jsonify({'error': f"Cargo inválido: '{novo_cargo}'. Permitidos: {', '.join(cargos_permitidos)}."}), 400

        user_to_update = User.query.filter_by(email=email_alvo).first()
        if user_to_update:
            if user_to_update.id == current_user.id:
                return jsonify({'error': 'Você não pode alterar seu próprio cargo.'}), 403
            user_to_update.cargo = novo_cargo
            db.session.commit()
            return jsonify({'output': f"Cargo de '{email_alvo}' alterado para '{novo_cargo}'."})
        return jsonify({'error': f"Usuário '{email_alvo}' não encontrado."}), 404

    # --- Outros comandos ---
    elif command_from_user == "whoami":
        return jsonify({
            'output': f"Você é: {current_user.nome} (ID: {current_user.id}, Cargo: {current_user.cargo})"
        })
    elif command_from_user == "help":
        return jsonify({'output': (
            "Comandos disponíveis:\n"
            "- set_cargo <email> <novo_cargo>\n"
            "- exec_code <código> (executa código no lineax)\n"
            "- whoami\n"
            "- clear\n"
            "- exit"
        )})
    elif command_from_user.lower() == "clear":
        return jsonify({'output': 'Comando clear recebido.'})
    else:
        return jsonify({'output': f"Comando '{command_from_user}' não reconhecido. Digite 'help'."})

@app.route("/registro", methods=["GET","POST"])
def registro():
    if request.method == "POST":
        nome = sanitize_name(request.form.get("nome") or "", limit=100)
        email = _cap((request.form.get("email") or "").strip().lower(), 255)
        senha = request.form.get("senha") or ""
        confirmar = request.form.get("confirmar_senha") or ""
        if not nome or not email or not senha or not confirmar:
            flash("Preencha todos os campos.", "error")
            return redirect(url_for("registro"))
        if not EMAIL_RE.match(email):
            flash("Email inválido.", "error")
            return redirect(url_for("registro"))
        if senha != confirmar:
            flash("Senhas não coincidem.", "error")
            return redirect(url_for("registro"))
        if len(senha) < 8:
            flash("Senha muito curta.", "error")
            return redirect(url_for("registro"))
        if User.query.filter_by(email=email).first():
            flash("Email já cadastrado.", "error")
            return redirect(url_for("registro"))
        codigo = strong_code()
        session["temp_user"] = {
            "nome": nome, "email": email,
            "senha": generate_password_hash(senha),
            "codigo": codigo,
            "expira_em": (datetime.utcnow() + timedelta(minutes=10)).isoformat()
        }
        enviar_email(codigo, email)
        flash("Código enviado para verificação (email ou console).", "success")
        return redirect(url_for("verified"))
    return render_template("registro.html")


@app.route("/verified", methods=["GET","POST"])
def verified():
    if request.method == "POST":
        temp = session.get("temp_user")
        if not temp:
            flash("Sessão expirada.", "error")
            return redirect(url_for("registro"))
        if datetime.utcnow() > datetime.fromisoformat(temp["expira_em"]):
            flash("Código expirou.", "error")
            session.pop("temp_user", None)
            return redirect(url_for("registro"))
        if _cap((request.form.get("codigo") or "").strip(), 6) != temp["codigo"]:
            flash("Código inválido.", "error")
            return redirect(url_for("verified"))
        try:
            novo = User(nome=temp["nome"], email=temp["email"], senha=temp["senha"])
            db.session.add(novo)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Email já usado.", "error")
            return redirect(url_for("login"))
        os.makedirs(_user_dir(novo.id), exist_ok=True)
        session.pop("temp_user", None)
        flash("Conta verificada! Faça login.", "success")
        return redirect(url_for("login"))
    return render_template("verified.html")

@app.route("/quiz")
def quiz():
    return render_template("quiz.html")
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = _cap((request.form.get("email") or "").strip().lower(), 255)
        senha = request.form.get("senha") or ""
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.senha, senha):
            # Autenticação bem-sucedida
            login_user(user)
            
            # Incrementa o contador de logins do usuário
            user.total_logins += 1
            db.session.commit()

            # Lógica de redirecionamento baseada no total de logins
            if user.total_logins == 1:
                flash("Bem-vindo! Por favor, responda a algumas perguntas rápidas.", "info")
                return redirect(url_for("quiz"))
            else:
                flash("Login realizado!", "success")
                return redirect(url_for("inicio"))
        
        flash("Email ou senha inválidos.", "error")
        return redirect(url_for("login"))
    
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("codigo_maquina_atual", None)
    flash("Você saiu da conta.", "success")
    return redirect(url_for("login"))
@app.route("/get-file-content/<path:filename>")
@login_required
def get_file_content(filename):
    user_dir = _user_dir()
    safe_name = secure_filename(filename)
    file_path = os.path.join(user_dir, safe_name)

    if not file_path.startswith(user_dir) or not os.path.exists(file_path):
        return jsonify({"status": "error", "error": "Arquivo não encontrado."}), 404

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Retorna o conteúdo do arquivo dentro do JSON
        return jsonify({"status": "success", "content": content})
    
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/files/save", methods=["POST"])
@login_required
def save_file():
    """Salva o conteúdo de um arquivo no diretório do usuário"""
    user_dir = _user_dir()
    os.makedirs(user_dir, exist_ok=True)

    data = request.get_json()
    filename = data.get("filename")
    content = data.get("content")

    if not filename:
        return jsonify({"status": "error", "error": "Filename is required"}), 400

    try:
        file_path = os.path.join(user_dir, filename)

        # Evita path traversal tipo ../../../
        if not file_path.startswith(user_dir):
            return jsonify({"status": "error", "error": "Invalid file path"}), 400

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        return jsonify({"status": "success", "message": f"File '{filename}' saved successfully"})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500
# =============================================================================
# Upload / Download
# =============================================================================
@app.route("/files/upload", methods=["POST"])
@login_required
def upload_file():
    if "file" not in request.files:
        return jsonify(status="error", error="Nenhum arquivo enviado")
    
    file = request.files["file"]
    if file.filename == "":
        return jsonify(status="error", error="Nenhum arquivo selecionado")
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        user_dir = _user_dir()
        os.makedirs(user_dir, exist_ok=True)
        file.save(os.path.join(user_dir, filename))
        return jsonify(status="success", filename=filename)
    
    return jsonify(status="error", error="Arquivo não permitido")
@app.route("/files/delete", methods=["DELETE"])
@login_required
def delete_file():
    """
    Deleta um arquivo do diretório do usuário.
    """
    try:
        data = request.get_json()
        if not data or "fileName" not in data:
            return jsonify(status="error", error="Nome do arquivo não fornecido"), 400

        filename = data.get("fileName", "").strip()
        if not filename:
            return jsonify(status="error", error="Nome do arquivo vazio"), 400

        # Validação do nome do arquivo para segurança
        safe_filename = secure_filename(filename)
        if safe_filename != filename:
            return jsonify(status="error", error="Nome de arquivo inválido"), 400

        user_dir = _user_dir()
        if not user_dir:
            return jsonify(status="error", error="Diretório do usuário não encontrado."), 500
        
        file_path = os.path.join(user_dir, safe_filename)

        # Verificação final para evitar acesso indevido a diretórios
        if not os.path.normpath(file_path).startswith(os.path.abspath(user_dir)):
            return jsonify(status="error", error="Operação não permitida"), 403

        # Verifica se o arquivo existe e o remove
        if os.path.exists(file_path):
            os.remove(file_path)
            return jsonify(status="success", message=f"Arquivo '{filename}' excluído com sucesso"), 200
        else:
            return jsonify(status="error", error="Arquivo não encontrado"), 404
            
    except Exception as e:
        print(f"Erro ao deletar arquivo: {e}")
        return jsonify(status="error", error=f"Erro interno do servidor: {str(e)}"), 500    
@app.route("/files/create", methods=["POST"])
@login_required
def create_file():
    data = request.get_json()
    if not data or "filename" not in data:
        return jsonify(status="error", error="Nome do arquivo não fornecido")

    filename = data["filename"].strip()
    if not filename:
        return jsonify(status="error", error="Nome do arquivo vazio")

    # Permitir apenas extensões específicas
    allowed_ext = ["txt", "md", "py", "js", "html", "css", "json", "sq"]
    ext = filename.split('.')[-1].lower()
    if ext not in allowed_ext:
        return jsonify(status="error", error="Extensão não permitida")

    filename = secure_filename(filename)
    user_dir = _user_dir()  # diretório do usuário
    os.makedirs(user_dir, exist_ok=True)
    file_path = os.path.join(user_dir, filename)

    if os.path.exists(file_path):
        return jsonify(status="error", error="Arquivo já existe")

    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write("")  # arquivo vazio
        return jsonify(status="success", filename=filename)
    except Exception as e:
        return jsonify(status="error", error=str(e))
@app.route("/folders/create", methods=["POST"])
@login_required
def create_folder():
    data = request.get_json()
    if not data or "foldername" not in data:
        return jsonify(status="error", error="Nome da pasta não fornecido")

    foldername = data["foldername"].strip()
    if not foldername:
        return jsonify(status="error", error="Nome da pasta vazio")

    # Sanitizar nome da pasta
    foldername = secure_filename(foldername)

    user_dir = _user_dir()  # diretório raiz do usuário
    os.makedirs(user_dir, exist_ok=True)

    folder_path = os.path.join(user_dir, foldername)

    if os.path.exists(folder_path):
        return jsonify(status="error", error="Pasta já existe")

    try:
        os.makedirs(folder_path, exist_ok=False)
        return jsonify(status="success", foldername=foldername)
    except Exception as e:
        return jsonify(status="error", error=str(e))

@app.route("/files/download/<path:filename>")
@login_required
def download_file(filename):
    user_dir = _user_dir()
    safe_name = secure_filename(filename)
    return send_from_directory(user_dir, safe_name, as_attachment=True)
@app.route("/files/list")
@login_required
def list_files():
    """Lista os arquivos da pasta do usuário com status de retorno."""
    user_dir = _user_dir()
    os.makedirs(user_dir, exist_ok=True)

    try:
        files = os.listdir(user_dir)
        return jsonify(status="success", files=files)
    except Exception as e:
        return jsonify(status="error", error=str(e)), 500


# =============================================================================
# Perfil
# =============================================================================
@app.route("/perfil")
@login_required
def perfil():
    is_vip = current_user.is_vip()
    return render_template("perfil.html", user=current_user, is_vip=is_vip)
# Diretório onde os arquivos de áudio estão armazenados.
AUDIO_DIR = 'sons'

# Mapeamento de perguntas e respostas com seus arquivos de áudio.
QA_MAPPING = [
    {
        "query": "Oi, quem é você",
        "response": "👋 Oi, tudo certo por aí? Eu sou a ALMA — Agente Linear Massivo Alternativo. Minha missão é te acompanhar em tudo que envolve a LINEX e a SQUARE CODE: tirar dúvidas, orientar seus projetos e, claro, trocar uma boa conversa sempre que quiser. Além de te ajudar, também vou interagir com você constantemente, fazendo perguntas e propondo desafios para garantir que você esteja aprendendo ao máximo comigo. Então, me conta: qual é o seu primeiro projeto de hoje?",
        "audio": "audio1.mp3",
        "audio2": "audio1v2.mp3",
        "audio3": "audio1v3.mp3"
    },
    {
        "query": "Oi, tudo bem? Queria saber mais sobre a LINEX.",
        "response": "Oi! Que ótimo que você quer conhecer a LINEX. Ela é uma ferramenta incrível da Square Code para te ajudar a criar e gerenciar projetos de forma super eficiente. Ela simplifica o fluxo de trabalho, desde a ideação até a entrega final. Para começarmos, qual tipo de projeto você está pensando em criar? Um site, um aplicativo, ou algo totalmente diferente?",
        "audio": "audio3.mp3",
        "audio2": "audio3v2.mp3",
        "audio3": "audio3v3.mp3"
    },
    {
        "query": "Estou com uma dúvida sobre a sintaxe da LINEX.",
        "response": "Perfeito! A sintaxe da LINEX é projetada para ser intuitiva e poderosa. Qual parte da sintaxe está te deixando com dúvidas? Pode ser sobre declaração de variáveis, loops, ou até mesmo como estruturar um componente. Me diga o que você precisa e eu te mostro um exemplo!",
        "audio": "audio4.mp3",
        "audio2": "audio4v2.mp3",
        "audio3": "audio4v3.mp3"
    },
    {
        "query": "Como eu faço para integrar a LINEX com uma API externa?",
        "response": "Essa é uma pergunta excelente! A LINEX foi feita para se integrar facilmente com outras tecnologias. Existem várias maneiras de fazer isso, mas a mais comum é usando as funcionalidades nativas de requisições HTTP da LINEX. Você já tem em mente a API que quer usar? Se sim, me conta qual é para eu te dar um passo a passo mais específico.",
        "audio": "audio5.mp3",
        "audio2": "audio5v2.mp3",
        "audio3": "audio5v3.mp3"
    },
    {
        "query": "Qual é a diferença entre a LINEX e outras linguagens de programação?",
        "response": "Ótima pergunta! A principal diferença da LINEX é a sua **abordagem linear e minimalista**. Enquanto outras linguagens podem ser mais complexas, a LINEX foca em resolver problemas de forma direta, sem a necessidade de um código enorme. Ela é ideal para quem busca produtividade e clareza. Você já tem experiência com outras linguagens? Se sim, qual?",
        "audio": "audio6.mp3",
        "audio2": "audio6v2.mp3",
        "audio3": "audio6v3.mp3"
    },
    {
        "query": "Preciso de ajuda para depurar um erro no meu código LINEX.",
        "response": "Fico feliz em te ajudar com isso! Depurar é uma parte fundamental do processo de desenvolvimento. A LINEX possui ferramentas de depuração bastante eficientes. Para que eu possa te guiar, você pode me mostrar a mensagem de erro que está recebendo ou a parte do código que você acha que está com problema? Juntos, vamos resolver isso rapidinho!",
        "audio": "audio7.mp3",
        "audio2": "audio7v2.mp3",
        "audio3": "audio7v3.mp3"
    },
    {
        "query": "Quais são os principais casos de uso da LINEX?",
        "response": "A LINEX é super versátil! Ela é muito utilizada em **desenvolvimento web**, tanto para o frontend quanto para o backend, em **automação de tarefas**, na criação de **aplicativos móveis** e até mesmo em projetos de **internet das coisas (IoT)**. Qual dessas áreas te interessa mais? Talvez possamos explorar um projeto nesse campo!",
        "audio": "audio8.mp3",
        "audio2": "audio8v2.mp3",
        "audio3": "audio8v3.mp3"
    },
    {
        "query": "Queria ver um exemplo prático de um projeto feito com LINEX.",
        "response": "Claro! Nada melhor que um exemplo para entender o potencial da LINEX. Que tipo de projeto você gostaria de ver? Um pequeno blog, um sistema de gerenciamento de tarefas ou um e-commerce? Me diz qual seria mais útil para você e eu te mostro um exemplo de código e a sua estrutura.",
        "audio": "audio9.mp3",
        "audio2": "audio9v2.mp3",
        "audio3": "audio9v3.mp3"
    },
    {
        "query": "Como posso aprender LINEX do zero?",
        "response": "Excelente! Se você está começando, o melhor caminho é seguir os tutoriais oficiais da Square Code, que são perfeitos para iniciantes. Eles cobrem desde o básico da sintaxe até projetos mais complexos. Além disso, eu estou aqui para te ajudar em cada passo. Qual tópico você gostaria de abordar primeiro para iniciarmos a sua jornada com a LINEX?",
        "audio": "audio10.mp3",
        "audio2": "audio10v2.mp3",
        "audio3": "audio10v3.mp3"
    },
    {
        "query": "Qual a melhor forma de organizar meu código LINEX em projetos grandes?",
        "response": "Essa é uma pergunta de quem já está pensando como um profissional! A organização do código é essencial. A LINEX incentiva a **modularização**, ou seja, dividir seu projeto em pequenos arquivos e módulos que se comunicam entre si. Isso torna o código mais fácil de ler e manter. Você já tem um projeto em mente? Podemos criar a estrutura de pastas juntos!",
        "audio": "audio11.mp3",
        "audio2": "audio11v2.mp3",
        "audio3": "audio11v3.mp3"
    },
    {
        "query": "A LINEX suporta programação orientada a objetos?",
        "response": "A LINEX tem uma abordagem um pouco diferente da programação orientada a objetos tradicional, focando mais na **programação funcional e procedural**. Ela utiliza conceitos como **composição** e **herança de protótipo**, que são poderosos e flexíveis. Se você já tem experiência com POO, vai se surpreender com a maneira elegante que a LINEX resolve esses problemas. Quer ver um exemplo de como criar um objeto e seus métodos na LINEX?",
        "audio": "audio12.mp3",
        "audio2": "audio12v2.mp3",
        "audio3": "audio12v3.mp3"
    },
    {
        "query": "Preciso de uma dica para otimizar a performance do meu código LINEX.",
        "response": "Ótima iniciativa! A otimização é crucial. Para melhorar a performance, é importante focar em evitar loops desnecessários, usar estruturas de dados eficientes e otimizar a forma como você faz requisições. Você está trabalhando em um projeto específico que está lento? Me conta um pouco sobre ele e eu te dou algumas dicas mais direcionadas.",
        "audio": "audio13.mp3",
        "audio2": "audio13v2.mp3",
        "audio3": "audio13v3.mp3"
    },
    {
        "query": "Como eu faço para publicar meu projeto LINEX online?",
        "response": "A LINEX tem um processo de publicação bem simples. Você geralmente vai precisar de um servidor ou serviço de hospedagem. O primeiro passo é compilar o seu código LINEX em um formato executável e depois fazer o upload para o servidor. Você já tem um serviço de hospedagem em mente, como a Amazon Web Services (AWS) ou a Vercel? Se sim, podemos ver os passos específicos para eles!",
        "audio": "audio14.mp3",
        "audio2": "audio14v2.mp3",
        "audio3": "audio14v3.mp3"
    },
    {
        "query": "Olá, queria saber como criar um novo projeto no Square OS.",
        "response": "Oi! Criar um projeto no Square OS é bem simples. Você pode ir no menu principal, clicar em 'Novo Projeto' e escolher o template que mais se adequa ao seu objetivo. Você já tem em mente o que quer construir hoje? Um site, um aplicativo, ou algo mais específico?",
        "audio": "audio15.mp3",
        "audio2": "audio15v2.mp3",
        "audio3": "audio15v3.mp3"
    },
    {
        "query": "Estou com problemas para instalar uma dependência na minha IDE da Square Code.",
        "response": "Hmm, isso pode acontecer. Qual é o nome da dependência que você está tentando instalar e qual a mensagem de erro que aparece? Na maioria das vezes, pode ser um problema de compatibilidade ou de permissões. Me diga os detalhes para que eu possa te guiar.",
        "audio": "audio16.mp3",
        "audio2": "audio16v2.mp3",
        "audio3": "audio16v3.mp3"
    },
    {
        "query": "Como eu faço para usar o sistema de controle de versão integrado do Square OS?",
        "response": "Ótima pergunta! O sistema de controle de versão do Square OS é uma mão na roda. Para começar, você precisa inicializar o repositório no seu projeto. O comando é `linex init`. Depois, você pode usar comandos como `linex commit` e `linex push` para salvar e compartilhar suas alterações. Quer que eu te mostre um exemplo de como fazer o primeiro commit?",
        "audio": "audio17.mp3",
        "audio2": "audio17v2.mp3",
        "audio3": "audio17v3.mp3"
    },
    {
        "query": "Qual a diferença entre o Square OS e outros sistemas operacionais como Windows ou Linux?",
        "response": "Essa é uma pergunta fundamental! A principal diferença é que o Square OS é um sistema operacional **otimizado para o desenvolvimento de software**, especialmente com as ferramentas da Square Code. Ele já vem com tudo que você precisa para programar, eliminando a necessidade de várias configurações manuais. Você já usou algum sistema operacional focado em desenvolvimento antes?",
        "audio": "audio18.mp3",
        "audio2": "audio18v2.mp3",
        "audio3": "audio18v3.mp3"
    },
    {
        "query": "Como posso personalizar a minha interface de usuário na IDE da Square Code?",
        "response": "A personalização é uma das partes mais divertidas! Na IDE da Square Code, você pode mudar o tema, as fontes, o esquema de cores e até a disposição dos painéis. Para começar, vá em 'Configurações' e depois em 'Aparência'. O que você gostaria de mudar primeiro, o tema escuro ou claro?",
        "audio": "audio19.mp3",
        "audio2": "audio19v2.mp3",
        "audio3": "audio19v3.mp3"
    },
    {
        "query": "Onde posso encontrar tutoriais sobre como usar a LINEX na IDE da Square Code?",
        "response": "Temos uma biblioteca de tutoriais completa dentro da IDE! Para acessá-la, vá em 'Ajuda' e depois em 'Tutoriais Interativos'. Eles são perfeitos para te guiar, desde o básico até projetos mais complexos. Qual tema você gostaria de explorar hoje: a sintaxe da LINEX, como usar o depurador, ou a integração com o Square OS?",
        "audio": "audio20.mp3",
        "audio2": "audio20v2.mp3",
        "audio3": "audio20v3.mp3"
    },
    {
        "query": "Quais são os atalhos de teclado mais úteis na IDE da Square Code?",
        "response": "Os atalhos de teclado são a chave para a produtividade! Alguns dos mais úteis são `Ctrl + S` para salvar, `Ctrl + F` para buscar e `Ctrl + Shift + P` para abrir a paleta de comandos. Você já usa algum atalho no seu dia a dia? Qual você mais gostaria de aprender?",
        "audio": "audio21.mp3",
        "audio2": "audio21v2.mp3",
        "audio3": "audio21v3.mp3"
    },
    {
        "query": "Preciso de ajuda com um erro de compilação no meu projeto LINEX.",
        "response": "Ok, vamos resolver isso. Erros de compilação geralmente indicam um problema na sintaxe do seu código. Qual é a mensagem de erro que está aparecendo no terminal de saída? Se você puder me mostrar a linha de código onde o erro ocorre, eu posso te dar uma solução mais rápida.",
        "audio": "audio22.mp3",
        "audio2": "audio22v2.mp3",
        "audio3": "audio22v3.mp3"
    },
    {
        "query": "Como eu faço para configurar um ambiente de desenvolvimento virtual no Square OS?",
        "response": "Configurar um ambiente virtual é super importante para manter seus projetos isolados. No Square OS, você pode usar o comando `linex env create`. Isso cria um ambiente limpo, sem interferir com outras dependências. Você já sabe qual versão da LINEX ou quais bibliotecas você precisa para o seu ambiente?",
        "audio": "audio23.mp3",
        "audio2": "audio23v2.mp3",
        "audio3": "audio23v3.mp3"
    },
    {
        "query": "O Square OS é compatível com outros programas que não sejam da Square Code?",
        "response": "Sim, com certeza! Embora o Square OS seja otimizado para o ecossistema Square Code, ele é baseado em tecnologias abertas e pode executar a maioria dos aplicativos e ferramentas de desenvolvimento comuns. Você está pensando em usar algum software específico? Me diz qual é para eu checar a compatibilidade para você.",
        "audio": "audio24.mp3",
        "audio2": "audio24v2.mp3",
        "audio3": "audio24v3.mp3"
    },
    {
        "query": "Quais são as melhores práticas para a escrita de código na LINEX?",
        "response": "Excelente pergunta! As melhores práticas de código com a LINEX incluem o uso de nomes de variáveis claros, a modularização de funções e a adição de comentários para explicar partes complexas. Seguir essas práticas torna seu código mais fácil de ler e manter. Você tem algum projeto em andamento onde podemos aplicar essas dicas agora mesmo?",
        "audio": "audio25.mp3",
        "audio2": "audio25v2.mp3",
        "audio3": "audio25v3.mp3"
    },
    {
        "query": "Como eu faço para otimizar o desempenho de um aplicativo mobile feito com LINEX?",
        "response": "A otimização é um passo crucial para um aplicativo de sucesso. Para melhorar a performance, você pode focar em reduzir o número de requisições de rede, otimizar o carregamento de imagens e usar a cache da maneira correta. A LINEX já tem recursos embutidos para ajudar nisso. Quer que eu te mostre um exemplo de como usar a cache no seu código?",
        "audio": "audio26.mp2",
        "audio2": "audio26v2.mp3",
        "audio3": "audio26v3.mp3"
    },
    {
        "query": "Qual é o futuro da LINEX e do ecossistema Square Code?",
        "response": "O futuro é muito promissor! A equipe da Square Code está constantemente trabalhando em novas funcionalidades, como IA integrada, ferramentas de colaboração em tempo real e melhorias na performance. A ideia é que o ecossistema se torne ainda mais intuitivo e poderoso. Que tipo de funcionalidade você gostaria de ver na LINEX ou no Square OS no futuro?",
        "audio": "audio27.mp3",
        "audio2": "audio27v2.mp3",
        "audio3": "audio27v3.mp3"
    },
    {
        "query": "Como eu faço para reportar um bug na IDE da Square Code?",
        "response": "Sua ajuda é muito valiosa para nós! Para reportar um bug, você pode ir no menu de 'Ajuda' na IDE e selecionar a opção 'Reportar Bug'. Isso abrirá um formulário onde você pode descrever o problema em detalhes. Se você já tem um bug em mente, me conte sobre ele e eu posso te ajudar a descrevê-lo da melhor forma.",
        "audio": "audio28.mp3",
        "audio2": "audio28v2.mp3",
        "audio3": "audio28v3.mp3"
    },
    {
        "query": "Existe algum recurso para iniciantes no Square OS?",
        "response": "Sim, com certeza! O Square OS é projetado para ser amigável para iniciantes. A primeira coisa que você vê ao abrir é um painel de boas-vindas com links para os tutoriais e a documentação. Além disso, eu estou aqui para te ajudar em cada passo. O que você quer aprender primeiro?",
        "audio": "audio29.mp3",
        "audio2": "audio29v2.mp3",
        "audio3": "audio29v3.mp3"
    },
    {
        "query": "Como eu faço para usar o terminal integrado do Square OS?",
        "response": "O terminal é uma das ferramentas mais poderosas! Ele é acessível diretamente da IDE, e você pode usá-lo para executar comandos do sistema operacional, rodar o seu código LINEX e instalar dependências. Basta clicar no ícone do terminal na barra de ferramentas. Qual comando você gostaria de tentar primeiro?",
        "audio": "audio30.mp3",
        "audio2": "audio30v2.mp3",
        "audio3": "audio30v3.mp3"
    },
    {
        "query": "É possível usar a LINEX para desenvolver jogos?",
        "response": "Sim, é totalmente possível! A LINEX, combinada com bibliotecas gráficas específicas, pode ser usada para o desenvolvimento de jogos 2D e até mesmo 3D mais simples. A sua sintaxe limpa ajuda a focar na lógica do jogo. Você tem alguma ideia de jogo que gostaria de criar?",
        "audio": "audio31.mp3",
        "audio2": "audio31v2.mp3",
        "audio3": "audio31v3.mp3"
    },
    {
        "query": "Como eu faço para criar testes automatizados no meu projeto LINEX?",
        "response": "Testes automatizados são essenciais para garantir a qualidade do seu código. A LINEX tem um framework de testes embutido. Você pode criar arquivos de teste e usar comandos como `linex test` para rodar os testes e verificar se tudo está funcionando como deveria. Você já tem alguma função que gostaria de testar?",
        "audio": "audio32.mp3",
        "audio2": "audio32v2.mp3",
        "audio3": "audio32v3.mp3"
    },
    {
        "query": "Qual a melhor forma de organizar as pastas do meu projeto no Square OS?",
        "response": "Uma boa organização é a base de um projeto escalável. Uma abordagem comum é separar o código por funcionalidade. Por exemplo, ter uma pasta para 'componentes', outra para 'serviços' e outra para 'utilidades'. Você já tem uma ideia de como quer estruturar seu projeto? Podemos criar o esqueleto juntos.",
        "audio": "audio33.mp3",
        "audio2": "audio33v2.mp3",
        "audio3": "audio33v3.mp3"
    },
    {
        "query": "Como eu faço para compartilhar meu projeto com um colega usando o Square OS?",
        "response": "O Square OS torna a colaboração super fácil. Você pode simplesmente usar o sistema de controle de versão para 'dar um push' no seu projeto para um repositório remoto, como o Git. Seu colega pode então 'clonar' o projeto para a máquina dele. Quer que eu te ajude a configurar o seu primeiro repositório remoto?",
        "audio": "audio34.mp3",
        "audio2": "audio34v2.mp3",
        "audio3": "audio34v3.mp3"
    },
    {
        "query": "Estou com um erro na minha função de loop, pode me ajudar?",
        "response": "Claro! Erros em loops são bastante comuns. Qual o tipo de loop que você está usando (`for`, `while`)? E qual a mensagem de erro que aparece? Muitas vezes, o problema está na condição de parada ou na inicialização da variável. Me mostre um pedacinho do seu código e eu te ajudo a identificar.",
        "audio": "audio35.mp3",
        "audio2": "audio35v2.mp3",
        "audio3": "audio35v3.mp3"
    },
    {
        "query": "Existe alguma ferramenta de design visual na IDE da Square Code para criar layouts de interface?",
        "response": "A IDE da Square Code foca mais na escrita de código, mas ela tem suporte para a visualização de interfaces criadas com frameworks compatíveis. O melhor é criar a interface em um framework de UI e a IDE te ajudará a visualizar o resultado. Você já tem um framework de design em mente?",
        "audio": "audio36.mp3",
        "audio2": "audio36v2.mp3",
        "audio3": "audio36v3.mp3"
    },
    {
        "query": "Como eu faço para criar um componente reutilizável em LINEX?",
        "response": "Criar componentes reutilizáveis é uma das grandes vantagens da LINEX. Você pode encapsular um bloco de código em um arquivo separado e depois 'importá-lo' onde precisar. Isso ajuda a manter o código limpo e evita repetição. Quer que eu te mostre um exemplo de como criar um componente para um botão ou um cartão de usuário?",
        "audio": "audio37.mp3",
        "audio2": "audio37v2.mp3",
        "audio3": "audio37v3.mp3"
    },
    {
        "query": "Qual é a diferença entre a LINEX e o Javascript?",
        "response": "Essa é uma ótima comparação! A LINEX e o Javascript são diferentes na sua filosofia. Enquanto o Javascript é mais voltado para a web, a LINEX tem uma abordagem mais ampla e minimalista. A LINEX foi projetada para ser mais rápida e fácil de ler, enquanto o Javascript é mais flexível, mas pode ser complexo. Em qual área você pretende usar a LINEX?",
        "audio": "audio38.mp3",
        "audio2": "audio38v2.mp3",
        "audio3": "audio38v3.mp3"
    },
    {
        "query": "É possível usar o Square OS sem internet?",
        "response": "Sim, com certeza! A maioria das funcionalidades do Square OS e da IDE da Square Code funciona offline. Você pode continuar programando, executando, e salvando seus projetos normalmente. Você só precisará de internet para coisas como instalar novas dependências ou fazer 'push' em um repositório remoto. O que você quer fazer offline?",
        "audio": "audio39.mp3",
        "audio2": "audio39v2.mp3",
        "audio3": "audio39v3.mp3"
    },
    {
        "query": "Como eu faço para criar um servidor web simples com LINEX?",
        "response": "Criar um servidor web com a LINEX é super rápido e fácil. Você pode usar a biblioteca de rede nativa da LINEX para isso. O processo é basicamente: importar a biblioteca, definir uma porta, e criar uma função para lidar com as requisições. Quer que eu te mostre o código para um servidor simples que diz 'Olá, mundo!'?",
        "audio": "audio40.mp3",
        "audio2": "audio40v2.mp3",
        "audio3": "audio40v3.mp3"
    },
    {
        "query": "Preciso de ajuda com a lógica do meu projeto, o código não está fazendo o que eu espero.",
        "response": "Sem problemas, a lógica é o coração do projeto. O que exatamente você quer que o seu código faça? E o que ele está fazendo no momento? Se você me der uma ideia do seu objetivo e o trecho de código, eu posso te ajudar a identificar onde a lógica precisa ser ajustada.",
        "audio": "audio41.mp3",
        "audio2": "audio41v2.mp3",
        "audio3": "audio41v3.mp3"
    },
    {
        "query": "Como eu faço para usar o depurador na IDE da Square Code?",
        "response": "O depurador é uma das ferramentas mais importantes! Para usá-lo, você pode ir em 'Executar' e depois em 'Depurar'. Você pode então colocar 'breakpoints' nas linhas de código que você quer inspecionar. Quando o código rodar, ele vai parar nesses pontos para que você possa ver os valores das variáveis. Você já tem um ponto no seu código que gostaria de investigar?",
        "audio": "audio42.mp3",
        "audio2": "audio42v2.mp3",
        "audio3": "audio42v3.mp3"
    },
    {
        "query": "Qual é o tipo de licença da LINEX e do Square OS?",
        "response": "A LINEX e o Square OS são distribuídos sob licenças que incentivam o uso e a inovação. A LINEX é de código aberto, enquanto o Square OS tem uma licença que permite o uso pessoal e profissional. Para saber os detalhes exatos, você pode verificar a documentação de licença. Você está pensando em usar para um projeto comercial ou pessoal?",
        "audio": "audio43.mp3",
        "audio2": "audio43v2.mp3",
        "audio3": "audio43v3.mp3"
    },
    {
        "query": "Como eu faço para atualizar a LINEX e o Square OS para a última versão?",
        "response": "Manter tudo atualizado é crucial! O Square OS tem um sistema de atualização automático. Você pode ir em 'Configurações' e depois em 'Atualizações' para verificar se há uma nova versão. Para a LINEX, você pode usar o comando `linex update` no terminal para garantir que você tenha a versão mais recente. Qual versão você está usando agora?",
        "audio": "audio44.mp3",
        "audio2": "audio44v2.mp3",
        "audio3": "audio44v3.mp3"
    },
    {
        "query": "Como eu faço para otimizar o tempo de carregamento do meu site feito com LINEX?",
        "response": "A velocidade de carregamento é fundamental! Para otimizar, você pode focar em minificar os arquivos CSS e Javascript, usar a compressão de imagens e fazer o carregamento assíncrono. A LINEX já tem ferramentas que ajudam com isso. Quer que eu te mostre como usar o otimizador de arquivos da LINEX?",
        "audio": "audio45.mp3",
        "audio2": "audio45v2.mp3",
        "audio3": "audio45v3.mp3"
    },
    {
        "query": "É possível usar a LINEX para desenvolver projetos de inteligência artificial?",
        "response": "Sim, a LINEX pode ser usada para projetos de IA, especialmente para a parte de processamento de dados e a integração com modelos de IA. Embora não seja tão especializada como Python para IA, a sua performance e a sua sintaxe limpa a tornam uma ótima opção para tarefas de pré-processamento. Você já tem um projeto de IA em mente?",
        "audio": "audio46.mp3",
        "audio2": "audio46v2.mp3",
        "audio3": "audio46v3.mp3"
    },
    {
        "query": "Qual a melhor forma de lidar com erros e exceções na LINEX?",
        "response": "Tratamento de erros é uma parte crucial de um código robusto. A LINEX tem um sistema de tratamento de exceções com blocos `try-catch` e `finally`. Isso permite que você execute um código que pode falhar de forma segura. Você tem uma parte do código que costuma dar erro? Me mostre para eu te ajudar a tratá-la.",
        "audio": "audio47.mp3",
        "audio2": "audio47v2.mp3",
        "audio3": "audio47v3.mp3"
    },
    {
        "query": "Como eu faço para usar a IDE da Square Code em modo de tela cheia?",
        "response": "Modo de tela cheia é ótimo para foco total no código! Na IDE da Square Code, você pode usar o atalho `F11` para alternar para o modo de tela cheia. Isso esconde os menus e barras de ferramentas, te dando mais espaço para trabalhar. Você já tentou usar esse atalho?",
        "audio": "audio48.mp3",
        "audio2": "audio48v2.mp3",
        "audio3": "audio48v3.mp3"
    },
    {
        "query": "É possível usar a LINEX para criar um aplicativo de desktop?",
        "response": "Sim, é totalmente possível! A LINEX, combinada com bibliotecas específicas para interfaces de desktop, pode ser usada para criar aplicativos para Windows, macOS e Linux. Você já tem uma ideia de aplicativo de desktop que gostaria de construir? Por exemplo, um editor de texto, um cliente de chat, etc.",
        "audio": "audio49.mp3",
        "audio2": "audio49v2.mp3",
        "audio3": "audio49v3.mp3"
    },
    {
        "query": "Como eu faço para configurar o tema escuro na IDE da Square Code?",
        "response": "O tema escuro é o favorito de muita gente! Para ativá-lo, você pode ir em 'Configurações' e depois em 'Aparência'. Lá você vai encontrar uma opção para mudar o tema para 'Escuro'. Se você quiser, pode personalizar as cores também. Qual cor de destaque você gosta?",
        "audio": "audio50.mp3",
        "audio2": "audio50v2.mp3",
        "audio3": "audio50v3.mp3"
    },
    {
        "query": "Onde posso encontrar a documentação da LINEX e do Square OS?",
        "response": "A documentação é a sua melhor amiga! A documentação completa da LINEX e do Square OS está disponível online e também offline, dentro da própria IDE. Você pode acessá-la diretamente no menu 'Ajuda'. O que você gostaria de procurar na documentação hoje?",
        "audio": "audio51.mp3",
        "audio2": "audio51v2.mp3",
        "audio3": "audio51v3.mp3"
    },
    {
        "query": "Preciso de ajuda para entender como funcionam as variáveis em LINEX.",
        "response": "Claro! As variáveis na LINEX são bem intuitivas. Você as declara com a palavra-chave `var` ou `let` e atribui um valor. A principal diferença é que `var` pode ser redeclarada e `let` não, o que ajuda a evitar erros. Qual a sua primeira variável que você quer declarar? Talvez um nome, uma idade ou uma lista de itens?",
        "audio": "audio52.mp3",
        "audio2": "audio52v2.mp3",
        "audio3": "audio52v3.mp3"
    },
    {
        "query": "Como eu faço para integrar a LINEX com um banco de dados?",
        "response": "A integração com bancos de dados é uma das funcionalidades mais poderosas da LINEX. A LINEX tem bibliotecas nativas para se conectar com os bancos de dados mais comuns, como SQL e NoSQL. Você precisa primeiro instalar a biblioteca para o banco de dados que você quer usar e depois configurar a conexão. Qual banco de dados você quer usar?",
        "audio": "audio53.mp3",
        "audio2": "audio53v2.mp3",
        "audio3": "audio53v3.mp3"
    },
    {
        "query": "Existe uma comunidade de desenvolvedores para LINEX e Square OS?",
        "response": "Sim, com certeza! Temos uma comunidade online muito ativa e acolhedora. Você pode encontrar grupos em fóruns, redes sociais e até em eventos presenciais. É o lugar perfeito para fazer perguntas, compartilhar seus projetos e se conectar com outros desenvolvedores. Você já usa alguma rede social para programar?",
        "audio": "audio54.mp3",
        "audio2": "audio54v2.mp3",
        "audio3": "audio54v3.mp3"
    },
    {
        "query": "Como eu faço para criar um teste de unidade para uma função na LINEX?",
        "response": "Criar testes de unidade é essencial para a qualidade do código. Com a LINEX, você pode criar um arquivo de teste e usar o framework de testes para verificar se a saída de uma função é a que você espera. Quer que eu te mostre um exemplo de como testar uma função que soma dois números?",
        "audio": "audio55.mp3",
        "audio2": "audio55v2.mp3",
        "audio3": "audio55v3.mp3"
    },
    {
        "query": "O que é a compilação 'on-the-fly' no Square OS?",
        "response": "A compilação 'on-the-fly' é uma funcionalidade que torna o desenvolvimento mais rápido. Ela significa que o Square OS compila o seu código LINEX em tempo real, enquanto você está digitando, e te mostra os erros e avisos instantaneamente, sem que você precise rodar o compilador manualmente. Isso é super útil, não é?",
        "audio": "audio56.mp3",
        "audio2": "audio56v2.mp3",
        "audio3": "audio56v3.mp3"
    },
    {
        "query": "Como eu faço para criar um componente de interface de usuário em LINEX?",
        "response": "A criação de componentes de UI na LINEX é bem modular. Você pode criar um arquivo para o componente e usar a sintaxe de templates da LINEX para definir a sua estrutura e estilização. Isso permite que você o reutilize em várias partes do seu projeto. Que tipo de componente você gostaria de criar?",
        "audio": "audio57.mp3",
        "audio2": "audio57v2.mp3",
        "audio3": "audio57v3.mp3"
    },
    {
        "query": "Qual a melhor forma de lidar com a segurança no meu projeto LINEX?",
        "response": "Segurança é um tópico crucial! Para garantir a segurança, você pode usar as bibliotecas de criptografia e autenticação da LINEX, validar as entradas do usuário, e nunca armazenar senhas em texto claro. Você está preocupado com a segurança de um site, um aplicativo ou outra coisa?",
        "audio": "audio58.mp3",
        "audio2": "audio58v2.mp3",
        "audio3": "audio58v3.mp3"
    },
    {
        "query": "Como eu faço para usar o sistema de 'live-preview' na IDE da Square Code?",
        "response": "O 'live-preview' é uma funcionalidade incrível! Ele permite que você veja as alterações no seu código LINEX em tempo real, sem precisar recarregar a página. Para ativá-lo, você pode ir no menu de 'Visualização' e selecionar a opção 'Live Preview'. Você está trabalhando em um projeto de interface de usuário?",
        "audio": "audio59.mp3",
        "audio2": "audio59v2.mp3",
        "audio3": "audio59v3.mp3"
    },
    {
        "query": "Preciso de ajuda para entender como funcionam as classes e objetos na LINEX.",
        "response": "A LINEX tem uma abordagem um pouco diferente para classes e objetos, com um foco em protótipos e composição em vez de herança clássica. Isso oferece mais flexibilidade. Basicamente, você cria um 'objeto' e depois 'anexa' métodos e propriedades a ele. Você tem alguma ideia de um objeto que queira criar? Talvez um carro ou um usuário?",
        "audio": "audio60.mp3",
        "audio2": "audio60v2.mp3",
        "audio3": "audio60v3.mp3"
    },
    {
        "query": "Qual a melhor forma de organizar o código de um aplicativo grande em LINEX?",
        "response": "Em projetos grandes, a organização é a chave para o sucesso. Uma boa prática é usar o padrão de 'Módulos', onde você divide o seu código em pequenos arquivos com responsabilidades específicas. Você pode ter um módulo para as funções de UI, outro para a lógica do negócio, e assim por diante. Você já começou a escrever o código do seu aplicativo?",
        "audio": "audio61.mp3",
        "audio2": "audio61v2.mp3",
        "audio3": "audio61v3.mp3"
    },
    {
        "query": "Como eu faço para usar as ferramentas de análise de código da IDE da Square Code?",
        "response": "As ferramentas de análise de código são muito úteis! A IDE da Square Code vem com um linter e um formatador de código embutidos. Eles te ajudam a encontrar erros, seguir as melhores práticas e manter a formatação do seu código consistente. Você já tentou usar o formatador de código? O atalho é `Ctrl + Shift + F`.",
        "audio": "ceo.mp3",
        "audio2": "audio62v2.mp3",
        "audio3": "audio62v3.mp3"
    },
    {
        "query": "Quem te desenvolveu",
        "response": "Fui desenvolvido por Miguel Viana, Fundador e CEO da SQUARE SYSTEMS.",
        "audio": "ceo.mp3"
        
    },
    {
        "query": "Abra a documentação",
        "response": "Claro, irei te levar até a documentação",
        "audio": "ceo1.mp3"
        
    }
]



# --- Início da nova lógica com LLM para similaridade ---

# --- Imports necessários ---
import random
import os

from sentence_transformers import SentenceTransformer, util



# Carrega o modelo de IA e os embeddings uma única vez no início.
model = SentenceTransformer('paraphrase-multilingual-mpnet-base-v2')
corpus_queries = [item["query"] for item in QA_MAPPING]
corpus_embeddings = model.encode(corpus_queries, convert_to_tensor=True)

# Limite de similaridade.
SIMILARITY_THRESHOLD = 0.65

# --- Funções de suporte ---
def find_best_match(user_query):
    """
    Usa o modelo de IA para encontrar a pergunta mais similar na lista.
    """
    query_embedding = model.encode(user_query, convert_to_tensor=True)
    cosine_scores = util.cos_sim(query_embedding, corpus_embeddings)[0]
    best_match_index = int(cosine_scores.argmax())
    best_score = float(cosine_scores[best_match_index])
    best_match_data = QA_MAPPING[best_match_index]
    
    return {
        "score": best_score,
        "audio_options": [best_match_data.get("audio"), best_match_data.get("audio2"), best_match_data.get("audio3")],
        "response": best_match_data.get("response")
    }

def normalize_text(text):
    """
    Normaliza o texto: remove acentos, pontuação e converte para minúsculas.
    Ex: "Olá, tudo bem?" -> "ola tudo bem"
    """
    if not isinstance(text, str):
        return ""
    text = ''.join(c for c in text if c.isalnum() or c.isspace())
    text = text.lower()
    return text.strip()

# --- Rota da API ---
@app.route('/process-audio', methods=['POST'])
def process_audio():
    """
    Recebe o texto do frontend, usa IA para encontrar a resposta mais similar
    e retorna o áudio e texto correspondentes.
    """
    try:
        data = request.get_json()
        user_text = data.get('text', '')

        if not user_text:
            return jsonify({"status": "error", "message": "Texto de entrada vazio."}), 400

        top_result = find_best_match(user_text)
        score_similaridade = top_result['score']

        # Se a similaridade for maior que o limite, usa a resposta da IA.
        if score_similaridade >= SIMILARITY_THRESHOLD:
            response_text = top_result.get('response', 'Desculpe, não encontrei uma resposta.')
            
            # --- VERIFICAÇÃO PRINCIPAL: REDIRECIONAR SE A INTENÇÃO FOR APRENDER A MEXER ---
            # A resposta 'Claro, irei te levar até a documentação' indica a intenção de redirecionamento.
            if response_text == "Claro, irei te levar até a documentação":
                # **CORREÇÃO AQUI**: Retorna um JSON que o frontend irá processar para redirecionar.
                return jsonify({
                    "status": "redirect",
                    "message": "Redirecionando para a documentação...",
                    "redirect_url": "/documentacao" 
                })

            # Se não for uma solicitação de redirecionamento, continua a lógica normal
            audio_choices = top_result.get("audio_options", [])
            response_audio_file = random.choice(audio_choices) if audio_choices else None
            
        else:
            # Lógica para resposta padrão de "não entendi".
            lista_audios_nao_entendi = [
                "desculpe_nao_entendi.mp3", "desculpe_nao_entendi1.mp3",
                "desculpe_nao_entendi2.mp3", "desculpe_nao_entendi3.mp3",
                "desculpe_nao_entendi4.mp3", "desculpe_nao_entendi5.mp3",
                "desculpe_nao_entendi6.mp3", "desculpe_nao_entendi7.mp3",
                "desculpe_nao_entendi8.mp3", "desculpe_nao_entendi9.mp3",
            ]
            response_audio_file = random.choice(lista_audios_nao_entendi)
            response_text = "Desculpe, não entendi. Poderia repetir?"
            score_similaridade = 0

        # Verifica se o arquivo de áudio existe no servidor
        if response_audio_file and not os.path.exists(os.path.join(AUDIO_DIR, response_audio_file)):
            return jsonify({
                "status": "error",
                "message": f"Arquivo de áudio não encontrado no servidor."
            }), 404

        # Retorna a resposta JSON normal
        return jsonify({
            "status": "success",
            "audio": response_audio_file,
            "resposta": response_text,
            "score_similaridade": score_similaridade
        })

    except Exception as e:
        print(f"Erro ao processar áudio: {e}")
        return jsonify({"status": "error", "message": "Ocorreu um erro no servidor."}), 500

@app.route('/get-audio/<path:filename>')
def get_audio(filename):
    """
    Envia o arquivo de áudio solicitado pelo frontend.
    """
    return send_from_directory(AUDIO_DIR, filename)
@app.route('/')
def index():
    """
    Serve o arquivo HTML principal da IDE.
    """
    return send_from_directory('.', 'index.html')

@app.route("/admin_dashboard")
@login_required
def admin_dashboard():
    try:
        total_usuarios = User.query.count()
        assinantes_vip = User.query.filter_by(vip=True).count()
        maquinas_ativas = Maquinas.query.filter_by(online=True).count()
        usuarios = User.query.all()
    except SQLAlchemyError as e:
        print(f"Erro ao carregar dados: {e}")
        total_usuarios = assinantes_vip = maquinas_ativas = 0
        usuarios = []

    return render_template(
        "admin_dashboard.html",
        total_usuarios=total_usuarios,
        assinantes_vip=assinantes_vip,
        maquinas_ativas=maquinas_ativas,
        usuarios=usuarios
    )

# ===================== ROTAS AJAX =====================

# Lista todos os usuários (JSON)
@app.route("/admin/users")
@login_required
def get_users():
    try:
        users = User.query.all()
        users_list = [{
            "id": u.id,
            "nome": u.nome,
            "email": u.email,
            "cargo": u.cargo,
            "vip": u.vip
        } for u in users]
        return jsonify(users_list)
    except SQLAlchemyError as e:
        print(f"Erro ao buscar usuários: {e}")
        return jsonify([]), 500

# Retorna estatísticas do dashboard (JSON)
@app.route("/admin/stats")
@login_required
def get_stats():
    try:
        total_usuarios = User.query.count()
        assinantes_vip = User.query.filter_by(vip=True).count()
        maquinas_ativas = Maquinas.query.filter_by(online=True).count()
        return jsonify({
            "total_usuarios": total_usuarios,
            "assinantes_vip": assinantes_vip,
            "maquinas_ativas": maquinas_ativas
        })
    except SQLAlchemyError as e:
        print(f"Erro ao buscar stats: {e}")
        return jsonify({
            "total_usuarios": 0,
            "assinantes_vip": 0,
            "maquinas_ativas": 0
        }), 500

# Atualiza o cargo do usuário
@app.route("/admin/user/<int:user_id>/cargo", methods=["PUT"])
@login_required
def update_cargo(user_id):
    data = request.get_json()
    cargo = data.get("cargo")
    try:
        user = User.query.get_or_404(user_id)
        user.cargo = cargo
        db.session.commit()
        return jsonify({"status": "success"})
    except SQLAlchemyError as e:
        db.session.rollback()
        print(f"Erro ao atualizar cargo: {e}")
        return jsonify({"status": "error"}), 500

# Ativa/Desativa VIP do usuário
@app.route("/admin/user/<int:user_id>/vip", methods=["PUT"])
@login_required
def toggle_vip(user_id):
    data = request.get_json()
    vip = data.get("vip", False)
    try:
        user = User.query.get_or_404(user_id)
        user.vip = vip
        db.session.commit()
        return jsonify({"status": "success"})
    except SQLAlchemyError as e:
        db.session.rollback()
        print(f"Erro ao atualizar VIP: {e}")
        return jsonify({"status": "error"}), 500

# Deleta um usuário e suas máquinas
@app.route("/admin/user/<int:user_id>/delete", methods=["DELETE"])
@login_required
def delete_user(user_id):
    try:
        user = User.query.get_or_404(user_id)
        Maquinas.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        return jsonify({"status": "success"})
    except SQLAlchemyError as e:
        db.session.rollback()
        print(f"Erro ao deletar usuário: {e}")
        return jsonify({"status": "error"}), 500

# Deleta uma máquina
@app.route("/admin/machine/<int:machine_id>/delete", methods=["DELETE"])
@login_required
def delete_machine(machine_id):
    try:
        machine = Maquinas.query.get_or_404(machine_id)
        db.session.delete(machine)
        db.session.commit()
        return jsonify({"status": "success"})
    except SQLAlchemyError as e:
        db.session.rollback()
        print(f"Erro ao deletar máquina: {e}")
        return jsonify({"status": "error"}), 500

# Pega as máquinas de um usuário
@app.route("/admin/user/<int:user_id>/machines")
@login_required
def get_user_machines(user_id):
    try:
        machines = Maquinas.query.filter_by(user_id=user_id).all()
        machine_list = [
            {
                "id": m.id,
                "maquina_nome": m.maquina_nome,
                "cpu": m.cpu,
                "ram": m.ram,
                "disco": m.disco,
                "online": m.online
            } for m in machines
        ]
        return jsonify(machine_list)
    except SQLAlchemyError as e:
        print(f"Erro ao buscar máquinas: {e}")
        return jsonify([]), 500

if __name__ == "__main__":
    os.makedirs("user", exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)

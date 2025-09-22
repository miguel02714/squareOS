import os
import threading
import json
import re
import requests  # Importação para o novo comando 'http'
import time
import math
import random

# Lock para evitar que múltiplos pedidos de execução rodem ao mesmo tempo
execution_lock = threading.Lock()

class LinexInterpreter:
    def __init__(self):
        self.variaveis = {}
        self.funcoes = {}
        self.entrada_simulada = []
        self.entrada_index = 0
        self.output = []
        self.safe_builtins = {
            'len': len,
            'str': str,
            'int': int,
            'float': float,
            'bool': bool,
            'math': math,
            'random': random
        }

    def _get_valor(self, expressao):
        """Obtém o valor de uma expressão ou variável."""
        expressao = expressao.strip()
        
        # Se for uma string literal
        if expressao.startswith('"') and expressao.endswith('"'):
            return expressao.strip('"')

        # Se for um número
        try:
            if '.' in expressao:
                return float(expressao)
            return int(expressao)
        except ValueError:
            pass

        # Se for uma variável
        if expressao in self.variaveis:
            return self.variaveis[expressao]
        
        # Se for acesso a propriedade de objeto JSON
        match_prop = re.match(r"(\w+)\.(.+)", expressao)
        if match_prop:
            var_json, prop = match_prop.groups()
            if var_json in self.variaveis and isinstance(self.variaveis[var_json], dict):
                partes = prop.split('.')
                valor = self.variaveis[var_json]
                try:
                    for p in partes:
                        if isinstance(valor, dict):
                            valor = valor.get(p)
                        else:
                            valor = None
                            break
                    if valor is not None:
                        return valor
                except (TypeError, KeyError):
                    pass
        
        return None

    def _avaliar_expressao(self, expressao):
        """Avalia uma expressão com suporte a concatenação, variáveis e funções."""
        expressao = expressao.strip()
        
        # Trata concatenação de strings
        if '+' in expressao:
            partes = expressao.split('+')
            conteudo = ""
            for p in partes:
                valor = self._get_valor(p.strip())
                if valor is not None:
                    conteudo += str(valor)
            return conteudo
        
        # Tenta avaliar uma expressão matemática complexa
        try:
            local_vars = {k: v for k, v in self.variaveis.items() if not isinstance(v, (dict, list))}
            local_vars.update(self.safe_builtins)
            return eval(expressao, {"__builtins__": self.safe_builtins}, local_vars)
        except (NameError, TypeError, SyntaxError):
            pass
            
        # Se não for uma expressão complexa, tenta avaliar como um valor simples
        valor = self._get_valor(expressao)
        if valor is not None:
            return valor

        raise ValueError(f"Expressão inválida ou variável não definida: '{expressao}'")

    def _avaliar_condicao(self, expressao):
        """Avalia uma condição de forma segura."""
        expressao = expressao.replace("and", " and ").replace("or", " or ")
        match = re.match(r"(.+?)\s*(==|!=|>|<|>=|<=)\s*(.+)", expressao.strip())
        if not match:
            return bool(self._avaliar_expressao(expressao))
        
        left, op, right = match.groups()
        valor_left = self._avaliar_expressao(left.strip())
        valor_right = self._avaliar_expressao(right.strip())

        if op == "==": return valor_left == valor_right
        if op == "!=": return valor_left != valor_right
        if op == ">": return valor_left > valor_right
        if op == "<": return valor_left < valor_right
        if op == ">=": return valor_left >= valor_right
        if op == "<=": return valor_left <= valor_right
        return False
        
    def _executar_comando(self, comando, linha_num):
        """Executa um único comando da linguagem Linex."""
        partes = comando.strip().split(maxsplit=1)
        if not partes: return
        comando_principal = partes[0].lower()
        argumentos = partes[1] if len(partes) > 1 else ""

        if comando_principal == "linex":
            sub_comando = argumentos.split(maxsplit=1)
            if sub_comando[0].lower() == "print":
                if len(sub_comando) < 2:
                    raise SyntaxError("Uso incorreto. Formato: linex print <expressao>")
                conteudo = self._avaliar_expressao(sub_comando[1])
                self.output.append(f"📢 {conteudo}")
            else:
                raise SyntaxError(f"Sub-comando '{sub_comando[0]}' desconhecido para 'linex'.")
        
        elif comando_principal == "var":
            match = re.match(r"(\w+)\s*=\s*(.*)", argumentos)
            if not match: 
                raise SyntaxError("Uso incorreto. Formato: var nome = valor")
            nome_var, valor_expr = match.groups()
            
            if valor_expr.strip().lower().startswith("calc"):
                expressao_calc = valor_expr.strip().split(maxsplit=1)[1]
                valor = self._avaliar_expressao(expressao_calc)
            else:
                valor = self._avaliar_expressao(valor_expr)
            
            self.variaveis[nome_var] = valor
            self.output.append(f"✅ Variável '{nome_var}' criada/atualizada.")
            
        elif comando_principal == "input":
            if not argumentos:
                raise SyntaxError("Uso incorreto. Formato: input <nome_da_variavel>")
            nome_var = argumentos.strip()
            if self.entrada_index < len(self.entrada_simulada):
                valor_input = self.entrada_simulada[self.entrada_index]
                self.entrada_index += 1
            else:
                valor_input = "Entrada do usuário"
            self.variaveis[nome_var] = valor_input
            self.output.append(f"⌨️ Variável '{nome_var}' recebeu entrada '{valor_input}'")

        elif comando_principal == "calc":
            if not argumentos:
                raise SyntaxError("Uso incorreto. Formato: calc <expressao>")
            resultado = self._avaliar_expressao(argumentos)
            self.output.append(f"🧮 Resultado: {resultado}")
            
        elif comando_principal == "save":
            match = re.match(r'"(.*)"', argumentos)
            if not match:
                raise SyntaxError("Uso incorreto. Formato: save \"nome_do_arquivo\"")
            filename = match.groups()[0]
            with open(f"{filename}.json", "w") as f:
                json.dump(self.variaveis, f, indent=4)
            self.output.append(f"💾 Variáveis salvas em {filename}.json")

        elif comando_principal == "load":
            match = re.match(r'"(.*)"', argumentos)
            if not match:
                raise SyntaxError("Uso incorreto. Formato: load \"nome_do_arquivo\"")
            filename = match.groups()[0]
            if not os.path.exists(f"{filename}.json"):
                raise FileNotFoundError(f"Arquivo '{filename}.json' não encontrado.")
            with open(f"{filename}.json", "r") as f:
                data = json.load(f)
                self.variaveis.update(data)
            self.output.append(f"📂 Variáveis carregadas de {filename}.json")

        elif comando_principal == "json":
            match = re.match(r"load\s+(\w+)\s+to\s+(\w+)", argumentos, re.IGNORECASE)
            if not match:
                raise SyntaxError("Uso incorreto. Formato: json load <variavel_string> to <variavel_json>")
            
            var_origem, var_destino = match.groups()
            if var_origem not in self.variaveis:
                raise NameError(f"Variável de origem '{var_origem}' não definida.")
            
            try:
                json_data = json.loads(self.variaveis[var_origem])
                self.variaveis[var_destino] = json_data
                self.output.append(f"📄 Conteúdo da variável '{var_origem}' carregado em formato JSON para '{var_destino}'.")
            except json.JSONDecodeError:
                raise ValueError(f"Conteúdo da variável '{var_origem}' não é um JSON válido.")

        elif comando_principal == "http":
            match = re.match(r"get\s+\"(.*?)\"\s+to\s+(\w+)", argumentos, re.IGNORECASE)
            if not match:
                raise SyntaxError("Uso incorreto. Formato: http get \"url\" to <nome_variavel>")
            url, nome_var = match.groups()
            try:
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                self.variaveis[nome_var] = response.text
                self.output.append(f"🌐 Requisição GET para `{url}` bem-sucedida. Conteúdo salvo em `{nome_var}`.")
            except requests.exceptions.RequestException as e:
                self.variaveis[nome_var] = None
                raise RuntimeError(f"Erro na requisição para `{url}`: {e}")

        elif comando_principal == "call":
            if not argumentos:
                raise SyntaxError("Uso incorreto. Formato: call <nome_funcao>")
            nome_funcao = argumentos.strip()
            if nome_funcao not in self.funcoes:
                raise NameError(f"Função '{nome_funcao}' não definida.")
            self.output.append(f"➡️ Chamando função '{nome_funcao}'...")
            self._executar_bloco(self.funcoes[nome_funcao], 0)
            self.output.append(f"⬅️ Finalizado função '{nome_funcao}'.")
            
        else:
            raise SyntaxError(f"Comando desconhecido: '{comando_principal}'")

    def _executar_bloco(self, bloco, linha_inicial):
        """Executa um bloco de código (código principal, função, if, loop)."""
        i = 0
        while i < len(bloco):
            linha = bloco[i].strip()
            linha_num_real = linha_inicial + i
            if not linha or linha.startswith("#"):
                i += 1
                continue
            
            partes = linha.split(maxsplit=1)
            comando_principal = partes[0].lower()
            argumentos = partes[1] if len(partes) > 1 else ""

            if comando_principal == "func":
                match = re.match(r"(\w+)\s+begin", argumentos, re.IGNORECASE)
                if not match: raise SyntaxError(f"Uso incorreto. Formato: func <nome_funcao> begin (linha {linha_num_real})")
                nome_funcao = match.groups()[0]
                
                bloco_func = []; j = i + 1
                while j < len(bloco) and not bloco[j].strip().lower().startswith("end func"):
                    bloco_func.append(bloco[j])
                    j += 1
                if j >= len(bloco): raise SyntaxError(f"Bloco da função '{nome_funcao}' não fechado com 'end func' (linha {linha_num_real})")
                
                self.funcoes[nome_funcao] = bloco_func
                self.output.append(f"📦 Função '{nome_funcao}' definida.")
                i = j + 1
            
            elif comando_principal == "if":
                match = re.match(r"(.*)\s+begin", argumentos, re.IGNORECASE)
                if not match: raise SyntaxError(f"Uso incorreto. Formato: if <condicao> begin (linha {linha_num_real})")
                condicao_expr = match.groups()[0]
                
                try:
                    condicao_eh_verdadeira = self._avaliar_condicao(condicao_expr)
                except Exception as e:
                    raise type(e)(f"Erro na condição do 'if': {e} (linha {linha_num_real})")
                
                bloco_if = []; j = i + 1
                bloco_else = []
                while j < len(bloco) and not bloco[j].strip().lower().startswith("else") and not bloco[j].strip().lower().startswith("end if"):
                    bloco_if.append(bloco[j])
                    j += 1
                
                if j < len(bloco) and bloco[j].strip().lower().startswith("else"):
                    k = j + 1
                    while k < len(bloco) and not bloco[k].strip().lower().startswith("end if"):
                        bloco_else.append(bloco[k])
                        k += 1
                    j = k
                    
                if j >= len(bloco) or not bloco[j].strip().lower().startswith("end if"): 
                    raise SyntaxError(f"Bloco 'if' não fechado com 'end if' (linha {linha_num_real})")
                
                if condicao_eh_verdadeira:
                    self.output.append(f"✅ Condição verdadeira. Executando bloco 'if'...")
                    self._executar_bloco(bloco_if, linha_num_real)
                else:
                    self.output.append(f"❌ Condição falsa. Pulando para o bloco 'else'...")
                    self._executar_bloco(bloco_else, linha_num_real)
                
                i = j + 1
            
            elif comando_principal == "loop":
                match = re.match(r"(\d+)\s+begin", argumentos, re.IGNORECASE)
                if not match: raise SyntaxError(f"Uso incorreto. Formato: loop <numero_vezes> begin (linha {linha_num_real})")
                
                try:
                    vezes = int(match.groups()[0])
                except ValueError:
                    raise ValueError(f"O número de repetições deve ser um número inteiro (linha {linha_num_real})")
                
                bloco_loop = []; j = i + 1
                while j < len(bloco) and not bloco[j].strip().lower().startswith("end loop"):
                    bloco_loop.append(bloco[j])
                    j += 1
                if j >= len(bloco): raise SyntaxError(f"Bloco 'loop' não fechado com 'end loop' (linha {linha_num_real})")
                
                self.output.append(f"🔄 Iniciando loop por {vezes} vezes...")
                for _ in range(vezes):
                    self._executar_bloco(bloco_loop, linha_num_real)
                self.output.append("✅ Loop finalizado.")
                i = j + 1
            else:
                try:
                    self._executar_comando(linha, linha_num_real)
                except (SyntaxError, ValueError, NameError, FileNotFoundError) as e:
                    raise type(e)(f"{e} (linha {linha_num_real})")
                except Exception as e:
                    raise Exception(f"Erro inesperado: {e} (linha {linha_num_real})")
                i += 1

    def executar_codigo_lineax(self, codigo, input_data=None):
        self.variaveis = {}; self.funcoes = {}; self.output = []
        if input_data:
            self.entrada_simulada = list(input_data)
        self.entrada_index = 0
        
        linhas = [linha for linha in codigo.splitlines() if linha.strip() and not linha.strip().startswith("#")]
        
        if not linhas or not linhas[0].strip().lower().startswith("linex init project"):
            return ["Erro: O projeto deve começar com 'linex init project'."]
        
        try:
            self.output.append("✅ Projeto iniciado com sucesso!")
            self._executar_bloco(linhas[1:], 1)
            self.output.append("\n**--- Fim da Execução ---**")
            return self.output
        except Exception as e:
            return [f"❌ Erro na execução: {str(e)}"]

def executar_codigo_lineax(codigo, input_data=None):
    with execution_lock:
        interpretador = LinexInterpreter()
        return interpretador.executar_codigo_lineax(codigo, input_data)
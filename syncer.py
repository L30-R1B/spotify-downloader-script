import json
import subprocess
import time
import logging
import sys
import os
from pathlib import Path

# --- Configuração de Caminhos ---
# O script assume que todos os arquivos (config, log, etc.)
# estão no mesmo diretório que o syncer.py
BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "config.json"
DEFECTIVE_FILE = BASE_DIR / "defective_songs.json"
LOG_FILE = BASE_DIR / "syncer.log"

# --- Configuração do Logging ---
# Gera logs no arquivo syncer.log E também no console (para o systemd)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)

def get_executable_path(name):
    """Encontra o caminho absoluto do executável (python/spotdl) no venv."""
    # Encontra o 'bin' do ambiente virtual atual
    python_path = Path(sys.executable)
    bin_dir = python_path.parent
    
    executable = bin_dir / name
    if not executable.exists():
        logging.critical(f"ERRO CRÍTICO: Não foi possível encontrar '{name}' em {bin_dir}")
        logging.critical("Certifique-se de que o 'spotdl' está instalado no mesmo venv que este script está rodando.")
        return None
    return str(executable)

def load_json(file_path):
    """Carrega um arquivo JSON de forma segura."""
    if not file_path.exists():
        logging.error(f"Arquivo não encontrado: {file_path}")
        return None
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        logging.error(f"Erro ao decodificar JSON: {file_path}")
        return None
    except Exception as e:
        logging.error(f"Erro ao ler arquivo {file_path}: {e}")
        return None

def save_json(file_path, data):
    """Salva dados em um arquivo JSON."""
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.error(f"Erro ao salvar arquivo {file_path}: {e}")

def load_defective_songs():
    """Carrega a lista de IDs de músicas defeituosas."""
    data = load_json(DEFECTIVE_FILE)
    if isinstance(data, list):
        return set(data)
    logging.warning("Arquivo defective_songs.json inválido. Começando com lista vazia.")
    return set()

def add_to_defective_list(song_id, defective_set):
    """Adiciona um ID de música à lista de defeituosos e salva."""
    if song_id not in defective_set:
        defective_set.add(song_id)
        save_json(DEFECTIVE_FILE, list(defective_set))
        logging.info(f"ID {song_id} adicionado à lista de defeituosos.")

def get_playlist_songs(playlist_url, spotdl_path):
    """Usa 'spotdl save' para obter todas as músicas de uma playlist."""
    logging.info(f"Buscando músicas da playlist: {playlist_url}")
    try:
        # CORREÇÃO: Trocado 'list' por 'save' e adicionado '--save-file -'
        # O '-' instrui o spotdl a imprimir o JSON no stdout em vez de um arquivo.
        result = subprocess.run(
            [spotdl_path, 'save', playlist_url, '--save-file', '-'],
            capture_output=True, text=True, check=True, encoding='utf-8',
            timeout=120 # Timeout de 2 minutos
        )
        
        stdout_lines = result.stdout.strip().splitlines()
        if not stdout_lines:
            logging.warning("Playlist não retornou músicas (pode estar vazia ou ser inválida).")
            return []
        
        # O 'save' retorna uma lista de JSONs, um por linha.
        # Precisamos envolvê-los em colchetes para ser um array JSON válido.
        json_string = f"[{','.join(stdout_lines)}]"
        songs = json.loads(json_string)
        logging.info(f"Encontradas {len(songs)} músicas na playlist.")
        return songs

    except subprocess.CalledProcessError as e:
        # A saída de erro do 'spotdl' vai para o stderr
        logging.error(f"Falha ao executar 'spotdl save' para {playlist_url}: {e.stderr}")
        return None
    except json.JSONDecodeError:
        logging.error(f"Falha ao decodificar a saída do 'spotdl save' para {playlist_url}.")
        return None
    except Exception as e:
        logging.error(f"Erro inesperado ao buscar playlist {playlist_url}: {e}")
        return None

def download_song(song, output_path, spotdl_path):
    """
    Tenta baixar uma única música.
    Retorna "downloaded", "skipped", ou "failed".
    """
    song_id = song.get("song_id")
    song_url = f"https://open.spotify.com/track/{song_id}"
    song_name = f"{song.get('name')} - {song.get('artists', ['?'])[0]}"
    
    # Garante que o diretório de saída exista
    os.makedirs(output_path, exist_ok=True)
    
    try:
        # Usamos --output para direcionar. spotdl naturalmente pula
        # músicas que já existem (baseado no nome do arquivo).
        result = subprocess.run(
            [
                spotdl_path, 'download', song_url,
                '--output', str(output_path) # Passa o caminho como string
            ],
            capture_output=True, text=True, check=True, encoding='utf-8',
            timeout=300 # Timeout de 5 minutos por música
        )
        
        output = result.stdout.lower()
        
        if "downloaded" in output:
            logging.info(f"BAIXADA: {song_name}")
            return "downloaded"
        elif "skipping" in output:
            # Isso é normal, a música já existe. Não logamos.
            return "skipped"
        else:
            # Caso estranho, mas bem-sucedido
            return "skipped"

    except subprocess.CalledProcessError as e:
        output = (e.stdout + e.stderr).lower()
        if "lookuperror" in output or "no results found" in output:
            logging.warning(f"FALHA (Lookup): Não foi possível encontrar '{song_name}' (ID: {song_id}).")
            return "failed"
        
        logging.error(f"FALHA (Subprocess) ao baixar {song_name}: {e.stderr}")
        return "failed"
    except Exception as e:
        logging.error(f"FALHA (Exceção) ao baixar {song_name}: {e}")
        return "failed"

def main():
    logging.info("--- Serviço de Sincronia Spotify Iniciado ---")
    
    spotdl_path = get_executable_path("spotdl")
    if not spotdl_path:
        sys.exit(1) # Sai se não encontrar o spotdl
    
    logging.info(f"Usando spotdl em: {spotdl_path}")
    
    while True:
        try:
            config = load_json(CONFIG_FILE)
            if not config:
                logging.error("Não foi possível carregar config.json. Tentando novamente em 60s.")
                time.sleep(60)
                continue
                
            defective_songs = load_defective_songs()
            interval_minutes = config.get("interval_minutes", 30)
            
            logging.info(f"Iniciando novo ciclo de verificação. {len(defective_songs)} músicas na lista de ignorados.")
            
            for playlist in config.get("playlists", []):
                playlist_url = playlist.get("url")
                playlist_path = Path(playlist.get("path"))
                
                if not playlist_url or not playlist_path:
                    logging.warning(f"Playlist inválida no config: {playlist}")
                    continue
                    
                logging.info(f"Processando playlist: {playlist_url} -> {playlist_path}")
                songs = get_playlist_songs(playlist_url, spotdl_path)
                
                if songs is None:
                    logging.error("Pulando esta playlist devido a erro anterior.")
                    continue

                new_downloads = 0
                for song in songs:
                    song_id = song.get("song_id")
                    if not song_id:
                        logging.warning(f"Música sem ID encontrada: {song.get('name')}")
                        continue
                    
                    # 1. Verifica se já falhou antes
                    if song_id in defective_songs:
                        continue
                        
                    # 2. Tenta baixar (spotdl vai pular se já existir)
                    status = download_song(song, playlist_path, spotdl_path)
                    
                    if status == "downloaded":
                        new_downloads += 1
                    elif status == "failed":
                        # Adiciona à lista de ignorados para o futuro
                        add_to_defective_list(song_id, defective_songs)
                
                logging.info(f"Playlist finalizada. {new_downloads} novas músicas baixadas.")
                
            logging.info(f"Ciclo de verificação completo. Aguardando {interval_minutes} minutos.")
            time.sleep(interval_minutes * 60)

        except KeyboardInterrupt:
            logging.info("Serviço interrompido manualmente.")
            break
        except Exception as e:
            logging.error(f"ERRO INESPERADO no loop principal: {e}")
            logging.error("Aguardando 5 minutos antes de tentar novamente...")
            time.sleep(300)

if __name__ == "__main__":
    main()

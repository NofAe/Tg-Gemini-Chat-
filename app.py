#!/usr/bin/env python3
"""
PDF to CBR Bot - Sin colas, modo por archivo, nombre automático, botón eliminar.
"""

import os
import sys
import tempfile
import threading
import time
import hashlib
import shutil
from urllib.parse import unquote, urlparse
from flask import Flask, request, send_from_directory, abort
import requests
import fitz
from PIL import Image
import zipfile
import io
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    logger.error("Falta TELEGRAM_BOT_TOKEN")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TOKEN}"
TEMP_DIR = '/tmp/cbr_conversions'
os.makedirs(TEMP_DIR, exist_ok=True)

app = Flask(__name__)
temp_files = {}           # {filename: expiration_time}
user_data = {}            # {chat_id: {'state': ..., 'pdf_path': ..., 'mode': ..., 'msg_id': ..., 'custom_name': ...}}
processed_updates = set()
processing_lock = {}      # {chat_id: bool} para evitar procesos paralelos por usuario

# ==================== FUNCIONES TELEGRAM ====================
def send_message(chat_id, text, keyboard=None):
    try:
        data = {'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown', 'disable_web_page_preview': True}
        if keyboard:
            data['reply_markup'] = keyboard
        r = requests.post(f"{API_URL}/sendMessage", json=data, timeout=15)
        if r.ok:
            return r.json().get('result', {}).get('message_id')
    except Exception as e:
        logger.error(f"send_message error: {e}")
    return None

def edit_message(chat_id, msg_id, text, keyboard=None):
    try:
        data = {'chat_id': chat_id, 'message_id': msg_id, 'text': text, 'parse_mode': 'Markdown'}
        if keyboard:
            data['reply_markup'] = keyboard
        requests.post(f"{API_URL}/editMessageText", json=data, timeout=15)
    except:
        pass

def delete_message(chat_id, msg_id):
    try:
        requests.post(f"{API_URL}/deleteMessage", json={'chat_id': chat_id, 'message_id': msg_id}, timeout=5)
    except:
        pass

def answer_callback(callback_id):
    try:
        requests.post(f"{API_URL}/answerCallbackQuery", json={'callback_query_id': callback_id}, timeout=5)
    except:
        pass

# ==================== GESTIÓN DE ARCHIVOS ====================
def save_file_locally(file_path, custom_name=None):
    if custom_name:
        safe = "".join(c for c in custom_name if c.isalnum() or c in ' ._-')[:50]
        name = f"{safe}.cbr"
    else:
        name = os.path.basename(file_path).replace('.pdf', '.cbr')
    dest = os.path.join(TEMP_DIR, name)
    shutil.copy2(file_path, dest)
    temp_files[name] = time.time() + 3600
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:10000")
    return f"{base_url}/download/{name}", name

def delete_local_file(filename):
    """Elimina un archivo del servidor si existe."""
    file_path = os.path.join(TEMP_DIR, filename)
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            return True
        except:
            return False
    return False

def extract_filename_from_url(url):
    """Extrae y decodifica el nombre del archivo desde la URL."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    filename = os.path.basename(path)
    if '.' in filename and not filename.lower().endswith('.pdf'):
        filename = filename.split('?')[0]
    if filename.lower().endswith('.pdf'):
        return filename[:-4]
    return filename.replace('.pdf', '')

def download_pdf(url):
    try:
        r = requests.get(url, stream=True, timeout=60)
        if r.status_code != 200:
            return None, None
        path = f"/tmp/{hashlib.md5(url.encode()).hexdigest()}.pdf"
        with open(path, 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        suggested_name = extract_filename_from_url(url)
        logger.info(f"PDF descargado: {path}, nombre sugerido: {suggested_name}")
        return path, suggested_name
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None, None
      # ==================== CONVERSIÓN PDF -> CBR ====================
def process_page(page, mode):
    if mode == 'manga':
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")
        return img_bytes, 'png'
    else:
        pix = page.get_pixmap(dpi=150)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        max_width = 1400
        if img.width > max_width:
            ratio = max_width / img.width
            new_size = (max_width, int(img.height * ratio))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=92, optimize=True)
        return output.getvalue(), 'jpg'

def convert_pdf_to_cbr(pdf_path, cbr_path, mode, chat_id, msg_id, custom_name):
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
    except Exception as e:
        edit_message(chat_id, msg_id, f"❌ No se puede abrir PDF: {e}")
        return None

    results = {}
    failed_pages = []
    start_time = time.time()
    last_update = 0
    mode_name = "Manga (PNG)" if mode == 'manga' else "Manhwa (JPEG)"

    for page_num in range(total_pages):
        try:
            page = doc[page_num]
            img_bytes, ext = process_page(page, mode)
            results[page_num] = (img_bytes, ext)
        except Exception as e:
            logger.error(f"Página {page_num+1} falló: {e}")
            failed_pages.append(page_num+1)
        processed = page_num + 1
        now = time.time()
        if now - last_update >= 1 or processed == total_pages:
            elapsed = now - start_time
            speed = processed / elapsed if elapsed > 0 else 0
            percent = processed / total_pages
            bar_length = 20
            filled = int(bar_length * percent)
            bar = '█' * filled + '░' * (bar_length - filled)
            text = (
                f"🎨 *Modo:* {mode_name}\n"
                f"📁 *Nombre:* {custom_name if custom_name else 'Sin nombre'}\n"
                f"📄 *Páginas:* {processed}/{total_pages}\n"
                f"⚡ *Velocidad:* {speed:.1f} img/s\n"
                f"📊 *Progreso:* `[{bar}]` {percent:.0%}\n"
                f"Estado: {'🟢' if not failed_pages else '⚠️'}\n\n"
                f"_Convirtiendo..._"
            )
            edit_message(chat_id, msg_id, text)
            last_update = now
    doc.close()

    if not results:
        edit_message(chat_id, msg_id, "❌ No se pudo extraer ninguna imagen.")
        return None

    with zipfile.ZipFile(cbr_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for page_num in sorted(results.keys()):
            img_bytes, ext = results[page_num]
            zf.writestr(f"{page_num+1:03d}.{ext}", img_bytes)

    fail_text = f"\n⚠️ Fallidas: {', '.join(map(str, failed_pages))}" if failed_pages else ""
    final_icon = "✅" if not failed_pages else "⚠️"
    edit_message(chat_id, msg_id, f"{final_icon} *Conversión completada*\n\nProcesadas: {len(results)}/{total_pages}{fail_text}\n\n🔄 Generando archivo...")
    return cbr_path
# ==================== PROCESO PRINCIPAL ====================
def start_conversion(chat_id, msg_id, pdf_path, mode, custom_name):
    """Inicia la conversión en un hilo separado."""
    def do_convert():
        out_path = pdf_path.replace('.pdf', '_cbr.cbr')
        result_path = convert_pdf_to_cbr(pdf_path, out_path, mode, chat_id, msg_id, custom_name)
        if result_path and os.path.exists(result_path):
            size_mb = os.path.getsize(result_path) / (1024 * 1024)
            download_url, filename = save_file_locally(result_path, custom_name)
            # Botón para eliminar el archivo
            keyboard = {
                'inline_keyboard': [
                    [{'text': '🗑️ Eliminar archivo', 'callback_data': f'delete_{filename}'}]
                ]
            }
            final_msg = (
                f"✅ *¡Listo!*\n\n"
                f"📁 *Nombre:* {custom_name if custom_name else 'Sin nombre'}\n"
                f"🎨 *Modo:* {'Manga' if mode=='manga' else 'Manhwa'}\n"
                f"💾 *Tamaño:* {size_mb:.1f} MB\n\n"
                f"🔗 [📥 Descargar CBR]({download_url})\n\n"
                f"_El enlace expira en 1 hora_"
            )
            edit_message(chat_id, msg_id, final_msg, keyboard)
            os.remove(result_path)
        else:
            edit_message(chat_id, msg_id, "❌ *Error en la conversión*.")
        try:
            os.remove(pdf_path)
        except:
            pass
        # Liberar el lock
        if chat_id in processing_lock:
            del processing_lock[chat_id]
        if chat_id in user_data:
            del user_data[chat_id]

    # Verificar si ya hay un proceso en curso para este chat
    if processing_lock.get(chat_id, False):
        send_message(chat_id, "⚠️ *Ya hay una conversión en curso*. Espera a que termine antes de enviar otro PDF.")
        return
    processing_lock[chat_id] = True
    threading.Thread(target=do_convert).start()

# ==================== MANEJADORES DE MENSAJES ====================
def ask_for_mode(chat_id, msg_id, pdf_path, suggested_name):
    """Pregunta el modo (Manga/Manhwa) y luego el nombre."""
    user_data[chat_id] = {
        'state': 'waiting_mode',
        'pdf_path': pdf_path,
        'suggested_name': suggested_name,
        'msg_id': msg_id
    }
    keyboard = {
        'inline_keyboard': [
            [{'text': '🖤 Manga (B/N, PNG alta calidad)', 'callback_data': 'mode_manga'}],
            [{'text': '🌈 Manhwa (Color, JPEG optimizado)', 'callback_data': 'mode_manhwa'}],
            [{'text': '❌ Cancelar', 'callback_data': 'cancel'}]
        ]
    }
    edit_message(chat_id, msg_id, "📌 *Selecciona el tipo de cómic:*\n\n• **Manga**: Ideal para blanco y negro, líneas nítidas. Usa PNG sin pérdida.\n• **Manhwa/Color**: Balance calidad/tamaño. Usa JPEG optimizado.", keyboard)

def ask_for_name(chat_id, msg_id, pdf_path, mode, suggested_name):
    """Pregunta el nombre personalizado."""
    user_data[chat_id] = {
        'state': 'waiting_name',
        'pdf_path': pdf_path,
        'mode': mode,
        'suggested_name': suggested_name,
        'msg_id': msg_id
    }
    edit_message(chat_id, msg_id, f"✏️ *Envía el nombre para el archivo CBR* (sin extensión)\nO envía `/` para usar el nombre sugerido: `{suggested_name if suggested_name else 'nombre_del_archivo'}`")

def handle_name_input(chat_id, text, data):
    """Procesa el nombre ingresado por el usuario."""
    if text == '/':
        custom_name = data.get('suggested_name')
    else:
        custom_name = text.strip()
    if not custom_name:
        custom_name = "documento"
    # Iniciar conversión
    start_conversion(chat_id, data['msg_id'], data['pdf_path'], data['mode'], custom_name)
  # ==================== WEBHOOK ====================
@app.route(f'/webhook/{TOKEN}', methods=['POST'])
def webhook():
    update = request.json
    if not update:
        return 'OK', 200

    update_id = update.get('update_id')
    if update_id in processed_updates:
        return 'OK', 200
    processed_updates.add(update_id)
    if len(processed_updates) > 1000:
        processed_updates.clear()

    # ---------- MENSAJES ----------
    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        if 'text' in msg:
            text = msg['text'].strip()

            if text == '/start':
                send_message(chat_id, "📚 *PDF to CBR Bot*\n\nConvierte PDF a CBR (imágenes por página).\n\n📌 *Cómo usar:*\n1. Envía un enlace PDF\n2. Elige el tipo (Manga/Manhwa)\n3. Personaliza el nombre (o usa el sugerido)\n4. Espera la conversión\n5. Descarga el CBR\n\n_El enlace expira en 1 hora_")
                return 'OK', 200

            # Si está esperando nombre
            if chat_id in user_data and user_data[chat_id].get('state') == 'waiting_name':
                handle_name_input(chat_id, text, user_data[chat_id])
                return 'OK', 200

            # Procesar URL de PDF
            if text.startswith('http'):
                # Verificar si ya está procesando
                if processing_lock.get(chat_id, False):
                    send_message(chat_id, "⚠️ *Ya hay una conversión en curso*. Espera a que termine antes de enviar otro PDF.")
                    return 'OK', 200

                status_msg = send_message(chat_id, "📥 *Descargando PDF...*")
                if not status_msg:
                    return 'OK', 200

                pdf_path, suggested_name = download_pdf(text)
                if not pdf_path:
                    edit_message(chat_id, status_msg, "❌ Error al descargar el PDF. Verifica el enlace.")
                    return 'OK', 200

                # Verificar que es un PDF válido
                try:
                    doc = fitz.open(pdf_path)
                    doc.close()
                except Exception as e:
                    edit_message(chat_id, status_msg, f"❌ PDF inválido: {str(e)[:100]}")
                    os.remove(pdf_path)
                    return 'OK', 200

                # Preguntar modo (Manga/Manhwa)
                ask_for_mode(chat_id, status_msg, pdf_path, suggested_name)
            else:
                send_message(chat_id, "❌ Envía un enlace HTTP/HTTPS a un PDF.\n\nUsa /start para ayuda.")
        else:
            # No es texto, ignorar
            pass

    # ---------- CALLBACKS ----------
    elif 'callback_query' in update:
        cb = update['callback_query']
        chat_id = cb['message']['chat']['id']
        msg_id = cb['message']['message_id']
        data = cb['data']
        answer_callback(cb['id'])

        if data == 'cancel':
            edit_message(chat_id, msg_id, "❌ Operación cancelada.")
            if chat_id in user_data:
                pdf_path = user_data[chat_id].get('pdf_path')
                if pdf_path and os.path.exists(pdf_path):
                    try:
                        os.remove(pdf_path)
                    except:
                        pass
                del user_data[chat_id]
            return 'OK', 200

        if data == 'mode_manga':
            if chat_id not in user_data or user_data[chat_id].get('state') != 'waiting_mode':
                edit_message(chat_id, msg_id, "❌ Sesión expirada. Envía el PDF nuevamente.")
                return 'OK', 200
            user_data[chat_id]['mode'] = 'manga'
            suggested = user_data[chat_id].get('suggested_name', 'documento')
            ask_for_name(chat_id, msg_id, user_data[chat_id]['pdf_path'], 'manga', suggested)
            return 'OK', 200

        if data == 'mode_manhwa':
            if chat_id not in user_data or user_data[chat_id].get('state') != 'waiting_mode':
                edit_message(chat_id, msg_id, "❌ Sesión expirada. Envía el PDF nuevamente.")
                return 'OK', 200
            user_data[chat_id]['mode'] = 'manhwa'
            suggested = user_data[chat_id].get('suggested_name', 'documento')
            ask_for_name(chat_id, msg_id, user_data[chat_id]['pdf_path'], 'manhwa', suggested)
            return 'OK', 200

        if data.startswith('delete_'):
            filename = data[7:]  # quita 'delete_'
            if delete_local_file(filename):
                edit_message(chat_id, msg_id, f"🗑️ *Archivo eliminado*: `{filename}`")
            else:
                edit_message(chat_id, msg_id, f"❌ No se pudo eliminar o el archivo ya no existe.")
            return 'OK', 200

    return 'OK', 200

@app.route('/download/<filename>')
def download_file(filename):
    if filename in temp_files and temp_files[filename] > time.time():
        return send_from_directory(TEMP_DIR, filename, as_attachment=True)
    abort(404)

@app.route('/')
def index():
    return "PDF to CBR Bot activo", 200

def setup_webhook():
    url = os.environ.get("RENDER_EXTERNAL_URL")
    if not url:
        logger.warning("RENDER_EXTERNAL_URL no definida")
        return
    webhook_url = f"{url}/webhook/{TOKEN}"
    try:
        r = requests.post(f"{API_URL}/setWebhook", json={'url': webhook_url})
        if r.ok:
            logger.info(f"✅ Webhook configurado: {webhook_url}")
        else:
            logger.error(f"Error: {r.text}")
    except Exception as e:
        logger.error(f"Excepción: {e}")

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    setup_webhook()
    app.run(host='0.0.0.0', port=port)

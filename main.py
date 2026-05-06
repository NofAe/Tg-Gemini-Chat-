#!/usr/bin/env python3
"""
Telegram Gemini Chat Bot – Conversaciones con IA + generación de imágenes
Soporte: Gemini 2.5 Flash (texto) + Nano Banana 2 (imágenes)
"""

import os
import sys
import logging
import threading
import time
import json
from datetime import datetime
from flask import Flask, request, jsonify
import requests
import google.generativeai as genai

# ==================== CONFIGURACIÓN ====================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Silenciar logs excesivos
logging.getLogger("google.generativeai").setLevel(logging.WARNING)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    logger.error("❌ Faltan TELEGRAM_TOKEN o GEMINI_API_KEY")
    sys.exit(1)

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Configurar Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Modelo de texto (rápido, inteligente)
text_model = genai.GenerativeModel('gemini-2.5-flash')

# Modelo de imágenes (Nano Banana 2) – requiere facturación o créditos
# Si no funciona, prueba con 'gemini-2.0-flash-exp-image-generation'
image_model = genai.GenerativeModel('gemini-2.0-flash-exp-image-generation')

# ==================== ALMACENAMIENTO DE CONVERSACIONES ====================
# Cada usuario tiene un historial de mensajes en formato Gemini: 
# [{"role": "user", "parts": ["texto"]}, {"role": "model", "parts": ["respuesta"]}, ...]
user_conversations = {}

# Opcional: persistencia en disco (archivo JSON) para que no se pierda al reiniciar
# Pero en Render el disco es efímero, podrías usar un volumen persistente.
# Por simplicidad, usamos memoria (se pierde al redeploy).
SAVE_TO_DISK = False  # Cambiar a True si quieres guardar en /tmp/conversations.json
CONVERSATIONS_FILE = "/tmp/conversations.json"

def load_conversations():
    """Carga historiales desde disco (si existe)"""
    if not SAVE_TO_DISK:
        return
    if os.path.exists(CONVERSATIONS_FILE):
        try:
            with open(CONVERSATIONS_FILE, 'r') as f:
                data = json.load(f)
                user_conversations.update(data)
            logger.info(f"✅ Cargadas conversaciones para {len(user_conversations)} usuarios")
        except Exception as e:
            logger.error(f"Error cargando conversaciones: {e}")

def save_conversations():
    """Guarda historiales en disco"""
    if not SAVE_TO_DISK:
        return
    try:
        # Copia ligera para serializar
        to_save = {uid: hist for uid, hist in user_conversations.items()}
        with open(CONVERSATIONS_FILE, 'w') as f:
            json.dump(to_save, f)
        logger.debug("Conversaciones guardadas")
    except Exception as e:
        logger.error(f"Error guardando conversaciones: {e}")

# Cargar al iniciar
load_conversations()

# Cada cierto tiempo guardar (cada 5 minutos)
def auto_save_loop():
    while True:
        time.sleep(300)
        save_conversations()

auto_save_thread = threading.Thread(target=auto_save_loop, daemon=True)
auto_save_thread.start()

# ==================== FUNCIONES DE TELGRAM ====================
def send_message(chat_id, text, parse_mode="Markdown", reply_markup=None):
    """Envía un mensaje de texto"""
    try:
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            data["reply_markup"] = reply_markup
        resp = requests.post(f"{API_URL}/sendMessage", json=data, timeout=15)
        if resp.ok:
            return resp.json().get("result", {}).get("message_id")
    except Exception as e:
        logger.error(f"Error send_message: {e}")
    return None

def send_photo(chat_id, photo_url, caption=None):
    """Envía una foto desde una URL pública (la imagen subida a un servicio)"""
    try:
        data = {
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption,
            "parse_mode": "Markdown"
        }
        resp = requests.post(f"{API_URL}/sendPhoto", json=data, timeout=30)
        if not resp.ok:
            logger.error(f"Error sendPhoto: {resp.text}")
    except Exception as e:
        logger.error(f"Error sendPhoto: {e}")

def send_chat_action(chat_id, action="typing"):
    """Indica que el bot está escribiendo"""
    try:
        requests.post(f"{API_URL}/sendChatAction", json={"chat_id": chat_id, "action": action}, timeout=5)
    except:
        pass

def delete_message(chat_id, message_id):
    try:
        requests.post(f"{API_URL}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
    except:
        pass

# ==================== GESTIÓN DE CONVERSACIONES ====================
def get_conversation_history(chat_id, max_tokens=4000):
    """Obtiene el historial del usuario en formato Gemini, limitando longitud"""
    history = user_conversations.get(chat_id, [])
    # Limitar a los últimos 20 mensajes para no exceder contexto
    if len(history) > 20:
        history = history[-20:]
    return history

def add_to_history(chat_id, role, content):
    """Añade un mensaje al historial (role: 'user' o 'model')"""
    if chat_id not in user_conversations:
        user_conversations[chat_id] = []
    user_conversations[chat_id].append({"role": role, "parts": [content]})
    # Guardado en disco cada cierto tiempo (o podemos guardar tras cada interacción)
    save_conversations()

def clear_conversation(chat_id):
    """Resetea el historial del usuario (comando /new)"""
    if chat_id in user_conversations:
        del user_conversations[chat_id]
    save_conversations()
    send_message(chat_id, "🧹 **Conversación reiniciada.**\n\n¡Puedes empezar de cero!")

# ==================== GENERACIÓN DE TEXTO (Gemini) ====================
def generate_text_response(chat_id, user_message, message_id):
    """Llama a Gemini 2.5 Flash con historial y envía la respuesta"""
    send_chat_action(chat_id, "typing")
    
    # Obtener historial
    history = get_conversation_history(chat_id)
    
    try:
        # Iniciar chat con Gemini (usando el historial)
        chat = text_model.start_chat(history=history)
        response = chat.send_message(user_message)
        reply = response.text
        
        # Guardar en historial
        add_to_history(chat_id, "user", user_message)
        add_to_history(chat_id, "model", reply)
        
        # Enviar respuesta (dividir si es muy larga)
        if len(reply) > 4000:
            for i in range(0, len(reply), 4000):
                send_message(chat_id, reply[i:i+4000])
        else:
            send_message(chat_id, reply)
        
        # Eliminar mensaje de "procesando" si existe
        if message_id:
            delete_message(chat_id, message_id)
            
    except Exception as e:
        logger.error(f"Error Gemini texto: {e}")
        send_message(chat_id, "❌ **Error al generar respuesta.**\n\nIntenta de nuevo más tarde.")
        delete_message(chat_id, message_id)

# ==================== GENERACIÓN DE IMÁGENES (Nano Banana 2) ====================
def generate_and_send_image(chat_id, prompt, message_id):
    """Genera una imagen con el modelo de Gemini y la envía como foto"""
    send_chat_action(chat_id, "upload_photo")
    
    try:
        # Llamada al modelo de imágenes
        response = image_model.generate_content(prompt)
        
        # La respuesta puede contener datos inline en base64 o una URL
        # Según la documentación, si genera imagen, viene como Part con inline_data
        if hasattr(response, '_result') and response._result.candidates:
            candidate = response._result.candidates[0]
            if candidate.content and candidate.content.parts:
                part = candidate.content.parts[0]
                if hasattr(part, 'inline_data') and part.inline_data:
                    import base64
                    image_data = part.inline_data.data
                    mime_type = part.inline_data.mime_type  # image/png
                    # Guardar temporalmente en /tmp
                    img_filename = f"/tmp/gemini_img_{chat_id}_{int(time.time())}.png"
                    with open(img_filename, "wb") as f:
                        f.write(base64.b64decode(image_data) if isinstance(image_data, str) else image_data)
                    # Subir a Telegram
                    with open(img_filename, "rb") as f:
                        files = {"photo": f}
                        requests.post(f"{API_URL}/sendPhoto", data={"chat_id": chat_id, "caption": f"🖼️ **Generado con:** {prompt[:100]}"}, files=files, timeout=30)
                    os.remove(img_filename)
                    delete_message(chat_id, message_id)
                    return
        # Si no hay inline_data, quizás la respuesta tiene texto con un enlace o error
        # En algunos casos el modelo devuelve texto explicando que no pudo generar
        send_message(chat_id, f"⚠️ No se pudo generar la imagen. El modelo respondió:\n\n{response.text[:500]}")
        delete_message(chat_id, message_id)
        
    except Exception as e:
        logger.error(f"Error generando imagen: {e}")
        send_message(chat_id, "❌ **Error al generar la imagen.**\n\n- ¿Tienes activada la facturación en Google Cloud?\n- El modelo de imágenes requiere plan de pago o créditos de prueba.")
        delete_message(chat_id, message_id)

# ==================== WEBHOOK ====================
app = Flask(__name__)

@app.route(f'/webhook/{TELEGRAM_TOKEN}', methods=['POST'])
def webhook():
    update = request.json
    if not update:
        return jsonify({'ok': True})
    
    # Procesar mensajes
    if 'message' in update:
        msg = update['message']
        chat_id = msg['chat']['id']
        
        # Comandos de texto
        if 'text' in msg:
            text = msg['text'].strip()
            
            if text == '/start':
                welcome = (
                    "🤖 **¡Hola! Soy un bot con Gemini 2.5 Flash**\n\n"
                    "Puedes conversar conmigo de forma natural. También genero imágenes.\n\n"
                    "📌 **Comandos:**\n"
                    "/new – Reiniciar la conversación (borra el historial)\n"
                    "/imagen <descripción> – Genera una imagen con Nano Banana 2\n"
                    "/help – Mostrar esta ayuda\n\n"
                    "✨ ¡Solo escríbeme lo que quieras!"
                )
                send_message(chat_id, welcome)
                # Iniciar historial vacío implícitamente
                clear_conversation(chat_id)  # Para empezar limpio
                return jsonify({'ok': True})
            
            elif text == '/help':
                help_text = (
                    "❓ **Ayuda del Bot**\n\n"
                    "📝 **Conversación:**\n"
                    "Envía cualquier mensaje de texto y te responderé manteniendo el contexto.\n\n"
                    "🖼️ **Generar imagen:**\n"
                    "`/imagen un gato astronauta en la luna`\n\n"
                    "🔄 **Reiniciar chat:**\n"
                    "`/new` – Borra el historial y empieza de cero.\n\n"
                    "⚠️ **Nota:** El modelo de imágenes requiere facturación en Google Cloud (o usar los $300 de crédito inicial).\n\n"
                    "📌 **Mi código fuente:** [GitHub](https://github.com/tuusuario/tubot)"
                )
                send_message(chat_id, help_text)
                return jsonify({'ok': True})
            
            elif text == '/new':
                clear_conversation(chat_id)
                return jsonify({'ok': True})
            
            elif text.startswith('/imagen'):
                # Extraer prompt después del comando
                prompt = text[8:].strip()
                if not prompt:
                    send_message(chat_id, "❌ **Usa:** `/imagen descripción de la imagen`")
                    return jsonify({'ok': True})
                # Mostrar mensaje de "generando"
                status_msg = send_message(chat_id, f"🎨 **Generando imagen...**\n\n_"Prompt: {prompt[:100]}_")
                # Lanzar hilo para no bloquear
                threading.Thread(target=generate_and_send_image, args=(chat_id, prompt, status_msg)).start()
                return jsonify({'ok': True})
            
            else:
                # Mensaje de texto normal -> respuesta de IA
                status_msg = send_message(chat_id, "🤔 *Pensando...*")
                threading.Thread(target=generate_text_response, args=(chat_id, text, status_msg)).start()
    
    # Callbacks (si usas botones, pero por ahora no)
    elif 'callback_query' in update:
        # Puedes añadir botones para borrar historial, etc.
        cb = update['callback_query']
        callback_id = cb['id']
        requests.post(f"{API_URL}/answerCallbackQuery", json={'callback_query_id': callback_id}, timeout=5)
    
    return jsonify({'ok': True})

# ==================== ENDPOINTS ADICIONALES ====================
@app.route('/')
def index():
    return "Bot de Gemini activo", 200

def setup_webhook():
    """Configura el webhook en Telegram"""
    webhook_base = os.environ.get("RENDER_EXTERNAL_URL")
    if not webhook_base:
        logger.warning("RENDER_EXTERNAL_URL no definida, webhook no configurado")
        return False
    webhook_url = f"{webhook_base}/webhook/{TELEGRAM_TOKEN}"
    try:
        resp = requests.post(f"{API_URL}/setWebhook", json={"url": webhook_url})
        if resp.ok:
            logger.info(f"✅ Webhook configurado: {webhook_url}")
            return True
        else:
            logger.error(f"❌ Error webhook: {resp.text}")
    except Exception as e:
        logger.error(f"❌ Excepción configurando webhook: {e}")
    return False

# ==================== INICIO ====================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    
    # Configurar webhook solo si estamos en Render (variable RENDER existe)
    if os.environ.get("RENDER"):
        setup_webhook()
    else:
        logger.info("Modo local - no se configura webhook")
    
    logger.info("="*60)
    logger.info("🤖 BOT CON GEMINI 2.5 FLASH + NANO BANANA 2")
    logger.info("="*60)
    logger.info(f"✅ Bot iniciado en puerto {port}")
    logger.info("📌 Comandos: /start, /new, /imagen <prompt>, /help")
    logger.info("="*60)
    
    app.run(host='0.0.0.0', port=port)

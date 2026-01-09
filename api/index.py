import os
import io
import json
import pytz
import datetime
import httplib2
import phonenumbers
import sib_api_v3_sdk

from flask import Flask, request, jsonify
from flask_cors import CORS
# MUDANÇA 1: Importar Credentials para ler o token do ambiente
from google.oauth2.credentials import Credentials 
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload
from sib_api_v3_sdk.rest import ApiException
from google_auth_httplib2 import AuthorizedHttp

app = Flask(__name__)

lista_de_origins = [
    "https://kyriefelix.netlify.app",
    "https://kyriefelix.netlify.app/",
    "https://www.seu-dominio-personalizado.com",
    "http://localhost:5000",
    "http://localhost:3000",
    "http://localhost:5173",
]

CORS(app, resources={r"/api/*": {"origins": lista_de_origins}})

WORK_START_HOUR = 9
WORK_END_HOUR = 21
SLOT_INTERVAL_MINUTES = 30
APPOINTMENT_DURATION_MINUTES = 60
CLEANUP_BUFFER_MINUTES = 30
CALENDAR_ID = "kayre.felix01@gmail.com"
TATUADOR_WHATSAPP = "5511937244363" 
SPREADSHEET_ID = "1qHbaHa2Jci7d87QAdLmE9X-yxsAHLBX3ge-twNFEiyk"
LOCAL_TIMEZONE = pytz.timezone('America/Sao_Paulo')
DRIVE_FOLDER_ID = "1RwN2S2ZmEZyVyqzxkzTBHhvQnZy37OU-" 
ARTIST_EMAIL = "kayre.felix01@gmail.com" 
SENDER_EMAIL = "kayre.felix01@gmail.com"
SENDER_NAME = "Estúdio de Tatuagem"

BREVO_API_KEY = os.environ.get('BREVO_API_KEY')

SCOPES = [
    "https://www.googleapis.com/auth/calendar", 
    "https://www.googleapis.com/auth/spreadsheets", 
    "https://www.googleapis.com/auth/drive"
]

def get_google_creds():
    token_json_content = os.environ.get('GOOGLE_TOKEN_JSON')
    
    # Se não achar na variável, tenta ler do arquivo local (para quando você rodar no PC)
    if not token_json_content:
        if os.path.exists('token.json'):
            try:
                with open('token.json', 'r') as f:
                    token_json_content = f.read()
            except Exception as e:
                print(f"Erro ao ler arquivo local: {e}")
    
    if not token_json_content:
        print("ERRO CRÍTICO: Token não encontrado nem nas variáveis nem no arquivo local.")
        return None

    try:
        # Converte a string JSON em um dicionário python
        if isinstance(token_json_content, str):
            token_info = json.loads(token_json_content)
        else:
            token_info = token_json_content
            
        # Cria as credenciais a partir do dicionário
        creds = Credentials.from_authorized_user_info(token_info, SCOPES)
        return creds
    except Exception as e:
        print(f"ERRO CRÍTICO: Falha ao carregar credenciais: {e}")
        return None

@app.route('/api/horarios', methods=['GET'])
def get_available_slots():
    try:
        creds = get_google_creds()
        if not creds:
             return jsonify({"error": "Erro de configuração de autenticação."}), 500
            
        http = httplib2.Http(timeout=30)
        authorized_http = AuthorizedHttp(creds, http=http) 
        
        calendar_service = build("calendar", "v3", http=authorized_http, cache_discovery=False)
        
        date_str = request.args.get('date')
        if not date_str:
            return jsonify({"error": "Data é obrigatória"}), 400
            
        selected_date = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
        time_min = datetime.datetime.combine(selected_date, datetime.time.min).isoformat() + 'Z'
        time_max = datetime.datetime.combine(selected_date, datetime.time.max).isoformat() + 'Z'
        
        events_result = calendar_service.events().list(
            calendarId=CALENDAR_ID, timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy='startTime'
        ).execute()
        busy_slots_events = events_result.get('items', [])
        available_slots = []
        
        slot_start_naive = datetime.datetime.combine(selected_date, datetime.time(WORK_START_HOUR))
        work_end_naive = datetime.datetime.combine(selected_date, datetime.time(WORK_END_HOUR))
        slot_start = LOCAL_TIMEZONE.localize(slot_start_naive)
        work_end_time = LOCAL_TIMEZONE.localize(work_end_naive)

        while slot_start < work_end_time:
            slot_end = slot_start + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
            if slot_end > work_end_time: break
            is_slot_busy = False
            for event in busy_slots_events:
                start_str = event['start'].get('dateTime') or event['start'].get('date')
                end_str = event['end'].get('dateTime') or event['end'].get('date')
                
                if 'T' not in str(start_str): continue # Ignora eventos de dia inteiro

                event_start = datetime.datetime.fromisoformat(start_str)
                effective_event_end = datetime.datetime.fromisoformat(end_str) + datetime.timedelta(minutes=CLEANUP_BUFFER_MINUTES)
                if slot_start < effective_event_end and slot_end > event_start:
                    is_slot_busy = True
                    break 
            if not is_slot_busy:
                available_slots.append(slot_start.strftime('%H:%M'))
            slot_start += datetime.timedelta(minutes=SLOT_INTERVAL_MINUTES)
        return jsonify(available_slots)
    except Exception as e:
        print(f"Ocorreu um erro em /api/horarios: {e}")
        return jsonify({"error": "Erro interno no servidor ao buscar horários."}), 500


@app.route('/api/agendar', methods=['POST'])
def create_booking():
    try:
        creds = get_google_creds()
        if not creds:
             return jsonify({"error": "Erro de configuração de autenticação."}), 500

        http = httplib2.Http(timeout=60)
        authorized_http = AuthorizedHttp(creds, http=http)
        
        calendar_service = build("calendar", "v3", http=authorized_http, cache_discovery=False)
        sheets_service = build("sheets", "v4", http=authorized_http, cache_discovery=False)
        drive_service = build("drive", "v3", http=authorized_http, cache_discovery=False)
        
        data = request.form
        image_file = request.files.get('ideia-imagem')
        telefone_cliente = data.get('telefone')
        if not telefone_cliente: return jsonify({"error": "O número de telefone é obrigatório."}), 400
        
        try:
            if not phonenumbers.is_valid_number(phonenumbers.parse(telefone_cliente, "BR")):
                raise ValueError("Número inválido.")
        except Exception as e:
            return jsonify({"error": "O número de telefone fornecido não parece ser válido."}), 400

        image_link = "Nenhuma imagem enviada."
        if image_file:
            try:
                file_metadata = {'name': f"Ref_{data.get('nome')}_{data.get('date')}.jpg", 'parents': [DRIVE_FOLDER_ID]}
                media = MediaIoBaseUpload(io.BytesIO(image_file.read()), mimetype=image_file.mimetype, resumable=True)
                
                uploaded_file = drive_service.files().create(
                    body=file_metadata, media_body=media, fields='id, webViewLink'
                ).execute()
                
                drive_service.permissions().create(fileId=uploaded_file.get('id'), body={'type': 'anyone', 'role': 'reader'}).execute()
                image_link = uploaded_file.get('webViewLink')
            except HttpError as error:
                print(f"ERRO DRIVE: {error}")
                image_link = "Erro ao fazer upload da imagem (Verifique Cotas)."
        
        description_text = (f"Contato: {data.get('telefone', 'N/A')}\n\nIdeia: {data.get('ideia', 'N/A')}\n\nReferência: {image_link}")
        start_dt = datetime.datetime.strptime(f"{data['date']} {data['time']}", '%Y-%m-%d %H:%M')
        end_dt = start_dt + datetime.timedelta(minutes=APPOINTMENT_DURATION_MINUTES)
        event_body = {
            'summary': f"Tatuagem - {data.get('nome', 'Novo Cliente')}", 'description': description_text,
            'start': {'dateTime': start_dt.isoformat(), 'timeZone': 'America/Sao_Paulo'},
            'end': {'dateTime': end_dt.isoformat(), 'timeZone': 'America/Sao_Paulo'},
        }
        created_event = calendar_service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
        
        try:
            agendamento_formatado = start_dt.strftime('%d/%m/%Y %H:%M')
            new_row_values = [agendamento_formatado, data.get('nome'), data.get('telefone')]
            
            sheets_service.spreadsheets().values().append(
                spreadsheetId=SPREADSHEET_ID, range="Registros!A1", 
                valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS", 
                body={"values": [new_row_values]}
            ).execute()
        except Exception as e:
            print(f"ERRO SHEETS: {e}")

        try:
            if BREVO_API_KEY:
                configuration = sib_api_v3_sdk.Configuration()
                configuration.api_key['api-key'] = BREVO_API_KEY
                api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))
                html_content=f"""<h3>✅ Agendamento recebido!</h3><p><strong>Cliente:</strong> {data.get('nome')}</p><p><strong>Contato:</strong> {data.get('telefone')}</p><p><strong>Data:</strong> {data.get('date')} às {data.get('time')}</p><p><strong>Ideia:</strong> {data.get('ideia', 'N/A')}</p><p><strong>Referência:</strong> <a href="{image_link}">{image_link}</a></p>"""
                send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(to=[{"email": ARTIST_EMAIL}], html_content=html_content, sender={"name": SENDER_NAME, "email": SENDER_EMAIL}, subject=f"Novo Agendamento: {data.get('nome')}")
                api_instance.send_transac_email(send_smtp_email)
        except ApiException as e:
            print(f"ERRO BREVO: {e}")

        # --- CORREÇÃO AQUI ---
        # Agora retornamos o whatsappNumber para o frontend usar
        return jsonify({
            "message": "Agendamento criado!", 
            "eventId": created_event['id'],
            "whatsappNumber": TATUADOR_WHATSAPP 
        }), 201
        
    except Exception as e:
        print(f"ERRO CRÍTICO: {e}")
        return jsonify({"error": "Erro interno no servidor."}), 500

if __name__ == '__main__':
    app.run(debug=True)

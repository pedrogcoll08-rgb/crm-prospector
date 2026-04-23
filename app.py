from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import time
import json
import re
from datetime import date
import threading
import os
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
SHEETS_URL     = os.environ.get('SHEETS_URL', '')
ZONA           = os.environ.get('ZONA', 'El Raal, Murcia, Spain')
RADIO_METROS   = int(os.environ.get('RADIO_METROS', '10000'))

TIPOS = [
    'restaurant','bar','cafe','beauty_salon','hair_care',
    'gym','doctor','dentist','real_estate_agency','car_repair',
    'car_dealer','clothing_store','florist','bakery','electrician',
    'plumber','lawyer','accounting','supermarket','pharmacy',
    'veterinary_care','lodging','hardware_store','painter','store'
]

job_status = {
    'running': False, 'progreso': 0, 'total_tipos': len(TIPOS),
    'tipo_actual': '', 'analizados': 0, 'prospectos': 0,
    'importados': 0, 'log': [], 'done': False, 'error': None
}

def log(msg):
    job_status['log'].append(msg)
    if len(job_status['log']) > 100:
        job_status['log'] = job_status['log'][-100:]
    print(msg)

def geocode_zona(zona):
    url = 'https://maps.googleapis.com/maps/api/geocode/json'
    r = requests.get(url, params={'address': zona, 'key': GOOGLE_API_KEY}, timeout=10)
    data = r.json()
    if data.get('results'):
        loc = data['results'][0]['geometry']['location']
        return loc['lat'], loc['lng']
    raise Exception(f'No se encontró: {zona}')

def buscar_negocios(lat, lng, tipo):
    url = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
    negocios = []
    params = {'location': f'{lat},{lng}', 'radius': RADIO_METROS, 'type': tipo, 'key': GOOGLE_API_KEY}
    while True:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        negocios.extend(data.get('results', []))
        token = data.get('next_page_token')
        if not token:
            break
        time.sleep(2)
        params = {'pagetoken': token, 'key': GOOGLE_API_KEY}
    return negocios

def obtener_detalle(place_id):
    url = 'https://maps.googleapis.com/maps/api/place/details/json'
    r = requests.get(url, params={
        'place_id': place_id,
        'fields': 'name,formatted_phone_number,website,formatted_address,rating,user_ratings_total',
        'key': GOOGLE_API_KEY
    }, timeout=10)
    return r.json().get('result', {})

def analizar_web(url_web):
    """Analiza la web usando requests + BeautifulSoup (sin navegador)"""
    if not url_web:
        return 'sin_web', 'Sin web registrada en Google'
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'es-ES,es;q=0.9',
        }
        r = requests.get(url_web, headers=headers, timeout=12, allow_redirects=True)
        if r.status_code != 200:
            return 'web_mala', f'Error HTTP {r.status_code}'

        html = r.text.lower()
        soup = BeautifulSoup(r.text, 'lxml')

        # Webs placeholder
        placeholders = [
            'domain for sale', 'this domain', 'buy this domain',
            'coming soon', 'próximamente', 'under construction',
            'en construcción', 'godaddy', 'parking'
        ]
        for p in placeholders:
            if p in html:
                return 'web_mala', f'Web placeholder: "{p}"'

        problemas = []

        # Poco contenido
        texto = soup.get_text(separator=' ', strip=True)
        palabras = len(texto.split())
        if palabras < 200:
            problemas.append(f'Poco contenido ({palabras} palabras)')

        # Sin HTTPS
        if not url_web.startswith('https'):
            problemas.append('Sin HTTPS')

        # Sin meta description
        meta = soup.find('meta', attrs={'name': re.compile('description', re.I)})
        if not meta or not meta.get('content', '').strip():
            problemas.append('Sin SEO básico')

        # Sin teléfono visible
        if not re.search(r'[\+6789]\d[\d\s\-]{7,}', texto):
            problemas.append('Sin teléfono visible')

        # Sin imágenes
        imgs = soup.find_all('img')
        if len(imgs) < 3:
            problemas.append(f'Solo {len(imgs)} imagen/es')

        if len(problemas) >= 2:
            return 'web_mala', ' | '.join(problemas)

        return 'web_ok', 'Web aceptable'

    except requests.exceptions.Timeout:
        return 'web_mala', 'Web muy lenta o caída'
    except requests.exceptions.ConnectionError:
        return 'web_mala', 'Web no accesible'
    except Exception as e:
        return 'web_mala', f'Error: {str(e)[:60]}'

def enviar_al_crm(negocio):
    if not SHEETS_URL:
        return False
    try:
        import urllib.request, urllib.parse
        fd = urllib.parse.urlencode({'payload': json.dumps({
            'action': 'add_prospecto', 'data': negocio
        })}).encode()
        req = urllib.request.Request(SHEETS_URL, data=fd, method='POST')
        urllib.request.urlopen(req, timeout=15)
        return True
    except:
        return False

def run_busqueda():
    global job_status
    job_status.update({
        'running': True, 'progreso': 0, 'tipo_actual': '',
        'analizados': 0, 'prospectos': 0, 'importados': 0,
        'log': [], 'done': False, 'error': None
    })
    try:
        log(f'Localizando zona: {ZONA}')
        lat, lng = geocode_zona(ZONA)
        log(f'Coordenadas: {lat:.4f}, {lng:.4f}')
        ya_vistos = set()

        for i, tipo in enumerate(TIPOS):
            job_status['tipo_actual'] = tipo
            job_status['progreso'] = int((i / len(TIPOS)) * 100)
            log(f'Buscando: {tipo}...')
            try:
                negocios = buscar_negocios(lat, lng, tipo)
            except Exception as e:
                log(f'  Error: {e}')
                continue

            nuevos = 0
            for n in negocios:
                pid = n.get('place_id', '')
                if pid in ya_vistos:
                    continue
                ya_vistos.add(pid)
                job_status['analizados'] += 1
                try:
                    det = obtener_detalle(pid)
                    time.sleep(0.08)
                except:
                    det = n

                nombre  = det.get('name') or n.get('name', 'Sin nombre')
                tel     = det.get('formatted_phone_number', '')
                web     = det.get('website', '')
                dir_    = det.get('formatted_address', '')
                rating  = det.get('rating', '')
                reviews = det.get('user_ratings_total', '')

                estado, nota = analizar_web(web)
                if estado == 'web_ok':
                    continue

                job_status['prospectos'] += 1
                nuevos += 1

                notas_crm = (
                    f'Fuente: Google Maps | Categoria: {tipo} | '
                    f'Estado web: {estado} | Analisis: {nota} | '
                    f'Web actual: {web or "ninguna"} | '
                    f'Direccion: {dir_}'
                )
                if rating:
                    notas_crm += f' | Google: {rating} ({reviews} resenas)'

                prospecto = {
                    'id': int(time.time() * 1000) % 999999,
                    'nombre': nombre, 'tel': tel, 'email': '',
                    'tipo': 'web', 'estado': 'lead',
                    'precio': 0, 'cobrado': 0,
                    'fecha': str(date.today()),
                    'notas': notas_crm
                }
                ok = enviar_al_crm(prospecto)
                if ok:
                    job_status['importados'] += 1
                    log(f'  OK: {nombre} ({estado})')
                else:
                    log(f'  Encontrado (sin sync): {nombre}')
                time.sleep(0.3)

            if nuevos > 0:
                log(f'  -> {nuevos} prospectos en {tipo}')

        job_status['progreso'] = 100
        job_status['done'] = True
        job_status['running'] = False
        log(f'Busqueda completada. Prospectos: {job_status["prospectos"]} | Importados: {job_status["importados"]}')

    except Exception as e:
        job_status['error'] = str(e)
        job_status['running'] = False
        job_status['done'] = True
        log(f'Error fatal: {e}')

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'msg': 'Prospector API activa'})

@app.route('/iniciar', methods=['POST'])
def iniciar():
    if job_status['running']:
        return jsonify({'ok': False, 'msg': 'Ya hay una busqueda en curso'})
    if not GOOGLE_API_KEY:
        return jsonify({'ok': False, 'msg': 'Falta GOOGLE_API_KEY'})
    t = threading.Thread(target=run_busqueda, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': 'Busqueda iniciada'})

@app.route('/estado')
def estado():
    return jsonify({
        'running':    job_status['running'],
        'progreso':   job_status['progreso'],
        'tipo_actual': job_status['tipo_actual'],
        'analizados': job_status['analizados'],
        'prospectos': job_status['prospectos'],
        'importados': job_status['importados'],
        'done':       job_status['done'],
        'error':      job_status['error'],
        'log':        job_status['log'][-20:]
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

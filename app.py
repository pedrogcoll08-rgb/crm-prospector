from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import time
import json
import re
from datetime import date
import threading
import os

app = Flask(__name__)
CORS(app)

# ─── CONFIG ───────────────────────────────────────────────
GOOGLE_API_KEY = os.environ.get('GOOGLE_API_KEY', '')
SHEETS_URL     = os.environ.get('SHEETS_URL', 'https://script.google.com/macros/s/AKfycbwangi6yd__KZLF74gapavnHB6pYT-At5h1qzwJE5OGdYhBp_GzjdaLm6dQD0997xLErg/exec')
ZONA           = os.environ.get('ZONA', 'El Raal, Murcia, Spain')
RADIO_METROS   = int(os.environ.get('RADIO_METROS', '10000'))

TIPOS = [
    'restaurant','bar','cafe','beauty_salon','hair_care',
    'gym','doctor','dentist','real_estate_agency','car_repair',
    'car_dealer','clothing_store','florist','bakery','electrician',
    'plumber','lawyer','accounting','supermarket','pharmacy',
    'veterinary_care','lodging','hardware_store','painter','store'
]
# ──────────────────────────────────────────────────────────

# Estado global de la búsqueda (para mostrar progreso)
job_status = {
    'running': False,
    'progreso': 0,
    'total_tipos': len(TIPOS),
    'tipo_actual': '',
    'analizados': 0,
    'prospectos': 0,
    'importados': 0,
    'log': [],
    'done': False,
    'error': None
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
    raise Exception(f'No se encontró la zona: {zona}')


def buscar_negocios(lat, lng, tipo):
    url = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
    negocios = []
    params = {
        'location': f'{lat},{lng}',
        'radius': RADIO_METROS,
        'type': tipo,
        'key': GOOGLE_API_KEY
    }
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
    if not url_web:
        return 'sin_web', 'Sin web registrada en Google'
    try:
        from scrapling import Fetcher
        page = Fetcher.get(url_web, timeout=12, stealthy_headers=True)
        if page.status != 200:
            return 'web_mala', f'Error HTTP {page.status}'
        html = page.html_content.lower() if hasattr(page, 'html_content') else ''
        placeholders = [
            'domain for sale','this domain','buy this domain',
            'coming soon','próximamente','under construction',
            'en construcción','godaddy','wix.com/website/templates'
        ]
        for p in placeholders:
            if p in html:
                return 'web_mala', f'Web placeholder: "{p}"'
        problemas = []
        try:
            texto = page.get_all_text(ignore_tags=['script','style'])
            palabras = len(texto.split())
            if palabras < 250:
                problemas.append(f'Poco contenido ({palabras} palabras)')
        except:
            pass
        if not url_web.startswith('https'):
            problemas.append('Sin HTTPS')
        if 'meta name="description"' not in html and "meta name='description'" not in html:
            problemas.append('Sin SEO básico')
        if not re.search(r'[\+6789]\d[\d\s\-]{7,}', html):
            problemas.append('Sin teléfono visible')
        try:
            imgs = page.css('img')
            if len(imgs) < 3:
                problemas.append('Sin imágenes')
        except:
            pass
        if len(problemas) >= 2:
            return 'web_mala', ' | '.join(problemas)
        return 'web_ok', 'Web aceptable'
    except Exception as e:
        msg = str(e)[:80]
        if 'timeout' in msg.lower() or 'connection' in msg.lower():
            return 'web_mala', 'Web caída o muy lenta'
        return 'web_mala', f'Error: {msg}'


def enviar_al_crm(negocio):
    try:
        import urllib.request, urllib.parse
        fd = urllib.parse.urlencode({'payload': json.dumps({
            'action': 'add_prospecto',
            'data': negocio
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
        log(f'📍 Localizando zona: {ZONA}')
        lat, lng = geocode_zona(ZONA)
        log(f'✓ Coordenadas: {lat:.4f}, {lng:.4f}')

        ya_vistos = set()

        for i, tipo in enumerate(TIPOS):
            job_status['tipo_actual'] = tipo
            job_status['progreso'] = int((i / len(TIPOS)) * 100)
            log(f'🔍 Buscando: {tipo}...')

            try:
                negocios = buscar_negocios(lat, lng, tipo)
            except Exception as e:
                log(f'  ✗ Error: {e}')
                continue

            nuevos_tipo = 0
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

                nombre = det.get('name') or n.get('name', 'Sin nombre')
                tel    = det.get('formatted_phone_number', '')
                web    = det.get('website', '')
                dir_   = det.get('formatted_address', '')
                rating = det.get('rating', '')
                reviews= det.get('user_ratings_total', '')

                estado, nota = analizar_web(web)
                if estado == 'web_ok':
                    continue

                job_status['prospectos'] += 1
                nuevos_tipo += 1

                notas_crm = (
                    f'Fuente: Google Maps | Categoría: {tipo} | '
                    f'Estado web: {estado} | Análisis: {nota} | '
                    f'Web actual: {web or "ninguna"} | '
                    f'Dirección: {dir_}'
                )
                if rating:
                    notas_crm += f' | Google: {rating}★ ({reviews} reseñas)'

                prospecto = {
                    'id': int(time.time() * 1000) % 999999,
                    'nombre': nombre,
                    'tel': tel,
                    'email': '',
                    'tipo': 'web',
                    'estado': 'lead',
                    'precio': 0,
                    'cobrado': 0,
                    'fecha': str(date.today()),
                    'notas': notas_crm
                }

                ok = enviar_al_crm(prospecto)
                if ok:
                    job_status['importados'] += 1
                    log(f'  ✓ {nombre} ({estado})')
                time.sleep(0.3)

            if nuevos_tipo > 0:
                log(f'  → {nuevos_tipo} prospectos en {tipo}')

        job_status['progreso'] = 100
        job_status['done'] = True
        job_status['running'] = False
        log(f'\n✅ Búsqueda completada')
        log(f'📊 Analizados: {job_status["analizados"]} | Prospectos: {job_status["prospectos"]} | Importados: {job_status["importados"]}')

    except Exception as e:
        job_status['error'] = str(e)
        job_status['running'] = False
        job_status['done'] = True
        log(f'✗ Error fatal: {e}')


# ─── RUTAS API ────────────────────────────────────────────

@app.route('/')
def index():
    return jsonify({'status': 'ok', 'msg': 'Prospector API activa'})

@app.route('/iniciar', methods=['POST'])
def iniciar():
    if job_status['running']:
        return jsonify({'ok': False, 'msg': 'Ya hay una búsqueda en curso'})
    if not GOOGLE_API_KEY:
        return jsonify({'ok': False, 'msg': 'Falta GOOGLE_API_KEY en variables de entorno'})
    t = threading.Thread(target=run_busqueda, daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': 'Búsqueda iniciada'})

@app.route('/estado')
def estado():
    return jsonify({
        'running':   job_status['running'],
        'progreso':  job_status['progreso'],
        'tipo_actual': job_status['tipo_actual'],
        'analizados': job_status['analizados'],
        'prospectos': job_status['prospectos'],
        'importados': job_status['importados'],
        'done':      job_status['done'],
        'error':     job_status['error'],
        'log':       job_status['log'][-20:]
    })

@app.route('/log')
def get_log():
    return jsonify({'log': job_status['log']})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

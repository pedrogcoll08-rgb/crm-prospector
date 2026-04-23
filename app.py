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

job_status = {
    'running': False, 'progreso': 0, 'tipo_actual': '',
    'analizados': 0, 'ya_vistos': 0, 'prospectos': 0,
    'importados': 0, 'log': [], 'done': False, 'error': None
}

# ── Memoria de analizados (place_ids ya procesados) ────────
_memoria = set()
_memoria_loaded = False

def log(msg):
    job_status['log'].append(msg)
    if len(job_status['log']) > 150:
        job_status['log'] = job_status['log'][-150:]
    print(msg)

# ── Sheets helpers ─────────────────────────────────────────
def sheets_get(action):
    try:
        r = requests.get(SHEETS_URL + '?action=' + action, timeout=15)
        return r.json()
    except:
        return None

def sheets_post(payload):
    try:
        import urllib.request, urllib.parse
        fd = urllib.parse.urlencode({'payload': json.dumps(payload)}).encode()
        req = urllib.request.Request(SHEETS_URL, data=fd, method='POST')
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except:
        return None

# ── Cargar memoria desde Sheets ────────────────────────────
def cargar_memoria():
    global _memoria, _memoria_loaded
    if _memoria_loaded:
        return
    log('Cargando memoria de analizados desde Sheets...')
    data = sheets_get('get_memoria')
    if data and isinstance(data, list):
        _memoria = set(data)
        log(f'Memoria cargada: {len(_memoria)} negocios ya analizados')
    else:
        _memoria = set()
        log('Memoria vacía — se analizarán todos los negocios')
    _memoria_loaded = True

def guardar_en_memoria(place_ids):
    if not place_ids:
        return
    sheets_post({'action': 'add_memoria', 'place_ids': list(place_ids)})

def reset_memoria():
    global _memoria, _memoria_loaded
    _memoria = set()
    _memoria_loaded = True
    sheets_post({'action': 'reset_memoria'})

def get_estadisticas():
    data = sheets_get('get_estadisticas')
    if data:
        return data
    return {'total_analizados': len(_memoria), 'total_prospectos': 0}

# ── Google Places ──────────────────────────────────────────
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

        html_low = r.text.lower()
        soup = BeautifulSoup(r.text, 'lxml')

        placeholders = ['domain for sale','this domain','buy this domain','coming soon',
                        'próximamente','under construction','en construcción','godaddy','parking']
        for p in placeholders:
            if p in html_low:
                return 'web_mala', f'Web placeholder: "{p}"'

        problemas = []
        texto = soup.get_text(separator=' ', strip=True)
        palabras = len(texto.split())
        if palabras < 200:
            problemas.append(f'Poco contenido ({palabras} palabras)')
        if not url_web.startswith('https'):
            problemas.append('Sin HTTPS')
        meta = soup.find('meta', attrs={'name': re.compile('description', re.I)})
        if not meta or not meta.get('content', '').strip():
            problemas.append('Sin SEO básico')
        if not re.search(r'[\+6789]\d[\d\s\-]{7,}', texto):
            problemas.append('Sin teléfono visible')
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
    result = sheets_post({'action': 'add_prospecto', 'data': negocio})
    return result and result.get('ok')

# ── Búsqueda principal ─────────────────────────────────────
def run_busqueda(tipos_sel=None, max_prospectos=50):
    global job_status, _memoria

    job_status.update({
        'running': True, 'progreso': 0, 'tipo_actual': '',
        'analizados': 0, 'ya_vistos': 0, 'prospectos': 0,
        'importados': 0, 'log': [], 'done': False, 'error': None
    })

    try:
        # Cargar memoria
        cargar_memoria()

        log(f'Localizando zona: {ZONA}')
        lat, lng = geocode_zona(ZONA)
        log(f'Coordenadas: {lat:.4f}, {lng:.4f}')
        if max_prospectos > 0:
            log(f'Objetivo: máximo {max_prospectos} prospectos nuevos')
        else:
            log('Sin límite de prospectos')

        tipos_a_buscar = tipos_sel or []
        if not tipos_a_buscar:
            log('ERROR: No se recibieron tipos')
            job_status['error'] = 'Sin tipos seleccionados'
            job_status['done'] = True
            job_status['running'] = False
            return

        ya_procesados_sesion = set()
        nuevos_en_memoria = set()

        for i, tipo in enumerate(tipos_a_buscar):
            # Parar si llegamos al límite
            if max_prospectos > 0 and job_status['prospectos'] >= max_prospectos:
                log(f'Límite alcanzado: {max_prospectos} prospectos')
                break

            job_status['tipo_actual'] = tipo
            job_status['progreso'] = int((i / len(tipos_a_buscar)) * 100)
            log(f'Buscando: {tipo}...')

            try:
                negocios = buscar_negocios(lat, lng, tipo)
            except Exception as e:
                log(f'  Error buscando {tipo}: {e}')
                continue

            nuevos_tipo = 0
            for n in negocios:
                # Parar si llegamos al límite
                if max_prospectos > 0 and job_status['prospectos'] >= max_prospectos:
                    break

                pid = n.get('place_id', '')
                if not pid:
                    continue

                # Ya procesado en esta sesión
                if pid in ya_procesados_sesion:
                    continue
                ya_procesados_sesion.add(pid)

                # Ya estaba en memoria (analizado en sesiones anteriores)
                if pid in _memoria:
                    job_status['ya_vistos'] += 1
                    continue

                job_status['analizados'] += 1
                nuevos_en_memoria.add(pid)

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
                nuevos_tipo += 1

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
                    log(f'  Encontrado (sin sync Sheets): {nombre}')

                time.sleep(0.3)

            if nuevos_tipo > 0:
                log(f'  -> {nuevos_tipo} prospectos en {tipo}')

            # Guardar lote de place_ids en memoria cada 20 negocios
            if len(nuevos_en_memoria) >= 20:
                _memoria.update(nuevos_en_memoria)
                guardar_en_memoria(nuevos_en_memoria)
                nuevos_en_memoria = set()

        # Guardar resto de memoria
        if nuevos_en_memoria:
            _memoria.update(nuevos_en_memoria)
            guardar_en_memoria(nuevos_en_memoria)

        job_status['progreso'] = 100
        job_status['done'] = True
        job_status['running'] = False
        log(f'Completado. Analizados: {job_status["analizados"]} | Ya vistos: {job_status["ya_vistos"]} | Prospectos: {job_status["prospectos"]} | Importados: {job_status["importados"]}')

    except Exception as e:
        job_status['error'] = str(e)
        job_status['running'] = False
        job_status['done'] = True
        log(f'Error fatal: {e}')

# ── Rutas ──────────────────────────────────────────────────
@app.route('/')
def index():
    return jsonify({'status': 'ok', 'msg': 'Prospector API activa'})

@app.route('/iniciar', methods=['POST'])
def iniciar():
    if job_status['running']:
        return jsonify({'ok': False, 'msg': 'Ya hay una busqueda en curso'})
    if not GOOGLE_API_KEY:
        return jsonify({'ok': False, 'msg': 'Falta GOOGLE_API_KEY'})
    tipos_sel = None
    max_prosp = 50
    try:
        data = request.get_json(silent=True)
        if data:
            tipos_sel = data.get('tipos')
            max_prosp = int(data.get('max_prospectos', 50))
    except:
        pass
    t = threading.Thread(target=run_busqueda, args=(tipos_sel, max_prosp), daemon=True)
    t.start()
    return jsonify({'ok': True, 'msg': 'Busqueda iniciada'})

@app.route('/estado')
def estado():
    return jsonify({
        'running':    job_status['running'],
        'progreso':   job_status['progreso'],
        'tipo_actual': job_status['tipo_actual'],
        'analizados': job_status['analizados'],
        'ya_vistos':  job_status['ya_vistos'],
        'prospectos': job_status['prospectos'],
        'importados': job_status['importados'],
        'done':       job_status['done'],
        'error':      job_status['error'],
        'log':        job_status['log'][-25:]
    })

@app.route('/estadisticas')
def estadisticas():
    return jsonify(get_estadisticas())

@app.route('/reset_memoria', methods=['POST'])
def reset_memoria_route():
    global _memoria_loaded
    reset_memoria()
    _memoria_loaded = True
    return jsonify({'ok': True, 'msg': 'Memoria reseteada'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

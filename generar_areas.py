#!/usr/bin/env python3
"""
Regenera incendios_areas_2026.geojson desde EFFIS (Copernicus).

Descarga el SHAPEZIP de la capa de superficie quemada, filtra España y el
año en curso, simplifica la geometría (MODIS es de ~250-500 m nativo, así
que el exceso de vértices no aporta) y redondea coordenadas. Resultado:
~0,8 MB frente a los ~18 MB del volcado crudo.

Pensado para ejecutarse a diario (cron). Requiere: pyshp, shapely.
    pip install pyshp shapely

Uso:
    python3 generar_areas.py                 # descarga de EFFIS
    python3 generar_areas.py modis_ba_poly   # procesa un shapefile local
"""

import io
import os
import sys
import json
import time
import zipfile
import tempfile
import datetime
import urllib.request
import urllib.error

import shapefile
from shapely.geometry import shape, mapping

# ── Config ────────────────────────────────────────────────────────────
PAIS        = 'ES'
ANIO        = str(datetime.date.today().year)     # año en curso, automático
TOLERANCIA  = 0.0005                              # ~50 m de simplificación
DECIMALES   = 5
SALIDA      = 'incendios_areas_2026.geojson'      # ajusta el año si quieres

# EFFIS da 502/500 con cierta frecuencia aunque el servicio esté bien;
# reintentamos con espera creciente antes de darnos por vencidos.
REINTENTOS       = 5
ESPERA_BASE_SEG  = 20   # 20s, 40s, 60s, 80s, 100s

# WFS de EFFIS. Se pide solo España vía CQL para no bajar el volcado global;
# si el servicio ignorase el filtro, el filtrado local de abajo lo corrige.
EFFIS_URL = (
    'https://maps.effis.emergency.copernicus.eu/effis'
    '?service=WFS&request=getfeature&typename=ms:modis.ba.poly'
    "&version=1.1.0&outputformat=SHAPEZIP&CQL_FILTER=COUNTRY='" + PAIS + "'"
)


def _ruta_shp(base_o_ruta):
    """Devuelve una ruta terminada en '.shp' explícito. pyshp usa
    internamente os.path.splitext() sobre la ruta que le pasamos para
    saber si ya incluye extensión; si el nombre base tiene más de un
    punto (p.ej. 'modis.ba.poly'), splitext corta por el ÚLTIMO punto y
    pyshp cree que '.poly' es la extensión, buscando 'modis.ba.shp' en
    vez de 'modis.ba.poly.shp'. Pasarle siempre la ruta con '.shp' ya
    puesto evita ese despiste."""
    p = base_o_ruta
    for ext in ('.shp', '.shx', '.dbf'):
        if p.lower().endswith(ext):
            p = p[:-len(ext)]
            break
    return p + '.shp'


def _limpiar_dir(destino_dir):
    for nombre in os.listdir(destino_dir):
        os.remove(os.path.join(destino_dir, nombre))


def _shapefile_poly_valido(destino_dir):
    """Busca dentro de destino_dir un .shp con 'poly' en el nombre que
    tenga su .dbf/.shx emparejados y se pueda abrir con registros.
    Devuelve la ruta base o None si no hay ninguno válido."""
    for nombre in os.listdir(destino_dir):
        if not nombre.lower().endswith('.shp'):
            continue
        if 'poly' not in nombre.lower():
            continue
        base = os.path.join(destino_dir, nombre[:-4])
        if not (os.path.exists(base + '.dbf') and os.path.exists(base + '.shx')):
            continue
        try:
            r_test = shapefile.Reader(_ruta_shp(base), encoding='latin-1')
            n = len(r_test)
            r_test.close()
        except Exception as e:
            print(f'    (descartado {nombre}: {type(e).__name__}: {e})')
            continue
        if n > 0:
            return base
    return None


def descargar_a_shapefile(destino_dir):
    """Descarga el SHAPEZIP, lo descomprime y valida su contenido.
    Reintenta la descarga completa (no solo ante errores HTTP, también
    si el ZIP llega corrupto o con una capa que no es la de polígonos,
    algo que EFFIS produce de forma intermitente sin dar error HTTP)."""
    for intento in range(1, REINTENTOS + 1):
        print(f'Descargando EFFIS SHAPEZIP… (intento {intento}/{REINTENTOS})')
        req = urllib.request.Request(EFFIS_URL, headers={'User-Agent': 'EpData/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data = r.read()

            _limpiar_dir(destino_dir)
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                z.extractall(destino_dir)

            base = _shapefile_poly_valido(destino_dir)
            if base is None:
                raise RuntimeError(
                    'El ZIP no contiene una capa "poly" válida y legible. '
                    f'Contenido: {os.listdir(destino_dir)}'
                )
            return base

        except Exception as e:
            print(f'  Fallo: {e}')
            if intento == REINTENTOS:
                raise
            espera = ESPERA_BASE_SEG * intento
            print(f'  Reintentando en {espera}s…')
            time.sleep(espera)



def procesar(base):
    """Lee el shapefile, filtra ES + año en curso, simplifica y escribe."""
    r = shapefile.Reader(_ruta_shp(base), encoding='latin-1')
    feats, area = [], 0.0

    def rnd(o):
        return [rnd(x) for x in o] if isinstance(o, (list, tuple)) else round(o, DECIMALES)

    try:
        for sr in r.iterShapeRecords():
            rec = sr.record
            if rec['COUNTRY'] != PAIS:
                continue
            if not (rec['FIREDATE'] or '').startswith(ANIO):
                continue
            g = shape(sr.shape.__geo_interface__).simplify(TOLERANCIA, preserve_topology=True)
            if g.is_empty:
                continue
            gj = mapping(g)
            gj['coordinates'] = rnd(gj['coordinates'])
            try:
                ha = round(float(rec['AREA_HA'])); area += ha
            except (TypeError, ValueError):
                ha = None
            feats.append({
                'type': 'Feature',
                'properties': {'fecha': rec['FIREDATE'][:10],
                               'provincia': rec['PROVINCE'], 'ha': ha},
                'geometry': gj,
            })
    finally:
        r.close()  # libera el .dbf/.shp/.shx antes de que se borre el temp dir

    out = {'type': 'FeatureCollection', 'features': feats}
    with open(SALIDA, 'w', encoding='utf-8') as f:
        json.dump(out, f, separators=(',', ':'), ensure_ascii=False)

    mb = os.path.getsize(SALIDA) / 1e6
    print(f'{SALIDA}: {len(feats)} perímetros, {round(area)} ha, {mb:.2f} MB')


def main():
    if len(sys.argv) > 1:                       # shapefile local
        procesar(sys.argv[1])
    else:                                        # descarga de EFFIS
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            procesar(descargar_a_shapefile(tmp))


if __name__ == '__main__':
    main()
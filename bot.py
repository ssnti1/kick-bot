import os
import time
import requests
import signal
import random

# ========= config desde env =========
SLUGS = [s.strip() for s in os.getenv("SLUGS", "nombre_del_canal").split(",") if s.strip()]
POLL_SEC = int(os.getenv("POLL_SEC", "25"))              # intervalo objetivo (seg)
X_ACCESS_TOKEN = os.environ["X_ACCESS_TOKEN"]            # token de usuario con permiso de escribir
TWEET_PREFIX = os.getenv("TWEET_PREFIX", "🔴 stream apagado")
POST_ON_START = os.getenv("POST_ON_START", "0") == "1"   # 1 = también tuitear cuando se enciende
INIT_ON_AS_START = os.getenv("INIT_ON_AS_START", "0") == "1"  # 1 = si ya está ON al arrancar, tuitea inicio

KICK_LIVE_URL = "https://kick.com/api/v2/channels/{slug}/livestream"
TW_POST_URL   = "https://api.twitter.com/2/tweets"

UA = {"User-Agent": "kick-offline-x-bot/1.1"}

# ========= estado por canal =========
def new_state():
    return {
        "was_live": False,
        "start_ts": None,
        "peak": 0,
        "sum_viewers": 0,        # para promedio
        "samples": 0,            # para promedio
        "viewer_seconds": 0.0,   # para horas vistas
        "last_sample_ts": None,  # tiempo de la última muestra (para integrar bien aunque el loop varíe)
    }

state = {slug: new_state() for slug in SLUGS}
running = True

# ========= utilidades =========
def secs_to_hm(secs: int) -> str:
    m, _ = divmod(max(0, secs), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"

def post_tweet(text: str):
    if os.getenv("DEBUG") == "1":
        print("DEBUG TWEET:", text)
        return
    headers = {
        "Authorization": f"Bearer {X_ACCESS_TOKEN}",
        "Content-Type": "application/json", 
        **UA
    }
    resp = requests.post(TW_POST_URL, headers=headers, json={"text": text}, timeout=20)
    resp.raise_for_status()


def fetch_live(slug: str):
    """
    Devuelve:
      - dict con datos del stream cuando está en vivo
      - None si está offline o error
    """
    try:
        r = requests.get(KICK_LIVE_URL.format(slug=slug), headers=UA, timeout=10)
        if r.status_code == 404:
            return None
        if not r.ok:
            return None
        data = r.json()
        if not data:
            return None
        return data
    except Exception:
        return None

def extract_viewers(payload) -> int:
    """
    Intenta encontrar un campo de viewers en varias formas posibles.
    Ajustado de forma tolerante porque Kick puede cambiar el shape.
    """
    if not isinstance(payload, dict):
        return 0

    # casos directos
    for k in ("viewer_count", "viewers", "current_viewers", "peak_viewers"):
        v = payload.get(k)
        if isinstance(v, (int, float)):
            return int(v)

    # a veces viene anidado: { "livestream": { "viewer_count": ... } }
    live = payload.get("livestream")
    if isinstance(live, dict):
        for k in ("viewer_count", "viewers", "current_viewers", "peak_viewers"):
            v = live.get(k)
            if isinstance(v, (int, float)):
                return int(v)

    return 0

def handle_sigterm(_sig, _frm):
    global running
    running = False

signal.signal(signal.SIGTERM, handle_sigterm)
signal.signal(signal.SIGINT, handle_sigterm)

print(f"monitoring {len(SLUGS)} channel(s): {', '.join(SLUGS)}")

# ========= bootstrap: marcar ON si ya estaban en vivo al arrancar =========
def bootstrap_live_states():
    now = time.time()
    for slug in SLUGS:
        data = fetch_live(slug)
        if data:  # ya está en vivo
            viewers = extract_viewers(data)
            s = state[slug]
            s.update({
                "was_live": True,
                "start_ts": now,
                "last_sample_ts": now,
                "peak": viewers,
                "sum_viewers": viewers,
                "samples": 1,
                "viewer_seconds": 0.0
            })
            print(f"[{slug}] BOOTSTRAP: detected LIVE on startup (viewers={viewers})")
            if POST_ON_START and INIT_ON_AS_START:
                try:
                    post_tweet(f"🟢 {slug} está en vivo ahora mismo en kick.com/{slug}")
                except Exception as e:
                    print(f"[{slug}] post start error (bootstrap):", e)

# Llama al bootstrap ANTES del loop principal
bootstrap_live_states()

# ========= bucle principal =========
while running:
    loop_start = time.time()

    for slug in SLUGS:
        s = state[slug]
        data = fetch_live(slug)
        is_live = bool(data)
        now = time.time()

        if is_live:
            viewers = extract_viewers(data)

            # transición OFF -> ON
            if not s["was_live"]:
                s.update(new_state())  # limpia contadores
                s["was_live"] = True
                s["start_ts"] = now
                s["last_sample_ts"] = now
                s["peak"] = viewers
                s["sum_viewers"] = viewers
                s["samples"] = 1
                s["viewer_seconds"] = 0.0
                print(f"[{slug}] LIVE (viewers={viewers})")
                if POST_ON_START:
                    try:
                        post_tweet(f"🟢 {slug} está en vivo ahora mismo en kick.com/{slug}")
                    except Exception as e:
                        print(f"[{slug}] post start error:", e)
            else:
                # ON -> ON (acumular métricas)
                dt = max(0.0, now - (s["last_sample_ts"] or now))
                s["last_sample_ts"] = now

                s["peak"] = max(s["peak"], viewers)
                s["sum_viewers"] += viewers
                s["samples"] += 1
                s["viewer_seconds"] += viewers * dt

        else:
            # transición ON -> OFF
            if s["was_live"]:
                dur = int(now - (s["start_ts"] or now))
                avg = int(s["sum_viewers"] / s["samples"]) if s["samples"] else 0
                hours_watched = round(s["viewer_seconds"] / 3600.0, 2)

                msg = (
                    f"📊 Stats del stream de {slug}\n"
                    f"⏱️ duración: {secs_to_hm(dur)}\n"
                    f"👥 peak: {s['peak']} | promedio: {avg}\n"
                    f"⏳ horas vistas: {hours_watched}"
                )
                try:
                    post_tweet(msg)
                    print(f"[{slug}] TWEETED: {msg.replace(os.linesep, ' | ')}")
                except Exception as e:
                    print(f"[{slug}] tweet error:", e)

                # reset
                state[slug] = new_state()

        # pequeña aleatoriedad para no “martillar” exacto el mismo timing
        time.sleep(0.1 + random.random() * 0.2)

    # intenta mantener ritmo aproximado de POLL_SEC entre rondas
    elapsed = time.time() - loop_start
    delay = max(0.0, POLL_SEC - elapsed)
    time.sleep(delay)

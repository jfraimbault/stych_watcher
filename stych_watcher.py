#!/usr/bin/env python3
"""
Surveillance des créneaux de conduite disponibles sur Stych.

Configuration attendue en variables d'environnement :
    STYCH_EMAIL         : ton email de connexion Stych
    STYCH_PASSWORD      : ton mot de passe Stych
    STYCH_DAYS_AHEAD    : (optionnel) nombre de jours à surveiller, défaut 10

Usage :
    STYCH_EMAIL="toi@mail.com" STYCH_PASSWORD="motdepasse" python3 stych_watcher.py
"""

import os
import re
import json
from datetime import datetime, timedelta
from shutil import which

import requests

# ---- Configuration ----
BASE_URL = "https://www.stych.fr"
PLANNING_PAGE = f"{BASE_URL}/elearning/planning"
RESERVATION_PAGE = f"{BASE_URL}/elearning/formation/conduite/reservation"
PROPOSITION_ENDPOINT = f"{BASE_URL}/elearning/planning-conduite/get-planning-proposition"
CHECK_AUTH_ENDPOINT = f"{BASE_URL}/check-auth"
LOGIN_ENDPOINT = f"{BASE_URL}/connexion/0/record3"

EMAIL = os.environ.get("STYCH_EMAIL")
PASSWORD = os.environ.get("STYCH_PASSWORD")
DAYS_AHEAD = int(os.environ.get("STYCH_DAYS_AHEAD", "10"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# Villes autorisées (le nom doit correspondre au champ "ville" renvoyé par Stych, en majuscules)
ALLOWED_CITIES = {"ORVAULT", "ST HERBLAIN"}

# Fichier qui garde en mémoire les créneaux déjà vus (à côté du script)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stych_seen_slots.json")

HEADERS_PAGE = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}

HEADERS_AJAX = {
    **HEADERS_PAGE,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/connexion",
    "Origin": BASE_URL,
}


def login(session: requests.Session) -> None:
    """Reproduit le flow de connexion en 2 étapes observé dans le navigateur."""
    if not EMAIL or not PASSWORD:
        raise RuntimeError(
            "STYCH_EMAIL et STYCH_PASSWORD doivent être définis en variables d'environnement."
        )

    # Étape 1 : check-auth (envoie l'email)
    resp_check = session.post(CHECK_AUTH_ENDPOINT, data={"email": EMAIL}, headers=HEADERS_AJAX)

    # Étape 2 : login réel (email + mot de passe)
    payload = {
        "email": EMAIL,
        "mdp": PASSWORD,
        "mdp_forgotten": "0",
        "remember_me": "0",
        "submit": "Connexion",
    }
    resp = session.post(LOGIN_ENDPOINT, data=payload, headers=HEADERS_AJAX)
    resp.raise_for_status()
    data = resp.json()
    if data.get("statut") != "OK":
        raise RuntimeError(f"Échec de connexion Stych : {data}")


def get_csrf_token(session: requests.Session) -> str:
    """Le token_csrf est injecté dans le HTML de la page réservation, on l'extrait par regex."""
    resp = session.get(RESERVATION_PAGE, headers=HEADERS_PAGE)
    resp.raise_for_status()
    match = re.search(r"token_csrf['\"]?\s*:\s*['\"]([a-f0-9]+)['\"]", resp.text)
    if not match:
        raise RuntimeError("Impossible de trouver le token_csrf sur la page réservation (structure du site a peut-être changé).")
    return match.group(1)


def get_available_slots(session: requests.Session, csrf_token: str) -> tuple:
    payload = {
        "calledFromFilter": "0",
        "token_csrf": csrf_token,
    }
    resp = session.post(PROPOSITION_ENDPOINT, data=payload, headers=HEADERS_AJAX)
    resp.raise_for_status()
    data = resp.json()
    if data.get("statut") != "OK":
        raise RuntimeError(f"Erreur en récupérant le planning : {data}")
    return data.get("rowsProposition", []), data.get("rowsPointDeCours", [])


def build_city_map(points_de_cours: list) -> dict:
    """Associe chaque id_lac (lieu de cours) à sa ville, pour pouvoir filtrer les créneaux."""
    return {p["id_liste_adresse_cours"]: p.get("ville", "").upper() for p in points_de_cours}


def filter_by_city(slots: list, city_map: dict, allowed_cities: set) -> list:
    filtered = []
    for slot in slots:
        ville = city_map.get(slot.get("id_lac"), "")
        if ville in allowed_cities:
            filtered.append(slot)
    return filtered


def filter_by_days_ahead(slots: list, days_ahead: int) -> list:
    today = datetime.now().date()
    limit = today + timedelta(days=days_ahead)
    filtered = []
    for slot in slots:
        try:
            slot_date = datetime.strptime(slot["info_date"], "%Y-%m-%d").date()
        except (ValueError, KeyError, TypeError):
            continue
        if today <= slot_date <= limit:
            filtered.append(slot)
    return filtered


def slot_key(slot: dict) -> str:
    """Identifiant unique d'un créneau, pour détecter les nouveautés d'un run à l'autre."""
    return f"{slot.get('info_date')}_{slot.get('heure_debut')}_{slot.get('heure_fin')}_{slot.get('id_lac')}_{slot.get('id_user')}"


def load_seen_slots() -> set:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_slots(keys: set) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(list(keys), f)


def send_ntfy(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        print("[ntfy] NTFY_TOPIC non configuré, notification non envoyée.")
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title.encode("utf-8")},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[ntfy] Échec de l'envoi : {e}")


def notify(new_slots: list) -> None:
    lines = [
        f"{s.get('info_date')} {s.get('heure_debut_fr')}-{s.get('heure_fin_fr')} avec {s.get('moniteur')}"
        for s in new_slots
    ]
    message = "\n".join(lines)
    title = f"{len(new_slots)} nouveau(x) créneau(x) Stych !"

    send_ntfy(title, message)

    if which("termux-notification"):
        # Notification native Android via Termux:API (si le script tourne aussi sur Termux)
        safe_message = message.replace('"', "'")[:500]
        os.system(f'termux-notification --title "{title}" --content "{safe_message}"')
    else:
        # Fallback console, utile pour tester sur Mac ou dans les logs GitHub Actions
        print(f"\n🔔 {title}\n{message}\n")


def main():
    session = requests.Session()
    login(session)
    csrf_token = get_csrf_token(session)
    all_slots, points_de_cours = get_available_slots(session, csrf_token)
    city_map = build_city_map(points_de_cours)

    city_slots = filter_by_city(all_slots, city_map, ALLOWED_CITIES)
    upcoming_slots = filter_by_days_ahead(city_slots, DAYS_AHEAD)

    seen_keys = load_seen_slots()
    current_keys = {slot_key(s) for s in upcoming_slots}
    new_keys = current_keys - seen_keys
    new_slots = [s for s in upcoming_slots if slot_key(s) in new_keys]

    if new_slots:
        notify(new_slots)
    else:
        print(f"Aucun nouveau créneau dans les {DAYS_AHEAD} prochains jours.")

    save_seen_slots(current_keys)


if __name__ == "__main__":
    main()

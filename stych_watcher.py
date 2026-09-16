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
#NTFY_TOPIC = os.environ.get("NTFY_TOPIC")

# Heure minimale de début de créneau, format "HH:MM" (ex: "15:30"). Vide par défaut = pas de restriction.
MIN_HOUR = os.environ.get("STYCH_MIN_HOUR", "").strip()

# Villes autorisées, séparées par des virgules (le nom doit correspondre au champ "ville" renvoyé par Stych)
# Valeur vide ou "*" -> pas de filtre, toutes les villes sont prises
_raw_cities = os.environ.get("STYCH_ALLOWED_CITIES", "*").strip()
if _raw_cities in ("", "*"):
    ALLOWED_CITIES = None
else:
    ALLOWED_CITIES = {v.strip().upper() for v in _raw_cities.split(",") if v.strip()}

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


def filter_by_city(slots: list, city_map: dict, allowed_cities) -> list:
    if not allowed_cities:  # None ou set vide -> pas de filtre, toutes les villes
        return slots
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


VACANCES_API = "https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets/fr-en-calendrier-scolaire/records"


def get_annee_scolaire_courante() -> str:
    """Format 'YYYY-YYYY+1'. La rentrée étant fin août/début septembre, on bascule
    sur l'année scolaire suivante à partir du mois d'août."""
    today = datetime.now()
    if today.month >= 8:
        return f"{today.year}-{today.year + 1}"
    return f"{today.year - 1}-{today.year}"


def get_vacances_zone_b() -> list:
    """Récupère les plages de vacances scolaires pour l'académie de Nantes (zone B),
    année scolaire en cours, via l'API publique data.education.gouv.fr."""
    annee_scolaire = get_annee_scolaire_courante()
    where_clause = f'annee_scolaire="{annee_scolaire}" and zones="Zone B" and location="Nantes"'
    params = {"where": where_clause}

    try:
        resp = requests.get(VACANCES_API, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError):
        return []  # en cas d'échec, on continue sans cette info plutôt que de faire planter le script

    plages = []
    for r in results:
        start = r.get("start_date")
        end = r.get("end_date")
        if start and end:
            try:
                plages.append((
                    datetime.fromisoformat(start[:10]).date(),
                    datetime.fromisoformat(end[:10]).date(),
                ))
            except ValueError:
                continue
    return plages


def is_in_vacances(date_obj, plages: list) -> bool:
    return any(start <= date_obj <= end for start, end in plages)


def is_eligible_for_booking(slot: dict, vacances_plages: list) -> bool:
    """Règle : à partir de MIN_HOUR (si défini), sauf pendant les vacances scolaires
    zone B (journée entière). Si MIN_HOUR est vide, aucune restriction horaire."""
    if not MIN_HOUR:
        return True

    try:
        slot_date = datetime.strptime(slot["info_date"], "%Y-%m-%d").date()
    except (ValueError, KeyError, TypeError):
        return False

    if is_in_vacances(slot_date, vacances_plages):
        return True
    return (slot.get("heure_debut") or "") >= f"{MIN_HOUR}:00"


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


JOURS_FR = {
    0: "Lundi",
    1: "Mardi",
    2: "Mercredi",
    3: "Jeudi",
    4: "Vendredi",
    5: "Samedi",
    6: "Dimanche",
}


def format_heure(heure_fr: str) -> str:
    """Retire les minutes ':00' inutiles, ex: '16h00' -> '16h', '12h45' inchangé."""
    if heure_fr and heure_fr.endswith("h00"):
        return heure_fr[:-2]
    return heure_fr


def group_slots_by_day(slots: list) -> list:
    """Regroupe les créneaux par (date, moniteur), triés par heure, une ligne par groupe."""
    groups = {}
    for slot in slots:
        try:
            slot_date = datetime.strptime(slot["info_date"], "%Y-%m-%d")
        except (ValueError, KeyError, TypeError):
            continue
        moniteur = slot.get("moniteur", "?")
        key = (slot_date, moniteur)
        groups.setdefault(key, []).append(slot)

    lines = []
    for (slot_date, moniteur), day_slots in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        day_slots.sort(key=lambda s: s.get("heure_debut", ""))
        creneaux = ", ".join(
            f"{format_heure(s.get('heure_debut_fr'))}-{format_heure(s.get('heure_fin_fr'))}"
            for s in day_slots
        )
        jour = JOURS_FR[slot_date.weekday()]
        date_str = slot_date.strftime("%d/%m")
        lines.append(f"{jour} {date_str}  {creneaux} avec {moniteur}")
    return lines


def notify(new_slots: list) -> None:
    lines = group_slots_by_day(new_slots)
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

    vacances_plages = get_vacances_zone_b()
    eligible_slots = [s for s in upcoming_slots if is_eligible_for_booking(s, vacances_plages)]

    seen_keys = load_seen_slots()
    current_keys = {slot_key(s) for s in eligible_slots}
    new_keys = current_keys - seen_keys
    new_slots = [s for s in eligible_slots if slot_key(s) in new_keys]

    if new_slots:
        notify(new_slots)
    else:
        print(f"Aucun nouveau créneau dans les {DAYS_AHEAD} prochains jours.")

    save_seen_slots(current_keys)


if __name__ == "__main__":
    main()

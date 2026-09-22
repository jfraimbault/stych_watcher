#!/usr/bin/env python3
"""
Génère un fichier .ics des créneaux de conduite DISPONIBLES (non réservés) sur Stych,
avec les mêmes critères de filtrage que stych_watcher.py (villes, heure minimale,
exception vacances scolaires), sur une fenêtre de jours donnée.

Configuration en variables d'environnement (mêmes noms que stych_watcher.py) :
    STYCH_EMAIL, STYCH_PASSWORD
    STYCH_DAYS_AHEAD       (défaut: 90)
    STYCH_ALLOWED_CITIES   (défaut: * = toutes les villes)
    STYCH_MIN_HOUR         (défaut: vide = pas de restriction)

Usage :
    STYCH_EMAIL="..." STYCH_PASSWORD="..." python3 stych_available_ics.py
"""

import os
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

BASE_URL = "https://www.stych.fr"
RESERVATION_PAGE = f"{BASE_URL}/elearning/formation/conduite/reservation"
PROPOSITION_ENDPOINT = f"{BASE_URL}/elearning/planning-conduite/get-planning-proposition"
CHECK_AUTH_ENDPOINT = f"{BASE_URL}/check-auth"
LOGIN_ENDPOINT = f"{BASE_URL}/connexion/0/record3"

EMAIL = os.environ.get("STYCH_EMAIL")
PASSWORD = os.environ.get("STYCH_PASSWORD")
DAYS_AHEAD = int(os.environ.get("STYCH_DAYS_AHEAD", "90"))
MIN_HOUR = os.environ.get("STYCH_MIN_HOUR", "").strip()

_raw_cities = os.environ.get("STYCH_ALLOWED_CITIES", "*").strip()
if _raw_cities in ("", "*"):
    ALLOWED_CITIES = None
else:
    ALLOWED_CITIES = {v.strip().upper() for v in _raw_cities.split(",") if v.strip()}

OUTPUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stych_available.json")
OUTPUT_ICS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stych_available.ics")

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
    if not EMAIL or not PASSWORD:
        raise RuntimeError("STYCH_EMAIL et STYCH_PASSWORD doivent être définis en variables d'environnement.")

    session.post(CHECK_AUTH_ENDPOINT, data={"email": EMAIL}, headers=HEADERS_AJAX)

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
    resp = session.get(RESERVATION_PAGE, headers=HEADERS_PAGE)
    resp.raise_for_status()
    match = re.search(r"token_csrf['\"]?\s*:\s*['\"]([a-f0-9]+)['\"]", resp.text)
    if not match:
        raise RuntimeError("Impossible de trouver le token_csrf sur la page réservation.")
    return match.group(1)


def get_available_slots(session: requests.Session, csrf_token: str) -> tuple:
    payload = {"calledFromFilter": "0", "token_csrf": csrf_token}
    resp = session.post(PROPOSITION_ENDPOINT, data=payload, headers=HEADERS_AJAX)
    resp.raise_for_status()
    data = resp.json()
    if data.get("statut") != "OK":
        raise RuntimeError(f"Erreur en récupérant le planning : {data}")
    return data.get("rowsProposition", []), data.get("rowsPointDeCours", [])


def build_lieu_map(points_de_cours: list) -> dict:
    return {
        p["id_liste_adresse_cours"]: {
            "ville": p.get("ville", "").upper(),
            "intitule": p.get("intitule", ""),
            "adresse": p.get("adresse", ""),
        }
        for p in points_de_cours
    }


def filter_by_city(slots: list, lieu_map: dict, allowed_cities) -> list:
    if not allowed_cities:
        return slots
    return [s for s in slots if lieu_map.get(s.get("id_lac"), {}).get("ville", "") in allowed_cities]


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
    today = datetime.now()
    if today.month >= 8:
        return f"{today.year}-{today.year + 1}"
    return f"{today.year - 1}-{today.year}"


def get_vacances_zone_b() -> list:
    annee_scolaire = get_annee_scolaire_courante()
    where_clause = f'annee_scolaire="{annee_scolaire}" and zones="Zone B" and location="Nantes"'
    try:
        resp = requests.get(VACANCES_API, params={"where": where_clause}, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError):
        return []

    plages = []
    for r in results:
        start, end = r.get("start_date"), r.get("end_date")
        if start and end:
            try:
                plages.append((datetime.fromisoformat(start[:10]).date(), datetime.fromisoformat(end[:10]).date()))
            except ValueError:
                continue
    return plages


def is_in_vacances(date_obj, plages: list) -> bool:
    return any(start <= date_obj <= end for start, end in plages)


def is_eligible_for_booking(slot: dict, vacances_plages: list) -> bool:
    if not MIN_HOUR:
        return True
    try:
        slot_date = datetime.strptime(slot["info_date"], "%Y-%m-%d").date()
    except (ValueError, KeyError, TypeError):
        return False
    if slot_date.weekday() == 5:  # samedi
        return True
    if is_in_vacances(slot_date, vacances_plages):
        return True
    return (slot.get("heure_debut") or "") >= f"{MIN_HOUR}:00"


def write_json(slots: list, lieu_map: dict) -> None:
    enriched = []
    for s in slots:
        lieu = lieu_map.get(s.get("id_lac"), {})
        enriched.append({
            "date": s.get("info_date"),
            "heure_debut": s.get("heure_debut"),
            "heure_fin": s.get("heure_fin"),
            "moniteur": s.get("moniteur"),
            "lieu_nom": lieu.get("intitule", ""),
            "adresse": lieu.get("adresse", ""),
            "ville": lieu.get("ville", ""),
        })
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2)
    return enriched


def write_ics(enriched: list) -> None:
    tz = ZoneInfo("Europe/Paris")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Stych Watcher - Dispo//FR"]
    
    for s in enriched:
        try:
            dt_start_naive = datetime.strptime(f"{s['date']} {s['heure_debut']}", "%Y-%m-%d %H:%M:%S")
            dt_end_naive = datetime.strptime(f"{s['date']} {s['heure_fin']}", "%Y-%m-%d %H:%M:%S")
        except (ValueError, KeyError):
            continue

        # Application du fuseau Europe/Paris
        dt_start = dt_start_naive.replace(tzinfo=tz)
        dt_end = dt_end_naive.replace(tzinfo=tz)

        uid = f"{s['date']}-{s['heure_debut']}-{s['heure_fin']}-{s['ville']}-{s['moniteur']}".replace(" ", "_")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@stych-available",
            f"DTSTART;TZID=Europe/Paris:{dt_start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID=Europe/Paris:{dt_end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:[DISPO Stych] {s['moniteur']}",
            f"LOCATION:{s['lieu_nom']}, {s['adresse']}, {s['ville']}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    with open(OUTPUT_ICS, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    session = requests.Session()
    login(session)
    csrf_token = get_csrf_token(session)
    all_slots, points_de_cours = get_available_slots(session, csrf_token)
    lieu_map = build_lieu_map(points_de_cours)

    city_slots = filter_by_city(all_slots, lieu_map, ALLOWED_CITIES)
    upcoming_slots = filter_by_days_ahead(city_slots, DAYS_AHEAD)

    vacances_plages = get_vacances_zone_b()
    eligible_slots = [s for s in upcoming_slots if is_eligible_for_booking(s, vacances_plages)]

    print(f"{len(eligible_slots)} créneau(x) disponible(s) éligible(s) trouvé(s).")
    enriched = write_json(eligible_slots, lieu_map)
    write_ics(enriched)
    print(f"Fichiers générés : {OUTPUT_JSON} et {OUTPUT_ICS}")


if __name__ == "__main__":
    main()

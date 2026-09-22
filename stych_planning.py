#!/usr/bin/env python3
"""
Récupère le planning des cours de conduite déjà réservés sur Stych,
avec date, heure, lieu/ville et moniteur, et génère :
  - stych_planning.json (données structurées)
  - stych_planning.ics  (importable dans Google Calendar)

Configuration attendue en variables d'environnement :
    STYCH_EMAIL     : email de connexion Stych
    STYCH_PASSWORD  : mot de passe Stych

Usage :
    STYCH_EMAIL="toi@mail.com" STYCH_PASSWORD="motdepasse" python3 stych_planning.py
"""

import os
import re
import json
from datetime import datetime, timedelta

import requests

BASE_URL = "https://www.stych.fr"
PLANNING_PAGE = f"{BASE_URL}/elearning/planning"
CHECK_AUTH_ENDPOINT = f"{BASE_URL}/check-auth"
LOGIN_ENDPOINT = f"{BASE_URL}/connexion/0/record3"

EMAIL = os.environ.get("STYCH_EMAIL")
PASSWORD = os.environ.get("STYCH_PASSWORD")

OUTPUT_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stych_planning.json")
OUTPUT_ICS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stych_planning.ics")

HEADERS_PAGE = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}

HEADERS_AJAX = {
    **HEADERS_PAGE,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{BASE_URL}/connexion",
    "Origin": BASE_URL,
}

MOIS_FR = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
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


def get_planning_html(session: requests.Session, year_month_url: str = None) -> str:
    url = PLANNING_PAGE if not year_month_url else f"{PLANNING_PAGE}/{year_month_url}"
    resp = session.get(url, headers=HEADERS_PAGE)
    resp.raise_for_status()
    return resp.text


def get_all_lessons(session: requests.Session, nb_mois: int = 7) -> list:
    """Récupère et fusionne les cours sur `nb_mois` mois (mois en cours inclus)."""
    all_lessons = {}
    today = datetime.now().date()
    for i in range(nb_mois):
        # Approximation simple pour avancer d'i mois (évite une dépendance externe type dateutil)
        year = today.year + (today.month - 1 + i) // 12
        month = (today.month - 1 + i) % 12 + 1
        target = f"{year:04d}-{month:02d}-01"
        html = get_planning_html(session, target if i > 0 else None)
        for lesson in parse_lessons(html):
            all_lessons[lesson["id"]] = lesson  # dédoublonne par id
    return sorted(all_lessons.values(), key=lambda l: (l["date"], l["heure_debut"]))


def parse_date_fr(date_fr: str):
    """'Jeudi 10 Septembre 2026' -> (2026, 9, 10)"""
    match = re.match(r"\w+\s+(\d{1,2})\s+(\w+)\s+(\d{4})", date_fr.strip())
    if not match:
        return None
    jour, mois_nom, annee = match.groups()
    mois = MOIS_FR.get(mois_nom.lower())
    if not mois:
        return None
    return int(annee), mois, int(jour)


def parse_heures(heures_str: str):
    """'17h15 - 18h00' -> ('17:15', '18:00')"""
    match = re.match(r"(\d{1,2})h(\d{2})\s*-\s*(\d{1,2})h(\d{2})", heures_str.strip())
    if not match:
        return None, None
    h1, m1, h2, m2 = match.groups()
    return f"{int(h1):02d}:{m1}", f"{int(h2):02d}:{m2}"


def parse_lessons(html: str) -> list:
    """Extrait chaque cours réservé depuis les blocs modaux 'event-drive-XXXXX'."""
    lessons = []
    blocks = re.split(r'data-ref="(event-drive-\d+)"', html)
    # re.split avec groupe capturant alterne : [avant, id1, bloc1, id2, bloc2, ...]
    for i in range(1, len(blocks), 2):
        event_id = blocks[i]
        block = blocks[i + 1] if i + 1 < len(blocks) else ""

        date_match = re.search(r'font-weight-boldest">\s*([^<]+?)\s*</div>\s*<span>([^<]+)</span>', block)
        if not date_match:
            continue
        date_fr, heures_str = date_match.groups()
        date_ymd = parse_date_fr(date_fr)
        heure_debut, heure_fin = parse_heures(heures_str)
        if not date_ymd or not heure_debut:
            continue

        lieu_match = re.search(
            r'planning-adresse">\s*<div class="font-weight-boldest">\s*([^<]+?)</div>'
            r'<div>([^<]*)</div><div>([^<]*)</div>',
            block,
        )
        lieu_nom = lieu_match.group(1).strip() if lieu_match else ""
        adresse = lieu_match.group(2).strip() if lieu_match else ""
        cp_ville = lieu_match.group(3).strip() if lieu_match else ""
        ville_match = re.search(r"\d{5}\s+(.+)", cp_ville)
        ville = ville_match.group(1).strip() if ville_match else cp_ville

        moniteur_match = re.search(r'font-weight-boldest mb-3">\s*Avec\s+([^<]+)</div>', block)
        moniteur = moniteur_match.group(1).strip() if moniteur_match else ""

        annee, mois, jour = date_ymd
        lessons.append({
            "id": event_id,
            "date": f"{annee:04d}-{mois:02d}-{jour:02d}",
            "heure_debut": heure_debut,
            "heure_fin": heure_fin,
            "lieu_nom": lieu_nom,
            "adresse": adresse,
            "ville": ville,
            "moniteur": moniteur,
        })
    return lessons


def write_json(lessons: list) -> None:
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(lessons, f, ensure_ascii=False, indent=2)


def write_ics(lessons: list) -> None:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Stych Watcher//FR"]
    for lesson in lessons:
        dt_start = datetime.strptime(f"{lesson['date']} {lesson['heure_debut']}", "%Y-%m-%d %H:%M")
        dt_end = datetime.strptime(f"{lesson['date']} {lesson['heure_fin']}", "%Y-%m-%d %H:%M")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{lesson['id']}@stych-watcher",
            f"DTSTART:{dt_start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND:{dt_end.strftime('%Y%m%dT%H%M%S')}",
            f"SUMMARY:Conduite avec {lesson['moniteur']}",
            f"LOCATION:{lesson['lieu_nom']}, {lesson['adresse']}, {lesson['ville']}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    with open(OUTPUT_ICS, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    session = requests.Session()
    login(session)
    lessons = get_all_lessons(session, nb_mois=7)

    if not lessons:
        print("Aucun cours trouvé dans le planning (ou structure de page différente de celle attendue).")
        return

    print(f"{len(lessons)} cours trouvé(s) :\n")
    for lesson in lessons:
        print(
            f"{lesson['date']} {lesson['heure_debut']}-{lesson['heure_fin']} "
            f"avec {lesson['moniteur']} — {lesson['lieu_nom']} ({lesson['ville']})"
        )

    write_json(lessons)
    write_ics(lessons)
    print(f"\nFichiers générés : {OUTPUT_JSON} et {OUTPUT_ICS}")


if __name__ == "__main__":
    main()

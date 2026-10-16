import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


import jwt
from flask import Flask, render_template, request, redirect, session, url_for, flash, g
from dotenv import load_dotenv
from supabase import create_client

from lib.supabase_client import get_client, get_authenticated_client

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

# Render läuft in UTC -> "heute" immer nach österreichischer Zeit bestimmen,
# sonst ist zwischen 0 und 2 Uhr noch "gestern"
TIMEZONE = ZoneInfo("Europe/Vienna")


def local_today():
    return datetime.now(TIMEZONE).date()


# Cookie-Sessions absichern (auf Render läuft alles über HTTPS)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))
# Login bleibt 30 Tage erhalten (auch wenn Browser/App am Handy geschlossen wird)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)


def current_client():
    token = session.get("access_token")
    refresh = session.get("refresh_token")
    if not token or not refresh:
        return None
    session.permanent = True  # auch bestehende Logins auf 30 Tage umstellen

    try:
        exp = jwt.decode(token, options={"verify_signature": False})["exp"]
    except Exception:
        exp = 0

    # Weniger als 60 Sekunden gültig -> erneuern
    if exp - time.time() < 60:
        try:
            fresh = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_ANON_KEY"])
            res = fresh.auth.refresh_session(refresh)
        except Exception:
            return None
        session["access_token"] = res.session.access_token
        session["refresh_token"] = res.session.refresh_token
        token = res.session.access_token

    return get_authenticated_client(token)


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session or current_client() is None:
            session.clear()
            # Hintergrund-Speichern (autosave) bekommt JSON statt einer Login-Seite
            if request.headers.get("X-Requested-With") == "fetch":
                return {"ok": False, "error": "login"}, 401
            flash("Sitzung abgelaufen, bitte neu einloggen.", "error")
            # nach dem Login zurück auf die Seite, auf der man war
            return redirect(url_for("login", next=request.path if request.method == "GET" else None))
        return view(*args, **kwargs)

    return wrapped

# Ermöglicht float eingaben beim Workout erfassung
def to_float(value):
    value = (value or "").strip().replace(",", ".")
    return float(value) if value else None

ENDURANCE_KEYS = ["distance_km", "duration_s", "elevation_m", "heart_rate_avg"]

DURATION_ERROR = "Dauer bitte als Minuten:Sekunden oder Stunden:Minuten:Sekunden eingeben, z.B. 52:30 oder 1:02:05."


def parse_duration(text):
    """Zeit-Eingabe -> Sekunden (so wird es in der DB gespeichert).
    '52:30' -> 3150, '1:02:05' -> 3725, '45' -> 2700 (nur eine Zahl = Minuten)
    """
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) > 3 or not all(p.strip().isdigit() for p in parts):
        raise ValueError(DURATION_ERROR)
    nums = [int(p) for p in parts]
    if len(nums) == 1:
        return nums[0] * 60
    if any(n >= 60 for n in nums[1:]):  # Minuten/Sekunden nach dem ersten ":" max. 59
        raise ValueError(DURATION_ERROR)
    h, m, s = [0] * (3 - len(nums)) + nums
    return h * 3600 + m * 60 + s


def collect_exercise_blocks(form):
    """Liest alle Übungsblöcke aus dem Formular.

    Feldnamen: ex_<n>_id, ex_<n>_new_name, ex_<n>_mode, ex_<n>_set_<m>_weight, ex_<n>_set_<m>_reps,
    ex_<n>_set_<m>_time, ex_<n>_set_<m>_dist (Meter),
    ex_<n>_distance_km usw. <n> = Nummer der Übung, <m> = Nummer des Satzes.
    """
    keys = list(form.keys())
    indices = sorted({int(m.group(1)) for k in keys if (m := re.match(r"ex_(\d+)_id$", k))})
    blocks = []
    for n in indices:
        # Satznummern: aus Wdh.- ODER Zeit-Feldern (bei Zeitübungen ist das Wdh.-Feld deaktiviert)
        set_nums = sorted({int(m.group(1)) for k in keys if (m := re.match(rf"ex_{n}_set_(\d+)_(reps|time)$", k))})
        mode = form.get(f"ex_{n}_mode")   # '' = Gewicht, 'bw' = Körpergewicht, 'time' = Zeit, None = Feld fehlt
        if mode is None and form.get(f"ex_{n}_bw") is not None:   # Seite von vor dem Update
            mode = "bw" if form.get(f"ex_{n}_bw") == "1" else ""
        blocks.append({
            "n": n,
            "exercise_id": form.get(f"ex_{n}_id") or None,
            "new_name": form.get(f"ex_{n}_new_name", "").strip(),
            "sets": [{
                "weight": form.get(f"ex_{n}_set_{m}_weight"),
                "reps": form.get(f"ex_{n}_set_{m}_reps"),
                "time": form.get(f"ex_{n}_set_{m}_time"),
                "dist": form.get(f"ex_{n}_set_{m}_dist"),   # Meter
            } for m in set_nums],
            "endurance": {key: form.get(f"ex_{n}_{key}") for key in ENDURANCE_KEYS},
            "timer": form.get(f"ex_{n}_timer_s", "").strip(),
            "mode": mode,
        })
    return blocks

# ------------------------------------------------------------
# Workouts laden: DB-Zeilen (EAV) -> handliche Struktur für die Templates
# ------------------------------------------------------------
# Supabase kann verknüpfte Tabellen direkt mitladen ("embedding"):
# workouts -> workout_entries -> exercises + entry_metrics -> metric_definitions
WORKOUT_SELECT = (
    "id, date, start_time, end_time, notes, status, created_at, workout_type_id, workout_types(key, label), "
    "workout_entries(id, order_index, exercise_id, exercises(name, is_timer, bw_factor, is_timed),"
    "entry_metrics(set_number, value, metric_definitions(key)))"
)


def clean_number(v):
    """62.0 -> 62, 62.5 -> 62.5 (für schöne Anzeige)"""
    f = float(v)
    return int(f) if f.is_integer() else f


def bodyweight_on(d):
    """Körpergewicht am Tag d: letzter Eintrag am oder vor d.
    Gibt es davor keinen (Gewicht erst später eingetragen) -> ältester Eintrag. Sonst None.
    Wird pro Seitenaufruf nur einmal aus der DB geladen (flask.g)."""
    if "bodyweights" not in g:
        client = current_client()
        mid = metric_id(client, "bodyweight_kg") if client else None
        rows = [] if mid is None else (
            client.table("measurements").select("value, date")
            .eq("metric_definition_id", mid).order("date").execute().data
        )
        g.bodyweights = [(date.fromisoformat(r["date"]), float(r["value"])) for r in rows]
    before = [v for day, v in g.bodyweights if day <= d]
    if before:
        return before[-1]
    return g.bodyweights[0][1] if g.bodyweights else None


def build_workout(raw):
    """Baut aus einer DB-Zeile ein Workout-Dict:
    {id, date, start_time, notes, type_key, type_label,
     entries: [{name, sets: [{weight_kg, reps}], endurance: {distance_km, ...}}],
     summary: {sets, volume, distance_km, duration_s}}
    """
    entries = []
    for e in sorted(raw.get("workout_entries") or [], key=lambda x: x["order_index"]):
        sets, endurance = {}, {}
        for m in e.get("entry_metrics") or []:
            key = m["metric_definitions"]["key"]
            value = clean_number(m["value"])
            if m["set_number"] is None:
                endurance[key] = value
            else:
                sets.setdefault(m["set_number"], {})[key] = value
        ex = e.get("exercises") or {}
        entries.append({
            "exercise_id":e["exercise_id"],
            "name": ex.get("name", "?"),
            "is_timer": bool(ex.get("is_timer")),
            "bw_factor": float(ex["bw_factor"]) if ex.get("bw_factor") else None,
            "is_timed": bool(ex.get("is_timed")),   # Zeitübung (Row, Sled, Plank …)
            "sets": [sets[n] for n in sorted(sets)],
            "endurance": endurance,
        })

    workout_date = date.fromisoformat(raw["date"])

    # Körpergewichtsübungen: bewegte Last = Körpergewicht x Faktor + Zusatzgewicht
    for e in entries:
        if e["bw_factor"] and e["sets"]:
            bw = bodyweight_on(workout_date)
            for st in e["sets"]:
                if bw:
                    st["load_kg"] = round(bw * e["bw_factor"] + st.get("weight_kg", 0), 1)

    summary = {"sets": 0, "reps": 0, "volume": 0, "distance_km": 0, "duration_s": 0,
               "set_time_s": 0, "set_distance_km": 0}   # set_* = Summe der Zeitübungen
    for e in entries:
        if e["is_timer"]:
            continue
        for st in e["sets"]:
            summary["sets"] += 1
            summary["reps"] += st.get("reps", 0)
            summary["set_time_s"] += st.get("duration_s", 0)
            summary["set_distance_km"] += st.get("distance_km", 0)
            load = st.get("load_kg", st.get("weight_kg"))   # Körpergewicht oder Hantelgewicht
            if load is not None and "reps" in st:
                summary["volume"] += load * st["reps"]
        summary["distance_km"] += e["endurance"].get("distance_km", 0)
        summary["duration_s"] += e["endurance"].get("duration_s", 0)

    wtype = raw.get("workout_types") or {}
    return {
        "id": raw["id"],
        "type_id": raw["workout_type_id"],
        "date": workout_date,
        "start_time": (raw.get("start_time") or "")[:5] or None,
        "notes": raw.get("notes"),
        "status": raw.get("status") or "finished",   # 'active' = läuft gerade
        "created_at": raw.get("created_at"),
        "end_time": raw.get("end_time"),
        "type_key": wtype.get("key"),
        "type_label": wtype.get("label", ""),
        "entries": entries,
        "summary": summary,
    }


# ------------------------------------------------------------
# Anzeige-Filter für Templates (deutsches Format)
# ------------------------------------------------------------
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTHS = ["Jänner", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


@app.template_filter("num")
def format_number(v, decimals=1):
    """1234.5 -> '1.234,5'"""
    if v is None or v == "":
        return "–"
    v = float(v)
    text = f"{v:,.0f}" if v.is_integer() else f"{v:,.{decimals}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


@app.template_filter("duration")
def format_duration(seconds):
    """3120 -> '52:00', 3725 -> '1:02:05'"""
    if not seconds:
        return "–"
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

@app.template_filter("hm")
def format_hours_minutes(seconds):
    """4320 -> '1 h 12 min', 2880 -> '48 min' (für die Trainingsdauer)"""
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"

@app.template_filter("pace")
def format_pace(seconds_per_km):
    """312.5 -> '5:12' (min/km)"""
    m, s = divmod(int(round(seconds_per_km)), 60)
    return f"{m}:{s:02d}"

@app.template_filter("countdown")
def format_countdown(days):
    """Tage bis zum Event -> 'Noch 12 Tage', 'Morgen', 'Heute', 'Vorbei'"""
    if days is None:
        return ""
    if days > 1:
        return f"Noch {days} Tage"
    return {1: "Morgen", 0: "Heute"}.get(days, "Vorbei")

@app.template_filter("weekday_date")
def format_weekday_date(d):
    """date(2026, 9, 29) -> 'Di, 29.9.'"""
    return f"{WEEKDAYS[d.weekday()]}, {d.day}.{d.month}."


@app.template_filter("long_date")
def format_long_date(d):
    """date(2026, 9, 29) -> 'Di, 29. September 2026'"""
    return f"{WEEKDAYS[d.weekday()]}, {d.day}. {MONTHS[d.month - 1]} {d.year}"




# Legt die Profil Zeile an, falls sie noch fehlt
def ensure_profile(client, user):
    meta = getattr(user, "user_metadata", None) or {}
    try:
        client.table("profiles").upsert(
            {"id": user.id, "display_name": meta.get("display_name") or user.email},
            ignore_duplicates=True,
        ).execute()
    except Exception as e:
        print(f"[WARN] Profil-Anlage fehlgeschlagen: {e}")

# Eingeloggt -> Dashboard, ansonsten login
@app.route("/", methods=["GET"])
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


# Registrierung
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form["email"].strip()
        password = request.form["password"]
        display_name = request.form.get("display_name", "").strip() or email

        client = get_client()
        try:
            res = client.auth.sign_up({
                "email": email,
                "password": password,
                "options": {"data":{"display_name": display_name}},
            })
        except Exception as e:
            flash(f"Signup fehlgeschlagen: {e}", "error")
            return render_template("signup.html")

        if res.user is None:
            flash("Signup fehlgeschlagen. Bitte Eingaben pruefen.", "error")
            return render_template("signup.html")

        flash("Account erstellt. Bitte einloggen.", "success")
        return redirect(url_for("login"))

    return render_template("signup.html")


# Login
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip()
        password = request.form["password"]

        client = get_client()
        try:
            res = client.auth.sign_in_with_password({"email": email, "password": password})
        except Exception as e:
            flash(f"Login fehlgeschlagen: {e}", "error")
            return render_template("login.html")

        session.permanent = True
        session["access_token"] = res.session.access_token
        session["refresh_token"] = res.session.refresh_token
        session["user_id"] = res.user.id
        session["email"] = res.user.email

        ensure_profile(get_authenticated_client(res.session.access_token), res.user)
        next_url = request.args.get("next", "")
        if next_url.startswith("/") and not next_url.startswith("//"):  # nur Seiten dieser App
            return redirect(next_url)
        return redirect(url_for("dashboard"))

    return render_template("login.html")


# Logout und wieder auf Login anzeigen
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ------------------------------------------------------------
# Profil: persönliche Daten (Name, Größe, Geburtsdatum, Körpergewicht)
# Größe + Geburtsdatum in profiles, Körpergewicht als Messwert in measurements
# (Verlauf bleibt erhalten -> später Volumen von Körpergewichtsübungen je Datum)
# ------------------------------------------------------------
def metric_id(client, key):
    rows = client.table("metric_definitions").select("id").eq("key", key).execute().data
    return rows[0]["id"] if rows else None

def latest_bodyweight(client):
    mid = metric_id(client, "bodyweight_kg")
    if mid is None:
        return None
    rows = (
        client.table("measurements").select("value, date")
        .eq("metric_definition_id", mid)
        .order("date", desc=True).limit(1).execute().data
    )
    return rows[0] if rows else None

def age_from(birth):
    today = local_today()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    client = current_client()
    welcome = request.args.get("welcome") == "1"

    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        errors = []

        try:
            height = to_float(request.form.get("height_cm"))
        except ValueError:
            height, errors = None, errors + ["Größe bitte als Zahl in cm eingeben."]
        if height is not None and not 100 <= height <= 250:
            errors.append("Größe bitte zwischen 100 und 250 cm eingeben.")

        try:
            weight = to_float(request.form.get("bodyweight_kg"))
        except ValueError:
            weight, errors = None, errors + ["Gewicht bitte als Zahl in kg eingeben."]
        if weight is not None and not 30 <= weight <= 300:
            errors.append("Gewicht bitte zwischen 30 und 300 kg eingeben.")
        
        if welcome and weight is None:
            errors.appen("Bitte dein Körpergewicht eintragen.")

        birth = None
        birth_text = request.form.get("birth_date", "").strip()
        if birth_text:
            try:
                birth = date.fromisoformat(birth_text)
            except ValueError:
                errors.append("Geburtsdatum ungültig.")
            else:
                if not 10 <= age_from(birth) <= 100:
                    errors.append("Geburtsdatum bitte prüfen.")

        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("profile", welcome=1) if welcome else url_for("profile"))

        client.table("profiles").update({
            "display_name": display_name or session.get("email"),
            "height_cm": height,
            "birth_date": birth.isoformat() if birth else None,
        }).eq("id", session["user_id"]).execute()

        # Gewicht nur speichern, wenn es sich geändert hat; pro Tag ein Wert (überschreibt)
        last = latest_bodyweight(client)
        if weight is not None and (last is None or float(last["value"]) != weight):
            client.table("measurements").upsert({
                "user_id": session["user_id"],
                "metric_definition_id": metric_id(client, "bodyweight_kg"),
                "value": weight,
                "date": local_today().isoformat(),
            }, on_conflict="user_id,metric_definition_id,date").execute()

        if weight is not None:
            session["has_bodyweight"] = True

        if welcome:
            flash("Alles eingerichtet - viel Spaß beim Training!", "success")
            return redirect(url_for("dashboard"))

        flash("Profil gespeichert.", "success")
        return redirect(url_for("profile"))

    rows = (
        client.table("profiles").select("display_name, height_cm, birth_date")
        .eq("id", session["user_id"]).execute().data
    )
    prof = rows[0] if rows else {}
    weight = latest_bodyweight(client)
    birth = date.fromisoformat(prof["birth_date"]) if prof.get("birth_date") else None
    return render_template(
        "profile.html",
        prof=prof,
        email=session.get("email"),
        height=format_number(prof.get("height_cm"), 0) if prof.get("height_cm") is not None else "",
        weight=format_number(weight["value"]) if weight else "",
        weight_date=format_long_date(date.fromisoformat(weight["date"])) if weight else None,
        age=age_from(birth) if birth else None,
        welcome=welcome,
    )



# ------------------------------------------------------------
# Workout starten / live erfassen / beenden / bearbeiten
# Ablauf: Start Workout -> Workout wird SOFORT in der DB angelegt (status 'active')
# -> jede Eingabe wird automatisch gespeichert (autosave) -> End Workout (status 'finished')
# ------------------------------------------------------------
def load_exercises(client, include_archived=False):
    """Übungen fürs Dropdown. Gelöschte (archived) nur, wenn ausdrücklich gewünscht."""
    q = client.table("exercises").select("id, name, workout_type_id, archived, is_timer, created_by, bw_factor, is_timed")
    if not include_archived:
        q = q.eq("archived", False)
    return q.order("name").execute().data

def normalize_name(name):
    """' Weighted  pull ups ' -> 'weighted pull ups' (zum Vergleichen von Übungsnamen)"""
    return " ".join((name or "").split()).lower()


# Wie viel vom Körpergewicht wird bei einer Übung bewegt? Grobe Richtwerte nach Übungsname.
# Erster Treffer gewinnt (darum "knie-liegestütz" vor "liegestütz"). Unbekannt = 100 %.
BW_FACTORS = [
    (("knieliegest", "knie-liegest", "knie liegest", "knee push"), 0.5),
    (("liegest", "push-up", "pushup", "push up"), 0.65),
    (("rudern", "inverted", "australian"), 0.5),
    (("sit-up", "situp", "sit up", "crunch"), 0.3),
    (("kniebeuge", "squat", "ausfallschritt", "lunge", "step-up", "step up"), 0.7),
]


def guess_bw_factor(name):
    n = normalize_name(name)
    return next((f for words, f in BW_FACTORS if any(w in n for w in words)), 1.0)


def find_exercise(exercises, name, workout_type):
    """Gibt es diese Übung (gleicher Name, egal ob groß/klein, gleicher Typ) schon? -> Zeile oder None"""
    wanted = normalize_name(name)
    return next((x for x in exercises
                 if normalize_name(x["name"]) == wanted and (x["workout_type_id"] == workout_type or x.get("is_timer"))), None)

def active_workout_id(client):
    """ID des laufenden Workouts oder None."""
    rows = (
        client.table("workouts").select("id").eq("status", "active")
        .order("created_at", desc=True).limit(1).execute()
    )
    return rows.data[0]["id"] if rows.data else None


def parse_workout_form(form, is_strength):
    """Prüft das Formular und rechnet alle Werte um – noch OHNE zu speichern.
    Rückgabe: (entries, fehlermeldung). Bei Fehler ist entries None.
    Leere Workouts sind erlaubt (direkt nach dem Start ist noch nichts eingetragen).
    """
    entries = []
    try:
        for b in collect_exercise_blocks(form):
            if not b["exercise_id"] and not b["new_name"]:
                continue  # leerer Übungsblock -> ignorieren
            metrics = []  # Liste von (metrik_key, satz_nummer, wert)
            # Erfassungsart: '' = Gewicht, 'bw' = Körpergewicht, 'time' = Zeit, None = Feld fehlt (alte Seite)
            b = {**b, "bw_on": b["mode"] == "bw", "timed": b["mode"] == "time", "mode_given": b["mode"] is not None}
            if b["timer"]:
                metrics.append(("duration_s", None, parse_duration(b["timer"])))
            elif is_strength:
                set_no = 0
                for st in b["sets"]:
                    weight = to_float(st["weight"])
                    if b["timed"]:   # Zeitübung: Zeit (Pflicht), Distanz in m + Gewicht optional
                        seconds = parse_duration(st["time"]) if (st["time"] or "").strip() else None
                        if seconds is None:
                            continue  # Satz ohne Zeit -> leere Zeile
                        dist_m = to_float(st["dist"])
                        set_no += 1
                        metrics.append(("duration_s", set_no, seconds))
                        if dist_m is not None:
                            metrics.append(("distance_km", set_no, dist_m / 1000))
                    else:
                        reps = to_float(st["reps"])
                        if reps is None:
                            continue  # Satz ohne Wiederholungen -> leere Zeile
                        set_no += 1
                        metrics.append(("reps", set_no, reps))
                    if weight is not None:  # ohne Gewicht = Körpergewichtsübung
                        metrics.append(("weight_kg", set_no, weight))
            else:
                for key in ENDURANCE_KEYS:
                    raw = b["endurance"].get(key)
                    val = parse_duration(raw) if key == "duration_s" else to_float(raw)
            
                    if val is not None:
                        metrics.append((key, None, val))
            entries.append({**b, "metrics": metrics})
    except ValueError as ex:
        if str(ex) == DURATION_ERROR:
            return None, DURATION_ERROR
        return None, "Bitte bei Gewicht, Wiederholungen und Ausdauerwerten nur Zahlen eingeben."
    return entries, None


def save_workout_form(client, workout_id, form):
    """Speichert den aktuellen Stand des Formulars in ein bestehendes Workout.

    Das eigentliche Speichern übernimmt die DB-Funktion save_workout():
    Workout + alle Übungen + alle Werte in EINER Transaktion (alles oder nichts).
    Rückgabe: (neu_angelegte_übungen, fehlermeldung)
    neu_angelegte_übungen = {"<übungsnummer>": {"id": ..., "name": ...}} -> damit das
    Formular die neue Übung übernimmt und sie beim nächsten Speichern nicht nochmal anlegt.
    """
    workout_type = int(form["workout_type"])
    entries, error = parse_workout_form(form, is_strength=(workout_type == 1))
    if error:
        return None, error

    created = {}
    payload_entries = []
    known = None  # vorhandene Übungen, nur laden wenn nötig
    is_strength = workout_type == 1
    for e in entries:
        # Krafttraining: geänderte Belastungsart (Gewicht / Körpergewicht) an der eigenen Übung merken
        if is_strength and e["exercise_id"]:
            if known is None:
                known = load_exercises(client, include_archived=True)
            row = next((x for x in known if x["id"] == e["exercise_id"]), None)
            if row and row.get("created_by") == session["user_id"] and not row.get("is_timer") \
                    and e["mode_given"] and (bool(row.get("bw_factor")) != e["bw_on"]
                                             or bool(row.get("is_timed")) != e["timed"]):
                change = {
                    "bw_factor": (row.get("bw_factor") or guess_bw_factor(row["name"])) if e["bw_on"] else None,
                    "is_timed": e["timed"],
                }
                client.table("exercises").update(change).eq("id", row["id"]).execute()
                row.update(change)
        exercise_id = e["exercise_id"]
        if not exercise_id:  # neuer Name eingetippt
            if known is None:
                known = load_exercises(client, include_archived=True)
            row = find_exercise(known, e["new_name"], workout_type)
            if row and row.get("archived"):  # gelöschte Übung wieder eingetippt -> zurückholen
                client.table("exercises").update({"archived": False}).eq("id", row["id"]).execute()
                row["archived"] = False
            if row is None:  # gibt es wirklich noch nicht -> anlegen
                row = client.table("exercises").insert({
                    "workout_type_id": workout_type,
                    "name": " ".join(e["new_name"].split()),  # doppelte Leerzeichen raus
                    "is_custom": True,
                    "created_by": session["user_id"],
                    "bw_factor": guess_bw_factor(e["new_name"]) if is_strength and e["bw_on"] else None,
                    "is_timed": is_strength and e["timed"],
                }).execute().data[0]
                known.append(row)
            exercise_id = row["id"]
            created[str(e["n"])] = {
                "id": exercise_id, "name": row["name"], "type": workout_type,
                "own": row.get("created_by") == session["user_id"],
                "bw": row.get("bw_factor"),
                "timed": bool(row.get("is_timed")),
            }
        payload_entries.append({
            "exercise_id": exercise_id,
            "metrics": [{"key": k, "set_number": n, "value": v} for k, n, v in e["metrics"]],
        })


    payload = {
        "id": workout_id,
        "workout_type_id": workout_type,
        "date": form.get("date") or str(local_today()),
        "start_time": form.get("start_time") or None,
        "notes": form.get("notes", "").strip() or None,
        "entries": payload_entries,
    }
    try:
        client.rpc("save_workout", {"p": payload}).execute()
    except Exception as ex:
        return None, f"Speichern fehlgeschlagen: {ex}"
    return created, None


def form_state(w):
    """Workout -> Daten zum Vorbefüllen des Formulars (wird als JSON ans JavaScript gegeben)."""
    return {
        "type_id": w["type_id"],
        "date": w["date"].isoformat(),
        "start_time": w["start_time"] or "",
        "notes": w["notes"] or "",
        "entries": [
            {"exercise_id": e["exercise_id"], "new_name": "",
             "mode": "time" if e["is_timed"] else "bw" if e["bw_factor"] else "",
             "sets": [{k: v for k, v in st.items() if k != "load_kg"} for st in e["sets"]],
             "endurance": e["endurance"]}
            for e in w["entries"]
        ],
    }

def last_values(workouts, current):
    """Pro Übung die Werte aus dem letzten Workout VOR diesem -> Platzhalter im Formular.
    Rückgabe: {exercise_id: {"date": "Di, 24.9.", "sets": [...], "endurance": {...}}}
    """
    when = lambda w: (w["date"], w["start_time"] or "")
    result = {}
    for w in sorted(workouts, key=when):  # alt -> neu: neuere Workouts überschreiben ältere
        if w["id"] == current["id"] or when(w) > when(current):
            continue  # das Workout selbst und spätere zählen nicht
        for e in w["entries"]:
            if e["sets"] or e["endurance"]:
                result[e["exercise_id"]] = {
                    "date": format_weekday_date(w["date"]),
                    "sets": e["sets"],
                    "endurance": e["endurance"],
                }
    return result

# ------------------------------------------------------------
# Fortschritt & Abschluss-Statistik
# ------------------------------------------------------------
def set_progress(now, last):
    """Vergleicht einen Satz mit dem gleichen Satz vom letzten Mal.
    Mehr Gewicht (mit Wiederholungen) oder gleiches Gewicht mit mehr Wdh. = Fortschritt.
    Rückgabe: '+2,5 kg' / '+2 Wdh.' oder None.
    """
    reps, last_reps = now.get("reps"), last.get("reps")
    weight, last_weight = now.get("weight_kg"), last.get("weight_kg")
    sec, last_sec = now.get("duration_s"), last.get("duration_s")
    if sec and last_sec and not reps:   # Zeitübung
        dist, last_dist = now.get("distance_km"), last.get("distance_km")
        if weight != last_weight or dist != last_dist:
            return None   # nur bei gleicher Distanz + gleichem Gewicht vergleichbar
        if dist and sec < last_sec:      # gleiche Strecke schneller
            return f"{format_duration(last_sec - sec)} schneller"
        if not dist and sec > last_sec:  # z.B. Plank: länger gehalten
            return f"{format_duration(sec - last_sec)} länger"
        return None
    if not reps:
        return None
    if weight is not None and ((last_weight is None and weight > 0)
                               or (last_weight is not None and weight > last_weight)):
        return f"+{format_number(weight - (last_weight or 0))} kg"
    if weight == last_weight and last_reps is not None and reps > last_reps:
        return f"+{format_number(reps - last_reps, 0)} Wdh."
    return None

def workout_progress(w, last):
    """Fortschritte pro Übung gegenüber dem letzten Mal.
    Rückgabe: [{"name": "Bankdrücken", "changes": ["Satz 1: +2,5 kg", ...]}, ...]
    """
    result = []
    for e in w["entries"]:
        prev = (last.get(e["exercise_id"]) or {}).get("sets") or []
        changes = []
        for i, s in enumerate(e["sets"]):
            if i < len(prev):
                p = set_progress(s, prev[i])
                if p:
                    changes.append(f"Satz {i + 1}: {p}")
        if changes:
            result.append({"name": e["name"], "changes": changes})
    return result

def workout_duration(w):
    """Sekunden von Start (Datum + Uhrzeit) bis "End Workout" – oder None, wenn nicht sinnvoll."""
    if not w["end_time"]:
        return None
    end = datetime.fromisoformat(w["end_time"])
    if w["start_time"]:
        start = datetime.combine(w["date"], datetime.strptime(w["start_time"], "%H:%M").time(), TIMEZONE)
    elif w["created_at"]:
        start = datetime.fromisoformat(w["created_at"])
    else:
        return None
    seconds = (end - start).total_seconds()
    return seconds if 0 < seconds <= 6 * 3600 else None   # nachgetragene Workouts nicht mitzählen


@app.route("/exercises/<exercise_id>/archive", methods=["POST"])
@login_required
def archive_exercise(exercise_id):
    """Übung aus dem Dropdown löschen. Nur ausgeblendet (archived) -> alte Workouts behalten sie."""
    client = current_client()
    rows = (
        client.table("exercises").update({"archived": True})
        .eq("id", exercise_id).eq("created_by", session["user_id"]).execute()
    )
    if not rows.data:
        return {"ok": False, "error": "Diese Übung kann nicht gelöscht werden."}, 404
    return {"ok": True}

@app.route("/workouts/new", methods=["GET", "POST"])
@login_required
def new_workout():
    """GET: Startseite (Typ, Datum, Uhrzeit). POST: Workout sofort in der DB anlegen."""
    client = current_client()

    # Läuft schon ein Workout? Dann dorthin, statt ein zweites zu starten
    running = active_workout_id(client)
    if running:
        return redirect(url_for("edit_workout", workout_id=running))

    if request.method == "POST":
        created = client.table("workouts").insert({
            "user_id": session["user_id"],
            "workout_type_id": int(request.form.get("workout_type", 1)),
            "date": request.form.get("date") or str(local_today()),
            "start_time": request.form.get("start_time") or None,
            "status": "active",
        }).execute()
        return redirect(url_for("edit_workout", workout_id=created.data[0]["id"]))

    return render_template("workout_start.html")


@app.route("/workouts/<workout_id>/edit")
@login_required
def edit_workout(workout_id):
    """Laufendes Workout erfassen ODER fertiges Workout bearbeiten – gleiches Formular."""
    client = current_client()
    rows = client.table("workouts").select(WORKOUT_SELECT).eq("id", workout_id).execute()
    if not rows.data:
        flash("Workout nicht gefunden.", "error")
        return redirect(url_for("workouts_list"))
    w = build_workout(rows.data[0])
    return render_template(
        "workout_form.html",
        exercises=load_exercises(client),
        workout=w,
        initial=form_state(w),
        last=last_values(load_all_workouts(client), w),
    )


@app.route("/workouts/<workout_id>/autosave", methods=["POST"])
@login_required
def autosave_workout(workout_id):
    """Wird vom Formular im Hintergrund aufgerufen. Antwortet mit JSON statt einer Seite."""
    client = current_client()
    # Gibt es das Workout und gehört es mir? (RLS liefert fremde Workouts nicht aus)
    if not client.table("workouts").select("id").eq("id", workout_id).execute().data:
        return {"ok": False, "error": "Workout nicht gefunden."}, 404

    created, error = save_workout_form(client, workout_id, request.form)
    if error:
        return {"ok": False, "error": error}, 400
    return {"ok": True, "created": created}


@app.route("/workouts/<workout_id>/end", methods=["POST"])
@login_required
def end_workout(workout_id):
    client = current_client()
    rows = client.table("workouts").select(WORKOUT_SELECT).eq("id", workout_id).execute()
    if not rows.data:
        flash("Workout nicht gefunden.", "error")
        return redirect(url_for("workouts_list"))

    if not build_workout(rows.data[0])["entries"]:
        flash("Trage mindestens eine Übung ein um das Workout zu speichern.", "error")
        return redirect(url_for("edit_workout", workout_id=workout_id))
    client.table("workouts").update({
        "status": "finished",
        "end_time": datetime.now(TIMEZONE).isoformat(),   # für die Trainingsdauer
    }).eq("id", workout_id).execute()
    return redirect(url_for("workout_summary", workout_id=workout_id))


@app.route("/workouts/<workout_id>/summary")
@login_required
def workout_summary(workout_id):
    """Abschluss-Seite nach "End Workout": Gesamtstatistik + Fortschritte."""
    client = current_client()
    rows = client.table("workouts").select(WORKOUT_SELECT).eq("id", workout_id).execute()
    if not rows.data:
        flash("Workout nicht gefunden.", "error")
        return redirect(url_for("workouts_list"))
    w = build_workout(rows.data[0])
    return render_template(
        "workout_summary.html",
        w=w,
        duration=workout_duration(w),
        progress=workout_progress(w, last_values(load_all_workouts(client), w)),
    )

@app.route("/workouts/<workout_id>/resume", methods=["POST"])
@login_required
def resume_workout(workout_id):
    """Beendetes Workout wieder auf 'active' setzen (z.B. zu früh beendet)."""
    client = current_client()
    running = active_workout_id(client)
    if running and running != workout_id:
        flash("Es läuft bereits ein anderes Workout. Beende das zuerst.", "error")
        return redirect(url_for("edit_workout", workout_id=running))

    client.table("workouts").update({"status": "active"}).eq("id", workout_id).execute()
    flash("Workout wieder aufgenommen.", "success")
    return redirect(url_for("edit_workout", workout_id=workout_id))

# ------------------------------------------------------------
# Workout-Bereich: Liste, Detailansicht, Löschen
# ------------------------------------------------------------
@app.route("/workouts")
@login_required
def workouts_list():
    client = current_client()
    rows = (
        client.table("workouts")
        .select(WORKOUT_SELECT)
        .order("date", desc=True)
        .order("start_time", desc=True)
        .execute()
    )
    workouts = [build_workout(r) for r in rows.data]

    # Nach Monaten gruppieren: [{"label": "September 2026", "workouts": [...]}, ...]
    months = []
    for w in workouts:
        label = f"{MONTHS[w['date'].month - 1]} {w['date'].year}"
        if not months or months[-1]["label"] != label:
            months.append({"label": label, "workouts": []})
        months[-1]["workouts"].append(w)

    return render_template("workouts.html", months=months)


@app.route("/workouts/<workout_id>")
@login_required
def workout_detail(workout_id):
    client = current_client()
    rows = client.table("workouts").select(WORKOUT_SELECT).eq("id", workout_id).execute()
    if not rows.data:  # gibt es nicht oder gehört einem anderen User (RLS)
        flash("Workout nicht gefunden.", "error")
        return redirect(url_for("workouts_list"))
    return render_template("workout_detail.html", w=build_workout(rows.data[0]))


@app.route("/workouts/<workout_id>/delete", methods=["POST"])
@login_required
def delete_workout(workout_id):
    client = current_client()
    # Übungen und Werte werden per "on delete cascade" automatisch mitgelöscht
    client.table("workouts").delete().eq("id", workout_id).execute()
    flash("Workout gelöscht.", "success")
    return redirect(url_for("workouts_list"))


# ------------------------------------------------------------
# Ziele (Goals): Liste, Detailansicht, Anlegen, Bearbeiten, Löschen
# ------------------------------------------------------------
GOAL_TYPES = {"event": "Event", "skill": "Skill", "general": "Allgemein"}
GOAL_STATUS = {"active": "Aktiv", "achieved": "Erreicht", "abandoned": "Verworfen"}

GOAL_SELECT = (
    "id, title, goal_type, description, target_date, target_value, status, created_at, "
    "metric_definition_id, exercise_id, metric_definitions(key, label, unit), exercises(name)"
)


@app.context_processor
def goal_labels():
    """Macht GOAL_TYPES und GOAL_STATUS in allen Templates verfügbar."""
    return {"GOAL_TYPES": GOAL_TYPES, "GOAL_STATUS": GOAL_STATUS}


def load_all_workouts(client):
    rows = client.table("workouts").select(WORKOUT_SELECT).order("date").execute()
    return [build_workout(r) for r in rows.data]


def goal_progress(goal, workouts):
    """Bester erreichter Wert für ein messbares Ziel + Fortschritt in Prozent.

    Sucht in allen Workouts (optional nur in der gewählten Übung) den besten Wert
    der Metrik. Bei Dauer ist weniger besser, bei allem anderen mehr.
    Rückgabe: None (nicht messbar) oder {best, target, percent, reached}
    """
    metric = goal.get("metric_definitions")
    if not metric or goal.get("target_value") is None:
        return None

    key = metric["key"]
    values = []
    for w in workouts:
        for e in w["entries"]:
            if e["is_timer"]:
                continue
            if goal["exercise_id"] and e["exercise_id"] != goal["exercise_id"]:
                continue
            values += [s[key] for s in e["sets"] if key in s]
            if key in e["endurance"]:
                values.append(e["endurance"][key])

    target = float(goal["target_value"])
    if not values:
        return {"best": None, "target": target, "percent": 0, "reached": False}

    lower_is_better = key == "duration_s"
    best = min(values) if lower_is_better else max(values)
    ratio = (target / best if best else 0) if lower_is_better else best / target
    return {
        "best": best,
        "target": target,
        "percent": round(min(ratio, 1) * 100),
        "reached": ratio >= 1,
    }


def enrich_goal(goal, workouts):
    """Ergänzt ein Ziel um Fortschritt und verbleibende Tage."""
    goal["progress"] = goal_progress(goal, workouts)
    goal["target_day"] = date.fromisoformat(goal["target_date"]) if goal.get("target_date") else None
    # Countdown nur bei Events
    is_event = goal.get("goal_type") == "event"
    goal["days_left"] = (goal["target_day"] - local_today()).days if goal["target_day"] and is_event else None
    return goal


def parse_goal_form(form):
    """Liest das Ziel-Formular. Rückgabe: (daten_für_db, fehlermeldung)"""
    row = {
        "title": form.get("title", "").strip(),
        "goal_type": form.get("goal_type", "general"),
        "description": form.get("description", "").strip() or None,
        "target_date": form.get("target_date") or None,
        "status": form.get("status", "active"),
        "metric_definition_id": None,
        "exercise_id": None,
        "target_value": None,
    }
    if not row["title"]:
        return None, "Bitte einen Titel eingeben."
    if row["goal_type"] not in GOAL_TYPES or row["status"] not in GOAL_STATUS:
        return None, "Ungültige Auswahl."

    if form.get("measurable"):  # optional: Ziel an Metrik + Zielwert koppeln
        try:
            row["target_value"] = to_float(form.get("target_value"))
        except ValueError:
            return None, "Der Zielwert muss eine Zahl sein."
        row["metric_definition_id"] = int(form.get("metric_definition_id") or 0) or None
        row["exercise_id"] = form.get("exercise_id") or None
        if not row["metric_definition_id"] or row["target_value"] is None:
            return None, "Für ein messbares Ziel brauchst du Metrik und Zielwert."
    return row, None


def render_goal_form(client, goal):
    metrics = client.table("metric_definitions").select("id, label, unit, key").order("id").execute().data
    return render_template(
        "goal_form.html",
        goal=goal,
        metrics=[m for m in metrics if m["key"] != "rpe"],
        exercises=load_exercises(client),
    )


@app.route("/goals")
@login_required
def goals_list():
    client = current_client()
    workouts = load_all_workouts(client)
    goals = client.table("goals").select(GOAL_SELECT).order("created_at", desc=True).execute().data
    goals = [enrich_goal(g, workouts) for g in goals]
    return render_template(
        "goals.html",
        active=[g for g in goals if g["status"] == "active"],
        finished=[g for g in goals if g["status"] != "active"],
    )


@app.route("/goals/new", methods=["GET", "POST"])
@login_required
def new_goal():
    client = current_client()
    if request.method == "POST":
        row, error = parse_goal_form(request.form)
        if error:
            flash(error, "error")
        else:
            row["user_id"] = session["user_id"]
            created = client.table("goals").insert(row).execute()
            flash("Ziel gespeichert.", "success")
            return redirect(url_for("goal_detail", goal_id=created.data[0]["id"]))
    return render_goal_form(client, goal=None)


@app.route("/goals/<goal_id>")
@login_required
def goal_detail(goal_id):
    client = current_client()
    rows = client.table("goals").select(GOAL_SELECT).eq("id", goal_id).execute()
    if not rows.data:
        flash("Ziel nicht gefunden.", "error")
        return redirect(url_for("goals_list"))
    return render_template("goal_detail.html", g=enrich_goal(rows.data[0], load_all_workouts(client)))


@app.route("/goals/<goal_id>/edit", methods=["GET", "POST"])
@login_required
def edit_goal(goal_id):
    client = current_client()
    rows = client.table("goals").select(GOAL_SELECT).eq("id", goal_id).execute()
    if not rows.data:
        flash("Ziel nicht gefunden.", "error")
        return redirect(url_for("goals_list"))

    if request.method == "POST":
        row, error = parse_goal_form(request.form)
        if error:
            flash(error, "error")
        else:
            client.table("goals").update(row).eq("id", goal_id).execute()
            flash("Änderungen gespeichert.", "success")
            return redirect(url_for("goal_detail", goal_id=goal_id))
    return render_goal_form(client, goal=rows.data[0])


@app.route("/goals/<goal_id>/status", methods=["POST"])
@login_required
def goal_status(goal_id):
    status = request.form.get("status")
    if status in GOAL_STATUS:
        current_client().table("goals").update({"status": status}).eq("id", goal_id).execute()
        flash(f"Ziel ist jetzt: {GOAL_STATUS[status]}.", "success")
    return redirect(url_for("goal_detail", goal_id=goal_id))


@app.route("/goals/<goal_id>/delete", methods=["POST"])
@login_required
def delete_goal(goal_id):
    current_client().table("goals").delete().eq("id", goal_id).execute()
    flash("Ziel gelöscht.", "success")
    return redirect(url_for("goals_list"))


# ------------------------------------------------------------
# Dashboard: Wochenübersicht, Verlauf, Entwicklung, Ziele, Hinweise
# ------------------------------------------------------------
def week_start(d):
    """Montag der Woche, in der d liegt."""
    return d - timedelta(days=d.weekday())


def e1rm(weight, reps):
    """Geschätztes 1-Wiederholungs-Maximum (Epley-Formel). Macht Sätze mit
    unterschiedlichen Wiederholungszahlen vergleichbar: 80 kg x 5 ≈ 93 kg."""
    if not weight or not reps:
        return None
    return weight if reps == 1 else weight * (1 + reps / 30)


def percent_change(new, old):
    if not new or not old:
        return None
    return round((new - old) / old * 100)


def totals(workouts):
    return {
        "count": len(workouts),
        "strength": sum(1 for w in workouts if w["type_key"] == "strength"),
        "endurance": sum(1 for w in workouts if w["type_key"] != "strength"),
        "volume": sum(w["summary"]["volume"] for w in workouts),
        "distance_km": sum(w["summary"]["distance_km"] for w in workouts),
        "duration_s": sum(w["summary"]["duration_s"] for w in workouts),
    }


CHART_WEEKS = 8   # so viele Wochen sind im Diagramm "Trainings pro Woche" gleichzeitig sichtbar


def week_grid(workouts, today, visible=CHART_WEEKS):
    """Daten für das Wochen-Raster am Dashboard.
    Spalten = Kalenderwochen (ab der ersten Trainingswoche), Zeilen = Tage Mo-So.
    Pro Tag: Liste der Workout-Typen an diesem Tag, z.B. ['strength', 'endurance'].
    Solange das Raster nicht voll ist, stehen rechts leere (zukünftige) Wochen.
    Ist es voll, rutscht die aktuelle Woche nach ganz rechts -> offset = Anzahl
    der links verdeckten Wochen (per Pfeil wieder sichtbar).
    """
    this_monday = week_start(today)
    first = min([week_start(w["date"]) for w in workouts] + [this_monday])
    weeks_until_now = (this_monday - first).days // 7 + 1          # inkl. aktueller Woche
    total = max(weeks_until_now, visible)

    types_by_day = {}
    for w in workouts:
        kind = "strength" if w["type_key"] == "strength" else "endurance"
        types_by_day.setdefault(w["date"], []).append(kind)

    weeks = []
    for i in range(total):
        monday = first + timedelta(weeks=i)
        days = []
        for d in range(7):
            day = monday + timedelta(days=d)
            types = set(types_by_day.get(day, []))
            if len(types) ==2:
                kind = "hybrid"        # Kraft und Ausdauer
            elif types:
                kind = types.pop()      # Kraft oder Ausdauer
            else: 
                kind = None
            days.append({
                "date": day.isoformat(),
                "kind": kind,
                "is_today": day == today, 
                "future": day > today,
            })
        weeks.append({"kw": monday.isocalendar()[1], "current": monday == this_monday, "days": days})

    return {"weeks": weeks, "offset": max(0, weeks_until_now - visible)}


def exercise_trends(workouts, today):
    """Entwicklung pro Übung: letzte 4 Wochen vs. die 4 Wochen davor.
    Kraft: bestes e1RM (bei Körpergewicht: meiste Wiederholungen).
    Ausdauer: Ø Pace (Sekunden pro km, weniger = schneller).
    """
    recent_from = today - timedelta(days=27)
    prev_from = today - timedelta(days=55)
    data = {}
    for w in workouts:
        period = "recent" if w["date"] >= recent_from else "prev" if w["date"] >= prev_from else None
        if not period:
            continue
        for e in w["entries"]:
            if e["is_timer"] or e["is_timed"]:
                continue
            d = data.setdefault(e["name"], {
                "name": e["name"], "last": w["date"], "kind": "strength" if e["sets"] else "endurance",
                "recent": [], "prev": [], "km": 0, "sec": {"recent": 0, "prev": 0}, "dist": {"recent": 0, "prev": 0},
            })
            d["last"] = max(d["last"], w["date"])
            if e["sets"]:
                has_weight = any("weight_kg" in s for s in e["sets"])
                for s in e["sets"]:
                    value = e1rm(s.get("load_kg", s.get("weight_kg")), s.get("reps")) if has_weight else s.get("reps")
                    if value:
                        d[period].append(value)
                d["unit"] = "kg (e1RM)" if has_weight else "Wdh."
            else:
                km, sec = e["endurance"].get("distance_km"), e["endurance"].get("duration_s")
                if km and sec:
                    d["dist"][period] += km
                    d["sec"][period] += sec

    trends = []
    for d in sorted(data.values(), key=lambda x: x["last"], reverse=True):
        if d["kind"] == "strength":
            recent = max(d["recent"], default=None)
            prev = max(d["prev"], default=None)
            change = percent_change(recent, prev)
        else:
            recent = d["sec"]["recent"] / d["dist"]["recent"] if d["dist"]["recent"] else None
            prev = d["sec"]["prev"] / d["dist"]["prev"] if d["dist"]["prev"] else None
            change = percent_change(prev, recent)  # Pace: kleiner = besser -> Richtung umdrehen
            d["unit"] = "/km"
        if recent is None:
            continue
        trends.append({"name": d["name"], "kind": d["kind"], "value": recent, "change": change, "unit": d["unit"]})
    return trends


def build_hints(workouts, trends, goals, today):
    """Einfache Regeln -> Hinweise, was du anpassen könntest."""
    hints = []

    # 1) Pause erkennen
    last = max((w["date"] for w in workouts), default=None)
    if last and (today - last).days >= 7:
        hints.append(f"Dein letztes Workout ist {(today - last).days} Tage her. Plane die nächste Einheit fix ein.")

    # 2) Hybrid-Balance der letzten 14 Tage
    recent = [w for w in workouts if w["date"] >= today - timedelta(days=13)]
    if recent:
        if not any(w["type_key"] == "strength" for w in recent):
            hints.append("In den letzten 2 Wochen kein Krafttraining. Eine Einheit hält deine Kraftwerte stabil.")
        if not any(w["type_key"] != "strength" for w in recent):
            hints.append("In den letzten 2 Wochen kein Ausdauertraining. Schon 1–2 lockere Einheiten erhalten die Grundausdauer.")

    # 3) Stagnation / Rückschritt je Übung
    for t in trends:
        if t["change"] is not None and t["change"] <= 0:
            if t["kind"] == "strength":
                hints.append(f"{t['name']}: kein Fortschritt zu den 4 Wochen davor ({t['change']:+d} %). "
                             "Versuch einen anderen Wiederholungsbereich, mehr Sätze oder eine leichtere Woche zur Erholung.")
            else:
                hints.append(f"{t['name']}: Pace nicht besser als in den 4 Wochen davor ({t['change']:+d} %). "
                             "Mehr lockere Umfänge oder eine gezielte Tempo-Einheit pro Woche können helfen.")

    # 4) Ziele mit knapper Zeit
    for g in goals:
        p, days = g.get("progress"), g.get("days_left")
        if p and not p["reached"] and days is not None and 0 <= days <= 28 and p["percent"] < 80:
            hints.append(f"Ziel „{g['title']}“: noch {days} Tage, erst {p['percent']} % erreicht. "
                         "Setz den Fokus der nächsten Einheiten darauf oder passe Zielwert/Datum an.")
    return hints


@app.route("/dashboard")
@login_required
def dashboard():
    client = current_client()
    #Noch kein Körpergewicht hinterlegt
    if not session.get("has_bodyweight"):
        if latest_bodyweight(client) is None:
            return redirect(url_for("profile", welcome=1))
        session["has_bodyweight"] = True

    today = local_today()
    workouts = load_all_workouts(client)

    # Wochen-Navigation: ?w=0 = diese Woche, ?w=1 = eine Woche zurück, ...

    this_monday = week_start(today)
    first_monday = week_start(min((w["date"] for w in workouts), default=today))
    max_back = max((this_monday - first_monday).days // 7, 0)
    back = min(max(request.args.get("w", 0, type=int), 0), max_back)
    sel_monday = this_monday - timedelta(weeks=back)
    sel_week = [w for w in workouts if sel_monday <= w["date"] < sel_monday + timedelta(days=7)]
    prev_week = [w for w in workouts if sel_monday - timedelta(days=7) <= w["date"] < sel_monday]

    goals = client.table("goals").select(GOAL_SELECT).eq("status", "active").execute().data
    goals = [enrich_goal(g, workouts) for g in goals]
    trends = exercise_trends(workouts, today)

    grid = week_grid(workouts, today)

    # Workouts pro Tag fürs Diagramm: Klick auf einen Tag -> Details
    day_workouts = {}
    for w in sorted(workouts, key=lambda w: (w["date"], w["start_time"] or "")):
        day_workouts.setdefault(w["date"].isoformat(), []).append({
            "url": url_for("workout_detail", workout_id=w["id"]),
            "type_key": w["type_key"],
            "type_label": w["type_label"],
            "time": w["start_time"],
            "names": " · ".join(e["name"] for e in w["entries"]) or "Keine Übungen",
        })

    return render_template(
        "dashboard.html",
        cur=totals(sel_week),
        prev=totals(prev_week),
        week={"back": back, "max_back": max_back, "kw": sel_monday.isocalendar()[1],
              "start": sel_monday, "end": sel_monday + timedelta(days=6)},
        grid=grid,
        day_workouts=day_workouts,
        trends=trends[:8],
        goals=goals,
        hints=build_hints(workouts, trends, goals, today),
        recent=sorted(workouts, key=lambda w: w["date"], reverse=True)[:3],
        active=next((w for w in workouts if w["status"] == "active"), None),
        training_days_month=len({w["date"] for w in workouts
                                if w["date"].year == today.year and w["date"].month == today.month}),
        month_name=MONTHS[today.month - 1],
    )



if __name__ == "__main__":
    app.run(debug=True)
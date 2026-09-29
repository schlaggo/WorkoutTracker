import os
import os
import time
from datetime import date

import jwt
from flask import Flask, render_template, request, redirect, session, url_for, flash
from dotenv import load_dotenv
from supabase import create_client

from lib.supabase_client import get_client, get_authenticated_client

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["FLASK_SECRET_KEY"]

# Cookie-Sessions absichern (auf Render läuft alles über HTTPS)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))


def current_client():
    token = session.get("access_token")
    refresh = session.get("refresh_token")
    if not token or not refresh:
        return None

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
            flash("Sitzung abgelaufen, bitte neu einloggen.", "error")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped


def to_float(value):
    value = (value or "").strip().replace(",", ".")
    return float(value) if value else None


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

        session["access_token"] = res.session.access_token
        session["refresh_token"] = res.session.refresh_token
        session["user_id"] = res.user.id
        session["email"] = res.user.email

        ensure_profile(get_authenticated_client(res.session.access_token), res.user)
        return redirect(url_for("dashboard"))

    return render_template("login.html")


# Logout und wieder auf Login anzeigen
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# Dashboard anzeigen mit Workouts und Goals
@app.route("/dashboard")
@login_required
def dashboard():
    client = current_client()

    workouts = (
        client.table("workouts")
        .select("id, date, notes, workout_types(label)")
        .order("date", desc=True)
        .limit(20)
        .execute()
    )

    goals = (
        client.table("goals")
        .select("id, target_value, target_date, status, metric_definitions(label, unit), exercises(name)")
        .eq("status", "active")
        .execute()
    )

    return render_template(
        "dashboard.html",
        workouts=workouts.data,
        goals=goals.data,
        email=session.get("email"),
    )


# Neues Workout erstellen
@app.route("/workouts/new", methods=["GET", "POST"])
@login_required
def new_workout():
    client = current_client()

    exercises = client.table("exercises").select("id, name, workout_type_id").execute()

    if request.method == "POST":
        workout_type = request.form["workout_type"]
        exercise_id = request.form.get("exercise_id")
        new_exercise_name = request.form.get("new_exercise_name", "").strip()
        notes = request.form.get("notes", "").strip()

        if not exercise_id and new_exercise_name:
            created = client.table("exercises").insert({
                "workout_type_id": int(workout_type),
                "name": new_exercise_name,
                "is_custom": True,
                "created_by": session["user_id"],
            }).execute()
            exercise_id = created.data[0]["id"]

        workout = client.table("workouts").insert({
            "user_id": session["user_id"],
            "workout_type_id": int(workout_type),
            "date": request.form.get("date") or str(date.today()),
            "notes": notes or None,
        }).execute()
        workout_id = workout.data[0]["id"]

        entry = client.table("workout_entries").insert({
            "workout_id": workout_id,
            "exercise_id": exercise_id,
            "order_index": 0,
        }).execute()
        entry_id = entry.data[0]["id"]

        if workout_type == "1":
            metric_keys = ["weight_kg", "reps"]
        else:
            metric_keys = ["distance_km", "duration_s", "elevation_m", "heart_rate_avg"]

        metrics = client.table("metric_definitions").select("id, key").in_("key", metric_keys).execute()
        metric_map = {m["key"]: m["id"] for m in metrics.data}

        rows = []
        if workout_type == "1":
            for i in range(1, 6):
                try:
                    weight = to_float(request.form.get(f"set_{i}_weight"))
                    reps = to_float(request.form.get(f"set_{i}_reps"))
                except ValueError:
                    flash(f"Satz {i}: bitte nur Zahlen eingeben.", "error")
                    return redirect(url_for("new_workout"))
                    
                if weight and reps:
                    rows.append({
                        "entry_id": entry_id,
                        "metric_definition_id": metric_map["weight_kg"],
                        "set_number": i,
                        "value": float(weight),
                    })
                    rows.append({
                        "entry_id": entry_id,
                        "metric_definition_id": metric_map["reps"],
                        "set_number": i,
                        "value": float(reps),
                    })
        else:
            field_to_key = {
                "distance_km": "distance_km",
                "duration_s": "duration_s",
                "elevation_m": "elevation_m",
                "heart_rate_avg": "heart_rate_avg",
            }
            for field, key in field_to_key.items():
                val = request.form.get(field)
                if val:
                    rows.append({
                        "entry_id": entry_id,
                        "metric_definition_id": metric_map[key],
                        "set_number": None,
                        "value": float(val),
                    })

        if rows:
            client.table("entry_metrics").insert(rows).execute()

        flash("Workout gespeichert.", "success")
        return redirect(url_for("dashboard"))

    return render_template("new_workout.html", exercises=exercises.data)


if __name__ == "__main__":
    app.run(debug=True)

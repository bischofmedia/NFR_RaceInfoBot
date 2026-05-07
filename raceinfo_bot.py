import discord
from discord.ext import tasks
import pymysql
import os
import random
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
CHANNEL_ID      = int(os.getenv("CHANNEL_ID"))
TEST_MODE       = os.getenv("TEST_MODE", "false").lower() == "true"
NFR_TEAM_IDS    = [8, 9, 10]  # NFR inkl. Sub-Teams
TZ              = pytz.timezone("Europe/Berlin")

DB_HOST         = os.getenv("DB_HOST")
DB_PORT         = int(os.getenv("DB_PORT", "3306"))
DB_USER         = os.getenv("DB_USER")
DB_PASS         = os.getenv("DB_PASS")
DB_NAME         = os.getenv("DB_NAME")

intents = discord.Intents.default()
client  = discord.Client(intents=intents)

# ── DB helper ─────────────────────────────────────────────────────────────────
def get_db():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASS, database=DB_NAME,
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor
    )

# ── Queries ───────────────────────────────────────────────────────────────────
def fetch_next_race(db, target_date):
    with db.cursor() as c:
        c.execute("""
            SELECT rc.*, t.name AS track_name_db
            FROM race_calendar rc
            LEFT JOIN tracks t ON rc.track_id = t.track_id
            WHERE rc.race_date = %s AND rc.is_pause = 0
        """, (target_date,))
        return c.fetchone()

def fetch_random_raced_track(db):
    with db.cursor() as c:
        c.execute("""
            SELECT rc.*, t.name AS track_name_db
            FROM race_calendar rc
            JOIN tracks t ON rc.track_id = t.track_id
            WHERE rc.is_pause = 0 AND rc.race_date < CURDATE()
            ORDER BY RAND() LIMIT 1
        """)
        return c.fetchone()

def fetch_track_history(db, track_id):
    with db.cursor() as c:
        c.execute("""
            SELECT COUNT(*) AS race_count,
                   MAX(race_date) AS last_race_date,
                   MAX(race_id) AS last_race_id
            FROM races
            WHERE track_id = %s AND race_date >= '2022-03-04'
        """, (track_id,))
        history = c.fetchone()
        if not history or not history["race_count"]:
            return None

        last_race_id = history["last_race_id"]

        c.execute("""
            SELECT d.psn_name, v.name AS vehicle_name
            FROM race_results rr
            JOIN drivers d ON rr.driver_id = d.driver_id
            LEFT JOIN vehicles v ON rr.vehicle_id = v.vehicle_id
            WHERE rr.race_id = %s AND rr.finish_pos_overall = 1
            LIMIT 1
        """, (last_race_id,))
        winner = c.fetchone()

        c.execute("""
            SELECT d.psn_name, r.fastest_lap_time
            FROM races r
            JOIN drivers d ON r.fastest_lap_driver_id = d.driver_id
            WHERE r.race_id = %s AND r.fastest_lap_time IS NOT NULL
            LIMIT 1
        """, (last_race_id,))
        fastest = c.fetchone()

        history["winner"]  = winner
        history["fastest"] = fastest
        return history

def fetch_nfr_drivers(db):
    """Aktive NFR-Fahrer anhand der letzten 20 Rennen aus race_results."""
    with db.cursor() as c:
        team_placeholders = ",".join(["%s"] * len(NFR_TEAM_IDS))
        c.execute("""
            CREATE TEMPORARY TABLE IF NOT EXISTS _last20 AS
            SELECT race_id FROM races ORDER BY race_date DESC LIMIT 20
        """)
        c.execute(f"""
            SELECT DISTINCT d.driver_id, d.psn_name
            FROM drivers d
            JOIN race_results rr ON d.driver_id = rr.driver_id
            WHERE rr.team_id IN ({team_placeholders})
            AND rr.race_id IN (SELECT race_id FROM _last20)
            AND d.is_active = 1
            ORDER BY d.psn_name
        """, NFR_TEAM_IDS)
        drivers = c.fetchall()
        c.execute("DROP TEMPORARY TABLE IF EXISTS _last20")
        return drivers

def fetch_nfr_results(db, track_id, nfr_drivers):
    """Letzte Ergebnisse der NFR-Fahrer auf dieser Strecke."""
    if not nfr_drivers:
        return []

    driver_ids = [d["driver_id"] for d in nfr_drivers]

    with db.cursor() as c:
        c.execute("""
            SELECT r.race_id, r.race_date, s.name AS season_name
            FROM races r
            JOIN seasons s ON r.season_id = s.season_id
            WHERE r.track_id = %s AND r.race_date >= '2022-03-04'
            ORDER BY r.race_date DESC
        """, (track_id,))
        races_on_track = c.fetchall()

        if not races_on_track:
            return []

        results_by_driver = {}
        no_result_drivers  = set()

        for race in races_on_track:
            remaining = [d for d in driver_ids
                         if d not in results_by_driver
                         and d not in no_result_drivers]
            if not remaining:
                break

            ph = ",".join(["%s"] * len(remaining))
            c.execute(f"""
                SELECT rr.driver_id, d.psn_name,
                       g.grid_label, rr.start_pos_grid,
                       rr.finish_pos_overall, v.name AS vehicle_name,
                       r.race_date, s.name AS season_name, r.race_id
                FROM race_results rr
                JOIN drivers d ON rr.driver_id = d.driver_id
                JOIN races r ON rr.race_id = r.race_id
                JOIN seasons s ON r.season_id = s.season_id
                JOIN grids g ON rr.grid_id = g.grid_id
                LEFT JOIN vehicles v ON rr.vehicle_id = v.vehicle_id
                WHERE rr.race_id = %s AND rr.driver_id IN ({ph})
            """, (race["race_id"], *remaining))
            for row in c.fetchall():
                results_by_driver[row["driver_id"]] = row

        # Fahrern ohne jedes Ergebnis prüfen
        for d in nfr_drivers:
            if d["driver_id"] not in results_by_driver:
                no_result_drivers.add(d["driver_id"])

        # Nach Rennen gruppieren
        races_dict = {}
        for row in results_by_driver.values():
            rid = row["race_id"]
            if rid not in races_dict:
                races_dict[rid] = {
                    "race_id":     rid,
                    "race_date":   row["race_date"],
                    "season_name": row["season_name"],
                    "results":     []
                }
            races_dict[rid]["results"].append(row)

        return sorted(races_dict.values(),
                      key=lambda x: x["race_date"], reverse=True)

def fetch_vehicle_stats(db, track_id):
    with db.cursor() as c:
        c.execute("""
            SELECT v.name AS vehicle_name, COUNT(rr.result_id) AS usage_count
            FROM race_results rr
            JOIN races r ON rr.race_id = r.race_id
            JOIN vehicles v ON rr.vehicle_id = v.vehicle_id
            WHERE r.track_id = %s AND r.race_date >= '2022-03-04'
            GROUP BY rr.vehicle_id, v.name
            ORDER BY usage_count DESC
            LIMIT 3
        """, (track_id,))
        most_used = c.fetchall()

        c.execute("""
            SELECT vehicle_id, vehicle_name, points_avg_weighted
            FROM v_vehicle_track_performance
            WHERE track_id = %s
            ORDER BY points_avg_weighted DESC
            LIMIT 5
        """, (track_id,))
        top5 = c.fetchall()

        used_in_top5 = {row["vehicle_id"] for row in top5}
        used_as_alt  = set()
        alternatives = {}

        for car in top5:
            vid = car["vehicle_id"]
            c.execute("""
                SELECT
                    CASE WHEN vehicle_id_a = %s THEN vehicle_id_b ELSE vehicle_id_a END AS alt_id,
                    CASE WHEN vehicle_id_a = %s THEN vehicle_name_b ELSE vehicle_name_a END AS alt_name,
                    avg_diff
                FROM v_vehicle_similarity
                WHERE vehicle_id_a = %s OR vehicle_id_b = %s
                ORDER BY avg_diff ASC
            """, (vid, vid, vid, vid))
            for alt in c.fetchall():
                if (alt["alt_id"] not in used_in_top5
                        and alt["alt_id"] not in used_as_alt):
                    used_as_alt.add(alt["alt_id"])
                    alternatives[vid] = alt
                    break

        # Mindestens 3 Alternativen sicherstellen
        if len(alternatives) < 3:
            c.execute("""
                SELECT
                    CASE WHEN vehicle_id_a IN %s THEN vehicle_id_b ELSE vehicle_id_a END AS alt_id,
                    CASE WHEN vehicle_id_a IN %s THEN vehicle_name_b ELSE vehicle_name_a END AS alt_name,
                    avg_diff
                FROM v_vehicle_similarity
                WHERE vehicle_id_a IN %s OR vehicle_id_b IN %s
                ORDER BY avg_diff ASC
            """, (tuple(used_in_top5), tuple(used_in_top5),
                  tuple(used_in_top5), tuple(used_in_top5)))
            for alt in c.fetchall():
                if len(alternatives) >= 3:
                    break
                if (alt["alt_id"] not in used_in_top5
                        and alt["alt_id"] not in used_as_alt):
                    used_as_alt.add(alt["alt_id"])
                    # Dummy-Schlüssel für zusätzliche Alternativen
                    alternatives[f"extra_{alt['alt_id']}"] = alt

        # Neue Autos (< 1 Jahr im Spiel) die noch nicht genannt wurden
        all_mentioned = used_in_top5 | used_as_alt
        c.execute("""
            SELECT v.vehicle_id, v.name AS vehicle_name, v.gt_added
            FROM vehicles v
            WHERE v.in_gt7 = 1
            AND v.gt_added >= DATE_SUB(CURDATE(), INTERVAL 1 YEAR)
            AND v.vehicle_id NOT IN %s
        """, (tuple(all_mentioned) if all_mentioned else (0,),))
        new_cars_raw = c.fetchall()
        print(f"Neue Autos gefunden: {len(new_cars_raw)}: {[c['vehicle_name'] for c in new_cars_raw]}")

        new_cars = []
        for car in new_cars_raw:
            vid = car["vehicle_id"]
            if not top5:
                break
            top5_ids = tuple(r["vehicle_id"] for r in top5)
            c.execute("""
                SELECT
                    avg_diff,
                    CASE WHEN vehicle_id_a = %s THEN -avg_delta ELSE avg_delta END AS delta
                FROM v_vehicle_similarity
                WHERE (vehicle_id_a = %s OR vehicle_id_b = %s)
                AND (vehicle_id_a IN %s OR vehicle_id_b IN %s)
                ORDER BY avg_diff ASC
                LIMIT 1
            """, (vid, vid, vid, top5_ids, top5_ids))
            sim = c.fetchone()
            if sim:
                car["avg_diff"]  = sim["avg_diff"]
                car["avg_delta"] = sim["delta"]
                new_cars.append(car)

        return most_used, top5, alternatives, new_cars

# ── Message builder ───────────────────────────────────────────────────────────
def build_message(race, track_history, nfr_drivers, nfr_races,
                  most_used, top5, alternatives, new_cars):
    is_rain      = race["weather_code"] and race["weather_code"].upper().startswith("R")
    weather_emoji = "🌧️" if is_rain else "☀️"

    time_label = race["time_of_day"] or ""
    if "/" in time_label:
        time_label = time_label.split("/")[0].strip()

    track_name = race["track_name"] or race["track_name_db"] or "Unbekannte Strecke"

    lines = []
    lines.append(f"## 🏁 Rennen auf {track_name}")
    lines.append("")
    lines.append("Hallo Fahrer,")
    lines.append(
        f"die Anmeldung für das Rennen auf **{track_name}** ist geöffnet. "
        f"Wir fahren **{race['laps']} Runden**, "
        f"die Settings sind {weather_emoji} **{time_label}** / **{race['weather_code']}**."
    )
    lines.append("")

    # ── Streckenhistorie ──
    if track_history and track_history["race_count"]:
        count     = track_history["race_count"]
        last_date = track_history["last_race_date"].strftime("%d.%m.%Y")
        winner    = track_history["winner"]
        fastest   = track_history["fastest"]

        lines.append(
            f"Die RTC ist auf dieser Strecke **{count}×** gefahren (GT7). "
            f"Das letzte Rennen war am **{last_date}**."
        )
        if winner and fastest:
            if winner["psn_name"] == fastest["psn_name"]:
                lines.append(
                    f"Gewonnen hat **{winner['psn_name']}** im {winner['vehicle_name']} — "
                    f"er fuhr dabei auch die schnellste Runde ({fastest['fastest_lap_time']})."
                )
            else:
                lines.append(
                    f"Gewonnen hat **{winner['psn_name']}** im {winner['vehicle_name']}. "
                    f"Die schnellste Runde fuhr **{fastest['psn_name']}** "
                    f"({fastest['fastest_lap_time']})."
                )
        elif winner:
            lines.append(
                f"Gewonnen hat **{winner['psn_name']}** im {winner['vehicle_name']}."
            )
    else:
        lines.append(
            "Diese Strecke wurde in GT7 noch nicht gefahren — "
            "es gibt noch keine Statistiken."
        )
    lines.append("")

    # ── NFR-Ergebnisse ──
    if nfr_races:
        lines.append("**Unsere letzten Ergebnisse:**")
        for race_block in nfr_races:
            date_str = race_block["race_date"].strftime("%d.%m.%Y")
            lines.append("")
            lines.append(f"📅 **{race_block['season_name']} — {date_str}**")
            lines.append("```")
            lines.append(f"{'Fahrer':<22} {'Grid':<12} {'Grid-P':>6} {'Ges.':>5}  Fahrzeug")
            lines.append("─" * 68)
            for r in sorted(race_block["results"],
                            key=lambda x: x["finish_pos_overall"] or 99):
                pos_overall = str(r["finish_pos_overall"]) if r["finish_pos_overall"] else "–"
                start_pos   = str(r["start_pos_grid"])     if r["start_pos_grid"]     else "–"
                vehicle     = r["vehicle_name"] or "–"
                grid        = r["grid_label"]   or "–"
                lines.append(
                    f"{r['psn_name']:<22} {grid:<12} {start_pos:>6} {pos_overall:>5}  {vehicle}"
                )
            lines.append("```")
    else:
        lines.append(
            "Für unsere aktiven Fahrer gibt es auf dieser Strecke noch keine Ergebnisse."
        )

    # ── Fahrzeugempfehlung ──
    lines.append("**🚗 Fahrzeugempfehlung:**")

    if most_used:
        used_str = ", ".join(
            [f"**{r['vehicle_name']}** ({r['usage_count']}×)" for r in most_used]
        )
        lines.append(f"Am meisten genutzt wurden auf dieser Strecke: {used_str}.")

    if top5:
        top5_str = ", ".join([f"**{r['vehicle_name']}**" for r in top5])
        lines.append(f"Die besten Ergebnisse erzielten: {top5_str}.")

        alt_names = [alt["alt_name"] for alt in alternatives.values()][:3]
        if alt_names:
            lines.append(
                f"Alternativ könnt ihr auch "
                f"{', '.join(f'**{n}**' for n in alt_names)} in Betracht ziehen."
            )
    else:
        lines.append(
            "Für diese Strecke liegen noch keine Performancedaten vor."
        )


    # ── Neue Fahrzeuge ──
    if new_cars:
        good = [c for c in new_cars if c.get("avg_delta", 0) >= 0]
        bad  = [c for c in new_cars if c.get("avg_delta", 0) < 0]
        no_data = [c for c in new_cars if "avg_delta" not in c]

        parts = []
        if good:
            names = ", ".join(f"**{c['vehicle_name']}**" for c in good)
            parts.append(f"{names} könnte{'n' if len(good) > 1 else ''} auf dieser Strecke gut funktionieren")
        if bad:
            names = ", ".join(f"**{c['vehicle_name']}**" for c in bad)
            parts.append(f"{names} ist{'sind' if len(bad) > 1 else ''} für diese Strecke eher nicht zu empfehlen")
        if no_data:
            names = ", ".join(f"**{c['vehicle_name']}**" for c in no_data)
            parts.append(f"für {names} gibt es noch keine ausreichenden Vergleichsdaten")

        if parts:
            lines.append("")
            lines.append(
                "🆕 **Neuere Fahrzeuge (< 1 Jahr im Spiel):** "
                + " — ".join(parts) + "."
            )

    if is_rain:
        lines.append("")
        lines.append(
            "⚠️ **Achtung:** Laut Wettercode könnte es regnen. "
            "Die Fahrzeugempfehlung ist deshalb mit Vorbehalt zu betrachten."
        )

    return "\n".join(lines)

# ── Main logic ────────────────────────────────────────────────────────────────
async def post_race_info():
    channel = client.get_channel(CHANNEL_ID)
    if not channel:
        print(f"Channel {CHANNEL_ID} nicht gefunden.")
        return

    db = get_db()
    try:
        today      = datetime.now(TZ).date()
        days_ahead = (7 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        next_monday = today + timedelta(days=days_ahead)

        if TEST_MODE:
            race = fetch_random_raced_track(db)
            if not race:
                await channel.send("TEST_MODE: Keine gefahrenen Strecken gefunden.")
                return
        else:
            race = fetch_next_race(db, next_monday)
            if not race:
                print(f"Kein Rennen am {next_monday}.")
                return

        track_id = race["track_id"]

        track_history            = fetch_track_history(db, track_id)
        nfr_drivers              = fetch_nfr_drivers(db)
        nfr_races                = fetch_nfr_results(db, track_id, nfr_drivers)
        most_used, top5, alts, new_cars = fetch_vehicle_stats(db, track_id)

        msg = build_message(race, track_history, nfr_drivers, nfr_races,
                            most_used, top5, alts, new_cars)

        # Discord-Limit: 2000 Zeichen pro Nachricht
        while len(msg) > 1900:
            split_at = msg.rfind("\n", 0, 1900)
            await channel.send(msg[:split_at])
            msg = msg[split_at:].lstrip()
        await channel.send(msg)

    finally:
        db.close()

# ── Scheduler ─────────────────────────────────────────────────────────────────
@tasks.loop(minutes=1)
async def scheduler():
    now = datetime.now(TZ)
    if now.weekday() == 1 and now.hour == 10 and now.minute == 0:
        await post_race_info()

@client.event
async def on_ready():
    print(f"NFR_RaceInfoBot eingeloggt als {client.user}")
    scheduler.start()
    if TEST_MODE:
        print("TEST_MODE aktiv — poste sofort.")
        await post_race_info()

client.run(DISCORD_TOKEN)

import requests
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
import math
import re
import json
import os
import statistics
from flask import Flask, jsonify, send_file, request

# Hide Flask logs
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

API_URL = "http://103.243.173.194:7188/tiles/players.json"

# === Regions will be loaded from regions.json ===
regions = []  # List of {"name": "Region Name", "bounds": [xmin, xmax, zmin, zmax]}
regions_lock = threading.Lock()

# Track players
last_positions = {}   # {player: (x, z, timestamp)}
seen_players = set()
main_cache = {}
# Track region entries {player: {region_id: entry_time}}
region_entries = {}
data_lock = threading.Lock()  # Lock for player data dictionaries

# Track plane state
plane_state = {}          # {player: True/False}
plane_last_change = {}    # {player: datetime}
plane_entry_time = {}     # {player: datetime}
plane_entry_pos = {}      # {player: (x, y, z)} -> starting position of flight (with Y)
plane_descent_samples = {} # {player: [dy_per_sec, ...]} -> track descent rate
plane_speed_samples = {}   # {player: [speed, ...]} -> track horizontal speed for fireworks detection
plane_altitude_gain = {}   # {player: max_altitude_gain} -> detect if altitude increased (fireworks indicator)

# Track logins
login_times = {}          # {player: datetime}
login_logged = {}         # {player: bool} -> has this login been logged (>=60s online)?

# Track health/armor for death and totem detection
last_health = {}          # {player: int}
last_armor = {}           # {player: int}
totem_state = {}          # {player: bool} -> True if health at 0 (potential totem state)
last_world = {}           # {player: str} -> last known world
died_players = {}         # {player: bool} -> True if health was 0 (for respawn detection)
last_logout_state = {}    # {player: {"x": x, "z": z, "health": health, "armor": armor, "timestamp": timestamp}}
health_zero_pos = {}      # {player: (x, z)} -> position when health hit 0 (for teleport detection)
death_times = {}          # {player: datetime} -> time of death (for respawn detection)
dead_players_display = {} # {player: {"x": x, "z": z, "world": world, "health": 0, "armor": armor, "death_time": datetime}} -> for 20sec display

# Player count tracking
current_count = None
count_start_time = None

# === File paths ===
LOGS_DIR = "logs"
DATA_DIR = "data"
COUNT_FILE     = os.path.join(LOGS_DIR, "player_count.txt")
DEATHS_FILE    = os.path.join(LOGS_DIR, "deaths_log.txt")
LOGINS_FILE    = os.path.join(LOGS_DIR, "logins_logouts.txt")
REGIONS_FILE   = os.path.join(LOGS_DIR, "region_detections.txt")
PLANES_FILE    = os.path.join(LOGS_DIR, "planes_log.txt")
MAIN_LOG_FILE  = os.path.join(LOGS_DIR, "main.txt")
CACHE_FILE     = os.path.join(DATA_DIR, "main_cache.json")
REGIONS_JSON   = os.path.join(DATA_DIR, "regions.json")

# Ensure directories exist
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Load regions from JSON on startup
def load_regions():
    """Load regions from regions.json."""
    global regions
    if os.path.exists(REGIONS_JSON):
        try:
            with open(REGIONS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
                with regions_lock:
                    regions = data if isinstance(data, list) else []
                print(f"Loaded {len(regions)} region(s) from {REGIONS_JSON}")
        except json.JSONDecodeError:
            print(f"regions.json is invalid, starting with empty regions")
            regions = []
    else:
        regions = []

def save_regions_to_file():
    """Save regions to regions.json."""
    with regions_lock:
        with open(REGIONS_JSON, "w", encoding="utf-8") as f:
            json.dump(regions, f, indent=2)

# Track logs (daily headers)
last_log_dates = {
    LOGINS_FILE: None,
    PLANES_FILE: None,
    REGIONS_FILE: None,
    COUNT_FILE: None,
    DEATHS_FILE: None,
}
# Keep separate latest logs per player per category
last_main_log_per_player = {"login": {}, "plane": {}}

# Error tracking
last_error_message = None
error_start_time = None

# GMT+8 timezone
GMT8 = timezone(timedelta(hours=8))

# Thresholds
MIN_SESSION_SECONDS = 0       # minimum session time to log
ELYTRA_MIN_DURATION = 3       # minimum flight seconds (doc recommendation #2)
ELYTRA_MIN_DISTANCE = 50      # minimum flight distance in blocks (doc recommendation #1)
ENTER_THRESHOLD = 14.0        # horizontal speed to trigger elytra entry (matches stall speed 14.4 m/s)
EXIT_THRESHOLD = 2.0          # horizontal speed to trigger elytra exit
ELYTRA_MIN_DESCENT = 0.01     # minimum downward velocity (blocks/sec)
ELYTRA_MAX_DESCENT = 10.0     # maximum downward velocity — raised for steep divers (wiki: up to 78 blk/s steep)
ELYTRA_MIN_ARMOR = 9          # kept for reference, not used in detection
FIREWORKS_SPEED_THRESHOLD = 25.0   # speed spike for fireworks (conservative below 33.5 wiki value)
FIREWORKS_ALTITUDE_GAIN = 2.0     # altitude gain to flag fireworks
ENTER_DELAY = 1.0             # seconds before confirming entry
EXIT_DELAY = 1.5              # seconds before confirming exit (was 2.0, doc recommendation #3)
SQUAREMAP_UPDATE_RATE = 1.0   # squaremap updates player positions every 1 second

def timestamp() -> str:
    return datetime.now(GMT8).strftime("[%Y-%m-%d %I:%M:%S %p GMT+8]")

def save_cache():
    """Save the latest player states to main_cache.json."""
    global last_main_log_per_player
    cache_data = {
        "logins": last_main_log_per_player.get("login", {}),
        "planes": last_main_log_per_player.get("plane", {}),
        "player_count": last_main_log_per_player.get("count", {}).get("latest", "")
    }
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, indent=4)

def load_cache():
    """Load cached player states from main_cache.json."""
    global last_main_log_per_player
    if not os.path.exists(CACHE_FILE):
        return
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return
        cache_data = json.loads(content)
    last_main_log_per_player["login"] = cache_data.get("logins", {})
    last_main_log_per_player["plane"] = cache_data.get("planes", {})
    last_main_log_per_player["count"] = {"latest": cache_data.get("player_count", "")}

def log_event(message: str, file: str):
    """Save events to file and print them with timestamp, with daily headers for selected logs."""
    global last_log_dates
    now = datetime.now(GMT8)
    current_date = now.date()
    log_line = f"{now.strftime('[%Y-%m-%d %I:%M:%S %p GMT+8]')} {message}"
    with open(file, "a", encoding="utf-8") as f:
        if file in last_log_dates:
            if last_log_dates[file] != current_date:
                header = now.strftime("---------%B %d %Y---------")
                f.write(header + "\n")
                last_log_dates[file] = current_date
        f.write(log_line + "\n")
    print(log_line)

def log_main(category: str, player: str, message: str):
    """Record categorized events into main log file (keeps separate latest per category)."""
    now = datetime.now(GMT8)
    cat = category.lower()
    if cat not in last_main_log_per_player:
        last_main_log_per_player[cat] = {}
    log_line = f"{now.strftime('[%Y-%m-%d %I:%M:%S %p GMT+8]')} {message}"
    if cat == "count":
        last_main_log_per_player[cat]["latest"] = log_line
    else:
        last_main_log_per_player[cat][player] = log_line
    with open(MAIN_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_line + "\n")

def cleanup_main_log():
    """Clear main log every 1s but keep the latest login, plane, and player count logs."""
    global last_main_log_per_player
    load_cache()
    while True:
        time.sleep(1)
        with open(MAIN_LOG_FILE, "w", encoding="utf-8") as f:
            now = datetime.now(GMT8)
            f.write(now.strftime("===== Main Summary (refreshed %Y-%m-%d %I:%M:%S %p GMT+8) =====\n"))
            f.write("\n-- Latest Login/Logout per Player --\n")
            for log in last_main_log_per_player.get("login", {}).values():
                # Add ✅ emoji if it's a login, ❌ emoji if it's a logout
                if "logged in" in log:
                    log = log.replace("Login ", "✅")
                elif "logged out" in log:
                    log = log.replace("Logout ", "❌")
                f.write(log + "\n")
            f.write("\n-- Latest Plane State per Player --\n")
            for log in last_main_log_per_player.get("plane", {}).values():
                # Add 🛩️ emoji for elytra
                log = log.replace("Elytra ", "🛩️")
                f.write(log + "\n")
            f.write("\n-- Latest Player Count --\n")
            last_count = last_main_log_per_player.get("count", {}).get("latest")
            if last_count:
                f.write(last_count + "\n")
        save_cache()

def format_duration(seconds: int) -> str:
    """Convert seconds into H:M:S format."""
    return str(timedelta(seconds=seconds))

def get_closest_alive_players(dead_player_name, dead_x, dead_z, dead_world, current_players):
    """Find up to 2 closest alive players in the same world."""
    closest = []
    for player in current_players:
        name = player.get("name")
        if name == dead_player_name or name not in last_positions:
            continue
        # Only consider alive players (health > 0)
        if last_health.get(name, 20) <= 0:
            continue
        # Only consider players in same world
        if last_world.get(name) != dead_world:
            continue
        x, z = player.get("x"), player.get("z")
        if x is None or z is None:
            continue
        dist = math.sqrt((x - dead_x) ** 2 + (z - dead_z) ** 2)
        closest.append((dist, name))
    
    closest.sort(key=lambda p: p[0])
    return closest[:2]

def update_player_count(players: list):
    """Log start/end times and durations ONLY when count is 0 to 3."""
    global current_count, count_start_time
    now = datetime.now(GMT8)
    players_len = len(players)
    players_str = ", ".join(players) if players else "None"
    if current_count is None:
        current_count = players_len
        count_start_time = now
        if 0 <= players_len <= 3:
            log_event(f"Player count is now {players_len} (started) | Players: {players_str}", COUNT_FILE)
        return
    prev_in_range = 0 <= current_count <= 3
    now_in_range = 0 <= players_len <= 3
    if now_in_range != prev_in_range:
        duration = int((now - count_start_time).total_seconds())
        if prev_in_range:
            log_event(f"Player count {current_count} ended after {format_duration(duration)}", COUNT_FILE)
        current_count = players_len
        count_start_time = now
        if now_in_range:
            log_event(f"Player count is now {players_len} (started) | Players: {players_str}", COUNT_FILE)
    else:
        current_count = players_len
        msg = f"[{datetime.now(GMT8).strftime('%Y-%m-%d %I:%M:%S %p GMT+8')}] Player count is now {players_len}"
        last_main_log_per_player["count"] = {"latest": msg}
        save_cache()

def cleanup_dead_players():
    """Remove dead players from display if 20 seconds have passed."""
    now = datetime.now(GMT8)
    for name in list(dead_players_display.keys()):
        death_time = dead_players_display[name]["death_time"]
        if (now - death_time).total_seconds() >= 20:
            dead_players_display.pop(name, None)

def process_players(players):
    """Handle login, logout, region, and plane logic."""
    global seen_players, last_positions, region_entries
    now = datetime.now(GMT8)
    
    # Cleanup dead players after 20 seconds
    cleanup_dead_players()

    player_names = [p["name"] for p in players if "name" in p]
    update_player_count(player_names)
    current_players = set()
    current_regions = {}

    with data_lock:
        for player in players:
            name, x, z, y = player.get("name"), player.get("x"), player.get("z"), player.get("y")
            if not name or x is None or z is None or y is None:
                continue

            current_players.add(name)
            current_regions[name] = []

            # --- Health/armor tracking and totem detection ---
            health = player.get("health")
            armor = player.get("armor")
            if health is not None:
                prev_health = last_health.get(name)
                if prev_health is not None:
                    # Totem detection: health was 0, now recovered to 3-10 (totem gives 1 heart then regens)
                    # Exclude respawn (0 -> 20 instantly) by capping at health <= 10
                    if prev_health == 0 and 3 <= health <= 10:
                        zero_pos = health_zero_pos.get(name, (x, z))
                        # Check if position changed significantly (large teleport = respawn/death, not totem)
                        dist = math.sqrt((x - zero_pos[0]) ** 2 + (z - zero_pos[1]) ** 2) if zero_pos else 0
                        if dist <= 5:  # No teleport = Totem
                            msg = f"Totem {name} used a TOTEM at X={x}, Z={z} (health: {prev_health}->{health}, armor: {armor})"
                            log_event(msg, DEATHS_FILE)
                            log_main("death", name, msg)
                        # If dist > 5 it's ambiguous — skip logging, logout handler will catch the death
                        died_players[name] = False
                        health_zero_pos.pop(name, None)
                    # Track position when health reaches 0
                    if health == 0 and prev_health != 0:
                        health_zero_pos[name] = (x, z)
                        died_players[name] = True
                    elif health > 0 and died_players.get(name, False):
                        died_players[name] = False
                        health_zero_pos.pop(name, None)
                last_health[name] = health

            if armor is not None:
                last_armor[name] = armor

            world = player.get("world", "")
            if world:
                last_world[name] = world

            # --- Speed & plane detection ---
            if name in last_positions:
                old_x, old_z, old_y, old_time = last_positions[name]
                dt = (now - old_time).total_seconds()
                
                # Skip stale polls — squaremap updates every 1s, we poll every 0.5s
                # If position hasn't changed, this is a stale read: skip to avoid false exits
                position_changed = (x != old_x or z != old_z or y != old_y)
                
                if dt > 0 and position_changed:
                    horizontal_distance = math.sqrt((x - old_x) ** 2 + (z - old_z) ** 2)
                    # Normalize speed to per-second using actual dt
                    # But clamp dt to SQUAREMAP_UPDATE_RATE to avoid underestimating speed
                    # when two consecutive squaremap ticks are captured in one poll
                    effective_dt = max(dt, SQUAREMAP_UPDATE_RATE)
                    horizontal_speed = horizontal_distance / effective_dt
                    dy = y - old_y
                    vertical_speed = dy / effective_dt

                    previously_in_plane = plane_state.get(name, False)
                    if name not in plane_last_change:
                        plane_last_change[name] = now
                    last_toggle = plane_last_change[name]

                    is_descending = vertical_speed < -ELYTRA_MIN_DESCENT
                    is_teleport = horizontal_speed > 100

                    if not previously_in_plane and horizontal_speed >= ENTER_THRESHOLD and not is_teleport:
                        if (now - last_toggle).total_seconds() >= ENTER_DELAY:
                            if is_descending:
                                plane_state[name] = True
                                plane_last_change[name] = now
                                plane_entry_time[name] = now
                                plane_entry_pos[name] = (x, y, z)
                                plane_descent_samples[name] = [vertical_speed]
                                plane_speed_samples[name] = [horizontal_speed]
                                plane_altitude_gain[name] = 0.0

                    elif previously_in_plane and is_teleport:
                        # Teleport mid-flight — cancel silently
                        plane_state[name] = False
                        plane_last_change[name] = now
                        plane_entry_time.pop(name, None)
                        plane_entry_pos.pop(name, None)
                        plane_descent_samples.pop(name, None)
                        plane_speed_samples.pop(name, None)
                        plane_altitude_gain.pop(name, None)

                    elif previously_in_plane and horizontal_speed <= EXIT_THRESHOLD:
                        if (now - last_toggle).total_seconds() >= EXIT_DELAY:
                            plane_state[name] = False
                            plane_last_change[name] = now
                            entry_time = plane_entry_time.pop(name, None)
                            entry_pos = plane_entry_pos.pop(name, None)
                            descent_samples = plane_descent_samples.pop(name, [])
                            speed_samples = plane_speed_samples.pop(name, [])
                            altitude_gain = plane_altitude_gain.pop(name, 0.0)

                            if entry_time:
                                duration = int((now - entry_time).total_seconds())
                                distance = math.sqrt((x - entry_pos[0]) ** 2 + (z - entry_pos[2]) ** 2) if entry_pos else 0

                                # Doc recommendation #1: min distance filter
                                # Doc recommendation #2: min duration filter
                                if duration >= ELYTRA_MIN_DURATION and distance >= ELYTRA_MIN_DISTANCE:
                                    # Doc recommendation #4: use median instead of average for outlier resistance
                                    median_descent = statistics.median(descent_samples) if descent_samples else 0
                                    is_valid_elytra = (median_descent < -ELYTRA_MIN_DESCENT and
                                                       median_descent > -ELYTRA_MAX_DESCENT)

                                    if is_valid_elytra:
                                        max_speed = max(speed_samples) if speed_samples else 0
                                        has_speed_spike = max_speed > FIREWORKS_SPEED_THRESHOLD
                                        has_altitude_gain = altitude_gain > FIREWORKS_ALTITUDE_GAIN
                                        used_fireworks = has_speed_spike or has_altitude_gain

                                        avg_speed = distance / duration if duration > 0 else 0
                                        speed_str = f" {avg_speed:.0f}blk/s" if avg_speed > 0 else ""
                                        from_str = f"{int(entry_pos[0])},{int(entry_pos[2])}" if entry_pos else ""
                                        to_str = f"{x},{z}"
                                        coords_str = f" {from_str} \u2192 {to_str}" if entry_pos else ""
                                        dist_str = f" {int(distance)} blocks"
                                        fireworks_str = ""
                                        if used_fireworks:
                                            fireworks_str = " (Fireworks)" if has_speed_spike else " (Fireworks - altitude)"

                                        msg = f"Elytra {name}{fireworks_str} time: {format_duration(duration)}{speed_str}{dist_str}{coords_str}"
                                        log_event(msg, PLANES_FILE)
                                        log_main("plane", name, msg)

                    elif previously_in_plane:
                        # Accumulate samples during flight (only on real position updates)
                        if name in plane_descent_samples and len(plane_descent_samples[name]) < 100:
                            plane_descent_samples[name].append(vertical_speed)
                        if name in plane_speed_samples and len(plane_speed_samples[name]) < 100:
                            plane_speed_samples[name].append(horizontal_speed)
                        # Track max altitude gain for fireworks detection
                        if name in plane_altitude_gain and name in plane_entry_pos:
                            entry_y = plane_entry_pos[name][1]
                            altitude_gain_now = y - entry_y
                            if altitude_gain_now > plane_altitude_gain[name]:
                                plane_altitude_gain[name] = altitude_gain_now

            last_positions[name] = (x, z, y, now)

            if name not in seen_players:
                login_times[name] = now
                login_logged[name] = False
                # Remove logout state when player logs back in (respawn or fresh login)
                last_logout_state.pop(name, None)
                # Remove from dead display if respawning
                dead_players_display.pop(name, None)
            elif name in login_times and not login_logged[name]:
                duration = int((now - login_times[name]).total_seconds())
                if duration >= MIN_SESSION_SECONDS:
                    # Check if player respawned (died within 20 seconds)
                    is_respawn = False
                    if name in death_times:
                        time_since_death = int((now - death_times[name]).total_seconds())
                        if time_since_death <= 20:
                            is_respawn = True
                            death_times.pop(name, None)  # Clear death time after respawn

                    # Only log login if it's not a respawn
                    if not is_respawn:
                        msg = f"Login {name} logged in at X={x}, Z={z}"
                        log_event(msg, LOGINS_FILE)
                        log_main("login", name, msg)
                    login_logged[name] = True

            # --- Region detection ---
            with regions_lock:
                for idx, region in enumerate(regions):
                    region_name = region.get("name", f"Region {idx+1}")
                    bounds = region.get("bounds", [])
                    if len(bounds) != 4:
                        continue
                    min_x, max_x, min_z, max_z = bounds
                    if min_x <= x <= max_x and min_z <= z <= max_z:
                        region_id = f"{idx}_{region_name}"
                        current_regions[name].append(region_id)
                        if name not in region_entries:
                            region_entries[name] = {}
                        if region_id not in region_entries[name]:
                            region_entries[name][region_id] = now
                            log_event(f"Region {name} ENTERED {region_name} at X={x}, Z={z}", REGIONS_FILE)

        # Logout / death detection
        logged_out = seen_players - current_players
        for name in logged_out:
            plane_last_change.pop(name, None)
            health_zero_pos.pop(name, None)
            death_times.pop(name, None)

            last_pos = last_positions.get(name)
            x, z = (last_pos[0], last_pos[1]) if last_pos else (None, None)
            login_time = login_times.pop(name, None)
            login_logged.pop(name, None)
            duration_str = ""
            if login_time:
                duration = int((now - login_time).total_seconds())
                duration_str = f" (time: {format_duration(duration)})"

            final_health = last_health.pop(name, None)
            final_armor = last_armor.pop(name, None)
            final_world = last_world.pop(name, None)
            totem_state.pop(name, None)

            # Save logout state for map display
            if x is not None and z is not None:
                last_logout_state[name] = {
                    "x": x,
                    "y": last_pos[2] if last_pos else None,
                    "z": z,
                    "health": final_health if final_health is not None else 20,
                    "armor": final_armor if final_armor is not None else 0,
                    "timestamp": now.strftime('[%Y-%m-%d %I:%M:%S %p GMT+8]')
                }

            # Death detection: if player logs out with health = 0 (died while offline or disconnected while dead)
            if final_health is not None and final_health == 0:
                # Find 2 closest alive players only (health > 0)
                nearby_players = []
                if last_pos and final_world:
                    for other, pos in last_positions.items():
                        if other == name:
                            continue
                        if last_world.get(other) != final_world:
                            continue
                        # Only count alive players (health > 0)
                        if last_health.get(other, 20) <= 0:
                            continue
                        dist = math.sqrt((pos[0] - x) ** 2 + (pos[1] - z) ** 2)
                        nearby_players.append((dist, other))

                # Sort by distance and take top 2
                nearby_players.sort(key=lambda p: p[0])
                top_2 = nearby_players[:2]

                nearby_str = ""
                if top_2:
                    closest_parts = [f'"{pname}"({int(d)}blk)' for d, pname in top_2]
                    nearby_str = f" {' '.join(closest_parts)}"

                # Format: user3 died | x:0 Z:-3 "player1" "player2"
                dmsg = f"{name} died | x:{x} Z:{z}{nearby_str}"
                log_event(dmsg, DEATHS_FILE)
                log_main("death", name, dmsg)
                died_players.pop(name, None)
                death_times[name] = now  # Record death time for respawn detection
                
                # Add to dead players display for 20 sec window
                dead_players_display[name] = {
                    "x": x,
                    "y": last_pos[2] if last_pos else None,
                    "z": z,
                    "world": final_world if final_world else "Unknown",
                    "health": 0,
                    "armor": final_armor if final_armor is not None else 0,
                    "death_time": now
                }
            else:
                # Only log logout if not a death
                msg = f"Logout {name} logged out at X={x}, Z={z}{duration_str}"
                log_event(msg, LOGINS_FILE)
                log_main("login", name, msg)

            if name in region_entries:
                for region_id, entry_time in list(region_entries[name].items()):
                    duration = format_duration(int((now - entry_time).total_seconds()))
                    region_name = region_id.split("_", 1)[1] if "_" in region_id else f"Region {region_id}"
                    log_event(f"Region {name} LEFT {region_name} at X={x}, Z={z} (spent {duration})", REGIONS_FILE)
                region_entries.pop(name, None)

        # Region exits for players still online but moved out
        for name, regions_tracked in list(region_entries.items()):
            if name not in current_regions:
                continue
            active_regions = current_regions[name]
            for region_id in list(regions_tracked.keys()):
                if region_id not in active_regions:
                    entry_time = regions_tracked.pop(region_id)
                    duration = format_duration(int((now - entry_time).total_seconds()))
                    region_name = region_id.split("_", 1)[1] if "_" in region_id else f"Region {region_id}"
                    last_pos = last_positions.get(name)
                    x, z = (last_pos[0], last_pos[1]) if last_pos else (None, None)
                    log_event(f"Region {name} LEFT {region_name} at X={x}, Z={z} (spent {duration})", REGIONS_FILE)
            if not regions_tracked:
                region_entries.pop(name, None)

        seen_players = current_players

def normalize_error(msg: str) -> str:
    msg = re.sub(r"0x[0-9A-Fa-f]+", "0xXXXX", msg)
    if "Connection refused" in msg or "WinError 10061" in msg:
        return "Connection refused"
    if "unreachable" in msg or "WinError 10065" in msg:
        return "Host unreachable"
    if "aborted" in msg or "WinError 10053" in msg:
        return "Connection aborted"
    if "Max retries exceeded" in msg:
        return "Max retries exceeded"
    return msg

def check_players():
    global last_error_message, error_start_time
    try:
        data = requests.get(API_URL, timeout=1).json()
        players = data.get("players", [])
        if not isinstance(players, list):
            print("Warning: 'players' is not a list!", players)
            players = []
        if last_error_message is not None:
            duration = (datetime.now(GMT8) - error_start_time).total_seconds()
            log_event(f"Connection recovered after {format_duration(int(duration))}", LOGINS_FILE)
            last_error_message = None
            error_start_time = None
        process_players(players)
    except Exception as e:
        raw_error = str(e)
        error_message = normalize_error(raw_error)
        if last_error_message is None:
            last_error_message = error_message
            error_start_time = datetime.now(GMT8)
            log_event(f"Error checking players: {error_message}", LOGINS_FILE)
        elif error_message != last_error_message:
            duration = (datetime.now(GMT8) - error_start_time).total_seconds()
            log_event(f"Previous error lasted {format_duration(int(duration))}", LOGINS_FILE)
            last_error_message = error_message
            error_start_time = datetime.now(GMT8)
            log_event(f"Error checking players: {error_message}", LOGINS_FILE)

def run_loop():
    log_event("Tracking players (logins, regions, planes, counts)...", LOGINS_FILE)
    while True:
        check_players()
        time.sleep(0.5)

# Start background threads
threading.Thread(target=cleanup_main_log, daemon=True).start()
threading.Thread(target=run_loop, daemon=True).start()

# Flask app
app = Flask(__name__)

@app.route("/")
def home():
    return send_file("dashboard.html")

@app.route("/tiles/players.json")
def proxy_players():
    try:
        resp = requests.get(API_URL, timeout=1)
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": str(e), "players": [], "max": 0}), 502

@app.route("/map-tile/<int:zoom>/<path:coords>.png")
def proxy_map_tile(zoom, coords):
    """Proxy Squaremap tiles — JS computes sqZoom/sqX/sqZ directly."""
    try:
        parts = coords.split('/')
        sq_x, sq_z = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return '', 400
    base = API_URL.rsplit('/tiles', 1)[0]
    tile_url = f"{base}/tiles/minecraft_overworld/{zoom}/{sq_x}_{sq_z}.png"
    try:
        from flask import Response
        resp = requests.get(tile_url, timeout=3)
        if resp.status_code == 200:
            return Response(resp.content, mimetype='image/png',
                            headers={'Cache-Control': 'public, max-age=10'})
        return '', 404
    except Exception:
        return '', 502

@app.route("/logs/l")
def show_logins():
    try:
        with open(LOGINS_FILE, "r", encoding="utf-8") as f:
            return "<pre>" + f.read() + "</pre>"
    except FileNotFoundError:
        return "No login/logout logs yet."

@app.route("/logs/r")
def show_regions():
    try:
        with open(REGIONS_FILE, "r", encoding="utf-8") as f:
            return "<pre>" + f.read() + "</pre>"
    except FileNotFoundError:
        return "No region detection logs yet."

@app.route("/logs/p")
def show_planes():
    try:
        with open(PLANES_FILE, "r", encoding="utf-8") as f:
            return "<pre>" + f.read() + "</pre>"
    except FileNotFoundError:
        return "No plane detection logs yet."

@app.route("/logs/d")
def show_deaths():
    try:
        with open(DEATHS_FILE, "r", encoding="utf-8") as f:
            return "<pre>" + f.read() + "</pre>"
    except FileNotFoundError:
        return "No death/totem logs yet."

@app.route("/logs/c")
def show_counts():
    try:
        with open(COUNT_FILE, "r", encoding="utf-8") as f:
            return "<pre>" + f.read() + "</pre>"
    except FileNotFoundError:
        return "No player count logs yet."

@app.route("/logs/main")
def show_main():
    try:
        with open(MAIN_LOG_FILE, "r", encoding="utf-8") as f:
            return "<pre>" + f.read() + "</pre>"
    except FileNotFoundError:
        return "No main logs yet."

@app.route("/logs/js")
def show_js():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON"}), 500

@app.route("/api/logout-state")
def get_logout_state():
    """Return logged-out players data for map display."""
    return jsonify(last_logout_state)

@app.route("/api/dead-players")
def get_dead_players():
    """Return dead players within 20 sec window for display."""
    return jsonify(dead_players_display)

# === Region Management API ===
@app.route("/api/regions", methods=["GET"])
def get_regions():
    """Return all regions."""
    with regions_lock:
        return jsonify(regions)

@app.route("/api/regions", methods=["POST"])
def save_regions():
    """Save regions from client."""
    global regions
    try:
        data = request.get_json()
        if not isinstance(data, list):
            return jsonify({"error": "Expected a list of regions"}), 400
        with regions_lock:
            regions = data
        save_regions_to_file()
        return jsonify({"success": True, "count": len(regions)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/world-bounds", methods=["GET"])
def get_world_bounds():
    """Return dynamic world bounds based on current player positions."""
    try:
        data = requests.get(API_URL, timeout=1).json()
        players = data.get("players", [])
        if not players:
            return jsonify({"xmin": -5000, "xmax": 5000, "zmin": -5000, "zmax": 5000})
        
        xs = [p["x"] for p in players if "x" in p]
        zs = [p["z"] for p in players if "z" in p]
        
        if not xs or not zs:
            return jsonify({"xmin": -5000, "xmax": 5000, "zmin": -5000, "zmax": 5000})
        
        # Add 20% padding to bounds
        xmin, xmax = min(xs), max(xs)
        zmin, zmax = min(zs), max(zs)
        x_padding = max((xmax - xmin) * 0.2, 1000)
        z_padding = max((zmax - zmin) * 0.2, 1000)
        
        return jsonify({
            "xmin": int(xmin - x_padding),
            "xmax": int(xmax + x_padding),
            "zmin": int(zmin - z_padding),
            "zmax": int(zmax + z_padding)
        })
    except Exception as e:
        return jsonify({"error": str(e), "xmin": -5000, "xmax": 5000, "zmin": -5000, "zmax": 5000}), 500

if __name__ == "__main__":
    load_regions()  # Load regions on startup
    app.run(host="0.0.0.0", port=8000)

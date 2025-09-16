import os
import csv
import time
import math
import queue
import pytz
import logging
import threading
import pandas as pd
from datetime import datetime
from collections import deque
from flask import Flask, render_template, Response
from flask_socketio import SocketIO
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

# -------------------- Logging --------------------
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# -------------------- Flask / SocketIO --------------------
app = Flask(__name__)
socketio = SocketIO(app)
BANGKOK_TZ = pytz.timezone('Asia/Bangkok')

# -------------------- OSC / Thresholds --------------------
OSC_IP = "0.0.0.0"
OSC_PORT = 12345
ACC_THRESHOLD = 0.25
GYRO_THRESHOLD = 0.1
MAG_THRESHOLD = 1.0  # ดูฟังก์ชัน mag_ratio_crossed()
RESET_TIMEOUT = 1.0

# -------------------- Paths --------------------
BASE_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
WALK_DIR = os.path.join(BASE_DATA_DIR, "walk")
BED_DIR = os.path.join(BASE_DATA_DIR, "bed to toilet")
for directory in [BASE_DATA_DIR, WALK_DIR, BED_DIR]:
    os.makedirs(directory, exist_ok=True)
if not os.access(BASE_DATA_DIR, os.W_OK):
    raise PermissionError(f"No write permission in {BASE_DATA_DIR}")

# ---------- Encoding helper ----------
def pick_file_encoding(path: str) -> str:
    """ลองเดาตัวเข้ารหัสของไฟล์ CSV เพื่อหลีกเลี่ยง UnicodeDecodeError"""
    candidates = ['utf-8-sig', 'utf-8', 'cp874', 'cp1252', 'latin1']
    for enc in candidates:
        try:
            with open(path, 'r', encoding=enc, errors='strict') as f:
                f.read(4096)  # ทดลองอ่าน
            return enc
        except UnicodeDecodeError:
            continue
        except Exception:
            break
    return 'latin1'

# -------------------- Runtime State --------------------
data_dict = {
    "ACC": {"x": 0.0, "y": 0.0, "z": 0.0},
    "GYRO": {"x": 0.0, "y": 0.0, "z": 0.0},
    "MAG": {"x": 0.0, "y": 0.0, "z": 0.0},
}
baseline = {
    "ACC": {"x": 0.0, "y": 0.0, "z": 0.0},
    "GYRO": {"x": 0.0, "y": 0.0, "z": 0.0},
}
previous_value = {
    "ACC": {"x": 0.0, "y": 0.0, "z": 0.0},
    "GYRO": {"x": 0.0, "y": 0.0, "z": 0.0},
    "MAG": {"x": 0.0, "y": 0.0, "z": 0.0},  # สำหรับโหมด prev
}
last_active_time = {"ACC": time.time(), "GYRO": time.time(), "MAG": time.time()}

# เก็บค่าดิบ ACC/MAG เพื่อคำนวณ heading ทุกครั้งที่เขียน CSV
raw_values = {
    "ACC": {"x": 0.0, "y": 0.0, "z": 0.0},
    "MAG": {"x": 0.0, "y": 0.0, "z": 0.0},
}

# ส่งขึ้นหน้าจอแบบเรียลไทม์
latest_heading_deg = None
latest_heading_dir = ""

# -------------------- MAG Baseline & Modes --------------------
MAG_baseline = {"x": 1.0, "y": 1.0, "z": 1.0}
RELATIVE_MODE_MAG = 'x0'  # 'x0' หรือ 'prev'
MAG_EPS = 1e-6

# -------------------- Recording State --------------------
is_recording = False
record_lock = threading.Lock()
csv_queue = queue.Queue(maxsize=1000)
csv_thread = None
current_raw_file = None
current_resampled_file = None
current_5s_resampled_file = None
current_10s_resampled_file = None
resample_lock = threading.Lock()
current_activity = None
log_buffer = deque(maxlen=50)
log_counter = 0

# =======================================================
# Routes
# =======================================================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/download/raw/<activity>/<filename>")
def download_raw_file(activity, filename):
    file_path = os.path.join(BASE_DATA_DIR, activity, filename)
    if not os.path.exists(file_path):
        return f"Raw file not found: {file_path}", 404
    with open(file_path, 'rb') as f:
        return Response(f.read(), mimetype='text/csv', headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/download/resampled/<activity>/<filename>")
def download_resampled_file(activity, filename):
    file_path = os.path.join(BASE_DATA_DIR, activity, filename)
    if not os.path.exists(file_path):
        return f"Resampled file not found: {file_path}", 404
    with open(file_path, 'rb') as f:
        return Response(f.read(), mimetype='text/csv', headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/download/resampled_5s/<activity>/<filename>")
def download_5s_resampled_file(activity, filename):
    file_path = os.path.join(BASE_DATA_DIR, activity, filename)
    if not os.path.exists(file_path):
        return f"5s Resampled file not found: {file_path}", 404
    with open(file_path, 'rb') as f:
        return Response(f.read(), mimetype='text/csv', headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.route("/download/resampled_10s/<activity>/<filename>")
def download_10s_resampled_file(activity, filename):
    file_path = os.path.join(BASE_DATA_DIR, activity, filename)
    if not os.path.exists(file_path):
        return f"10s Resampled file not found: {file_path}", 404
    with open(file_path, 'rb') as f:
        return Response(f.read(), mimetype='text/csv', headers={"Content-Disposition": f"attachment; filename={filename}"})

# =======================================================
# Socket events
# =======================================================
@socketio.on("activity_started")
def handle_activity_started(data):
    global current_activity
    current_activity = data["activity"]

@socketio.on("set_mag_mode")
def set_mag_mode(data):
    global RELATIVE_MODE_MAG
    mode = (data or {}).get("mode")
    if mode not in ("x0", "prev"):
        socketio.emit("mag_mode_status", {"ok": False, "mode": RELATIVE_MODE_MAG, "error": "invalid mode"})
        return
    RELATIVE_MODE_MAG = mode
    socketio.emit("mag_mode_status", {"ok": True, "mode": RELATIVE_MODE_MAG})

@socketio.on("get_mag_mode")
def get_mag_mode():
    socketio.emit("mag_mode_status", {"ok": True, "mode": RELATIVE_MODE_MAG})

# ---------- Helpers ----------
def mag_ratio_crossed(ratio_value: float, threshold: float) -> bool:
    """threshold >=1: ใช้ lower/upper จาก 1.0 | threshold <1: ใช้ |ratio-1| > threshold"""
    if threshold >= 1.0:
        upper = threshold
        lower = 2.0 - threshold
        return ratio_value > upper or ratio_value < lower
    else:
        return abs(ratio_value - 1.0) > threshold

def heading_to_cardinal_th(deg: float) -> str:
    dirs = [
        "N (เหนือ)", "NNE", "NE (ตอ.เฉียงเหนือ)", "ENE", "E (ตะวันออก)", "ESE",
        "SE (ตอ.เฉียงใต้)", "SSE", "S (ใต้)", "SSW", "SW (ตะวันตกเฉียงใต้)", "WSW",
        "W (ตะวันตก)", "WNW", "NW (ตะวันตกเฉียงเหนือ)", "NNW"
    ]
    idx = int((deg + 11.25) // 22.5) % 16
    return dirs[idx]

def compute_heading_deg(raw_acc: dict, raw_mag: dict):
    """tilt-compensated heading (0..360, 0=เหนือ)"""
    ax, ay, az = raw_acc["x"], raw_acc["y"], raw_acc["z"]
    mx, my, mz = raw_mag["x"], raw_mag["y"], raw_mag["z"]
    if abs(mx) + abs(my) + abs(mz) < 1e-9:
        return None
    norm_a = math.sqrt(ax*ax + ay*ay + az*az)
    if norm_a > 1e-6:
        roll = math.atan2(ay, az)
        pitch = math.atan2(-ax, math.sqrt(ay*ay + az*az))
        mx2 = mx * math.cos(pitch) + mz * math.sin(pitch)
        my2 = mx * math.sin(roll) * math.sin(pitch) + my * math.cos(roll) - mz * math.sin(roll) * math.cos(pitch)
        heading_rad = math.atan2(mx2, my2)
    else:
        heading_rad = math.atan2(mx, my)
    deg = math.degrees(heading_rad)
    if deg < 0:
        deg += 360.0
    return deg

def print_all(address, *args):
    global is_recording, baseline, previous_value, last_active_time, current_activity, log_counter
    global latest_heading_deg, latest_heading_dir
    current_time = time.time()
    updated = False
    baseline_updated = False
    log_counter += 1
    if log_counter % 10 == 0:
        log_buffer.append(f"Received OSC message: address={address}, args={args}")
        logger.debug("\n".join(log_buffer))

    def process(sensor_type, threshold):
        nonlocal updated, baseline_updated
        key = address.split('/')[-1].lower()
        if ':' in key:
            key = key.split(':')[-1].lower()
        try:
            value = float(args[-1])
        except (ValueError, TypeError):
            return

        # เก็บค่าดิบไว้คำนวณ heading
        if sensor_type in ("ACC", "MAG") and key in ("x", "y", "z"):
            raw_values[sensor_type][key] = value

        # ACC / GYRO
        if sensor_type in ["ACC", "GYRO"] and key in baseline[sensor_type]:
            delta = abs(value - previous_value[sensor_type][key])
            if delta < threshold / 2:
                baseline[sensor_type][key] = (baseline[sensor_type][key] + value) / 2.0
                baseline_updated = True
            previous_value[sensor_type][key] = value
            value_corrected = value - baseline[sensor_type][key]
            if abs(value_corrected) > threshold:
                data_dict[sensor_type][key] = value_corrected
                last_active_time[sensor_type] = current_time
                updated = True
            elif current_time - last_active_time[sensor_type] > RESET_TIMEOUT:
                if data_dict[sensor_type].get(key, 0.0) != 0.0:
                    data_dict[sensor_type][key] = 0.0
                    updated = True
        # MAG (เลือกโหมด x0 / prev)
        elif sensor_type == "MAG":
            if RELATIVE_MODE_MAG == "prev":
                ref = previous_value['MAG'].get(key, value)  # Xi-1
            else:
                ref = MAG_baseline.get(key, value)  # X0
            denom = ref if abs(ref) > MAG_EPS else 1.0
            ratio = value / denom
            if mag_ratio_crossed(ratio, threshold):
                data_dict[sensor_type][key] = ratio
                last_active_time[sensor_type] = current_time
                updated = True
            elif current_time - last_active_time[sensor_type] > RESET_TIMEOUT:
                if data_dict[sensor_type].get(key, 0.0) != 0.0:
                    data_dict[sensor_type][key] = 0.0
                    updated = True
            previous_value['MAG'][key] = value

        # อัปเดต heading แบบเรียลไทม์เพื่อโชว์บน UI
        hd = compute_heading_deg(raw_values["ACC"], raw_values["MAG"])
        if hd is not None:
            latest_heading_deg = float(round(hd, 1))
            latest_heading_dir = heading_to_cardinal_th(latest_heading_deg)
            socketio.emit("heading_data", {"deg": latest_heading_deg, "dir": latest_heading_dir})

    # route
    if "ACC" in address:
        process("ACC", ACC_THRESHOLD)
    elif "GYRO" in address:
        process("GYRO", GYRO_THRESHOLD)
    elif "MAG" in address:
        process("MAG", MAG_THRESHOLD)

    if updated:
        socketio.emit("sensor_data", data_dict)

    # -------- เขียน CSV: คำนวณ heading ใหม่ทุกครั้ง เพื่อให้มีค่าทั้งองศาและชื่อทิศ --------
    with record_lock:
        recording = is_recording
    if recording and updated:
        csv_row = {}
        for sensor in ["ACC", "GYRO", "MAG"]:
            for axis in ["x", "y", "z"]:
                v = data_dict.get(sensor, {}).get(axis, 0.0)
                csv_row[f"{sensor}_{axis.upper()}"] = f"{v:.4f}"
        hd = compute_heading_deg(raw_values["ACC"], raw_values["MAG"])
        if hd is not None:
            hd = float(round(hd, 1))
            hd_dir = heading_to_cardinal_th(hd)
            csv_row["Heading_Deg"] = f"{hd:.1f}"
            csv_row["Heading_Dir"] = hd_dir
            latest_heading_deg, latest_heading_dir = hd, hd_dir
        else:
            csv_row["Heading_Deg"] = ""
            csv_row["Heading_Dir"] = ""
        now = datetime.now(BANGKOK_TZ)
        csv_row["Date"] = now.strftime('%d/%m/%Y')
        csv_row["Time"] = now.strftime('%H:%M:%S.%f')[:-3]
        csv_row["Activity"] = current_activity if current_activity else "unknown"
        try:
            csv_queue.put_nowait(csv_row)
        except queue.Full:
            logger.warning("CSV queue full, dropping oldest data")
            csv_queue.get()
            csv_queue.put_nowait(csv_row)

    if baseline_updated:
        socketio.emit("baseline_data", baseline)

# -------------------- CSV Writer Thread --------------------
def csv_writer_thread():
    fieldnames = ["Date", "Time", "Activity"] + \
        [f"{sensor}_{axis}" for sensor in ["ACC", "GYRO", "MAG"] for axis in ["X", "Y", "Z"]] + \
        ["Heading_Deg", "Heading_Dir"]
    batch_size = 100
    buffer = []
    if not os.access(os.path.dirname(current_raw_file) or '.', os.W_OK):
        logger.error(f"No write permission for file: {current_raw_file}")
        return
    try:
        # เขียนเป็น UTF-8 (BOM)
        with open(current_raw_file, mode='w', newline='', buffering=8192, encoding='utf-8-sig') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            csv_file.flush()
        with open(current_raw_file, mode='a', newline='', buffering=8192, encoding='utf-8-sig') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            while True:
                row = csv_queue.get()
                if row is None:
                    if buffer:
                        writer.writerows(buffer)
                        csv_file.flush()
                    break
                buffer.append(row)
                if len(buffer) >= batch_size:
                    writer.writerows(buffer)
                    csv_file.flush()
                    buffer.clear()
    except Exception as e:
        logger.error(f"Error in csv_writer_thread: {str(e)}")

# -------------------- Start / Stop Recording --------------------
@socketio.on("start_recording")
def start_recording(data):
    global is_recording, current_raw_file, current_resampled_file, current_5s_resampled_file, current_10s_resampled_file, csv_thread, current_activity
    activity = data["activity"]
    if activity not in ["walk", "bed to toilet"]:
        socketio.emit("recording_stopped", {"status": "error", "message": "Invalid activity"})
        return
    with record_lock:
        if is_recording:
            return
        is_recording = True
        current_activity = activity
    while not csv_queue.empty():
        csv_queue.get()
    socketio.emit("activity_started", {"activity": activity})
    timestamp = datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d_%H-%M-%S")
    activity_dir = WALK_DIR if activity == "walk" else BED_DIR
    current_raw_file = os.path.join(activity_dir, f"{activity}_data_{timestamp}.csv")
    current_resampled_file = os.path.join(activity_dir, f"resampled_{activity}_data_{timestamp}.csv")
    current_5s_resampled_file = os.path.join(activity_dir, f"resampled_5s_{activity}_data_{timestamp}.csv")
    current_10s_resampled_file = os.path.join(activity_dir, f"resampled_10s_{activity}_data_{timestamp}.csv")
    csv_thread = threading.Thread(target=csv_writer_thread, daemon=True)
    csv_thread.start()

@socketio.on("stop_recording")
def stop_recording():
    global is_recording, csv_thread, current_activity, current_raw_file, current_resampled_file, current_5s_resampled_file, current_10s_resampled_file
    with record_lock:
        if not is_recording:
            return
        is_recording = False
    while not csv_queue.empty():
        time.sleep(0.1)
    csv_queue.put(None)
    if csv_thread:
        csv_thread.join(timeout=2.0)
    try:
        if os.path.exists(current_raw_file) and os.path.getsize(current_raw_file) > 0:
            with resample_lock:
                cols = ['Date', 'Time', 'Activity'] + \
                    [f"{sensor}_{axis}" for sensor in ["ACC", "GYRO", "MAG"] for axis in ["X", "Y", "Z"]] + \
                    ["Heading_Deg", "Heading_Dir"]

                # Resample 1s
                with open(current_resampled_file, mode='w', newline='', encoding='utf-8-sig') as f:
                    csv.DictWriter(f, fieldnames=cols).writeheader()
                chunk_size = 1000
                resampled_chunks = []
                enc = pick_file_encoding(current_raw_file)
                for chunk in pd.read_csv(current_raw_file, chunksize=chunk_size, encoding=enc, engine='python', on_bad_lines='skip'):
                    chunk['timestamp'] = pd.to_datetime(
                        chunk['Date'] + ' ' + chunk['Time'],
                        format='%d/%m/%Y %H:%M:%S.%f',
                        errors='coerce'
                    )
                    chunk = chunk.dropna(subset=['timestamp'])
                    chunk['timestamp'] = chunk['timestamp'].dt.tz_localize(BANGKOK_TZ)
                    chunk = chunk.drop_duplicates(subset=['timestamp'], keep='last')
                    chunk = chunk.set_index('timestamp')
                    numeric_cols = [f"{sensor}_{axis}" for sensor in ["ACC", "GYRO", "MAG"] for axis in ["X", "Y", "Z"]] + ["Heading_Deg"]
                    resampled_numeric = (chunk[numeric_cols]
                        .resample('1S', closed='right', label='left')
                        .mean())
                    def _dir_from_deg(d):
                        return heading_to_cardinal_th(d) if pd.notna(d) else ""
                    dir_series = resampled_numeric['Heading_Deg'].apply(_dir_from_deg)
                    resampled_numeric = resampled_numeric.fillna(0)
                    resampled_chunk = resampled_numeric.copy()
                    resampled_chunk['Heading_Dir'] = dir_series
                    resampled_chunk['Activity'] = (chunk['Activity']
                        .resample('1S', closed='right', label='left')
                        .ffill())
                    resampled_chunk['Date'] = resampled_chunk.index.strftime('%d/%m/%Y')
                    resampled_chunk['Time'] = resampled_chunk.index.strftime('%H:%M:%S')
                    resampled_chunks.append(resampled_chunk[cols])
                if resampled_chunks:
                    resampled_df = pd.concat(resampled_chunks).reset_index()
                    resampled_df = resampled_df.drop_duplicates(subset='timestamp', keep='last')
                    resampled_df['Activity'] = resampled_df['Activity'].fillna(current_activity)
                    resampled_df[cols].to_csv(current_resampled_file, mode='a', index=False, float_format='%.4f', header=False, encoding='utf-8-sig')
                else:
                    pd.DataFrame(columns=cols).to_csv(current_resampled_file, mode='a', index=False, header=False, encoding='utf-8-sig')

                # Resample 5s
                with open(current_5s_resampled_file, mode='w', newline='', encoding='utf-8-sig') as f:
                    csv.DictWriter(f, fieldnames=cols).writeheader()
                chunk_size = 1000
                resampled_5s_chunks = []
                enc = pick_file_encoding(current_raw_file)
                for chunk in pd.read_csv(current_raw_file, chunksize=chunk_size, encoding=enc, engine='python', on_bad_lines='skip'):
                    chunk['timestamp'] = pd.to_datetime(
                        chunk['Date'] + ' ' + chunk['Time'],
                        format='%d/%m/%Y %H:%M:%S.%f',
                        errors='coerce'
                    )
                    chunk = chunk.dropna(subset=['timestamp'])
                    chunk['timestamp'] = chunk['timestamp'].dt.tz_localize(BANGKOK_TZ)
                    chunk = chunk.drop_duplicates(subset=['timestamp'], keep='last')
                    chunk = chunk.set_index('timestamp')
                    numeric_cols = [f"{sensor}_{axis}" for sensor in ["ACC", "GYRO", "MAG"] for axis in ["X", "Y", "Z"]] + ["Heading_Deg"]
                    resampled_numeric = (chunk[numeric_cols]
                        .resample('5S', closed='right', label='left')
                        .mean())
                    def _dir_from_deg(d):
                        return heading_to_cardinal_th(d) if pd.notna(d) else ""
                    dir_series = resampled_numeric['Heading_Deg'].apply(_dir_from_deg)
                    resampled_numeric = resampled_numeric.fillna(0)
                    resampled_chunk = resampled_numeric.copy()
                    resampled_chunk['Heading_Dir'] = dir_series
                    resampled_chunk['Activity'] = (chunk['Activity']
                        .resample('5S', closed='right', label='left')
                        .ffill())
                    resampled_chunk['Date'] = resampled_chunk.index.strftime('%d/%m/%Y')
                    resampled_chunk['Time'] = resampled_chunk.index.strftime('%H:%M:%S')
                    resampled_5s_chunks.append(resampled_chunk[cols])
                if resampled_5s_chunks:
                    resampled_5s_df = pd.concat(resampled_5s_chunks).reset_index()
                    resampled_5s_df = resampled_5s_df.drop_duplicates(subset='timestamp', keep='last')
                    resampled_5s_df['Activity'] = resampled_5s_df['Activity'].fillna(current_activity)
                    resampled_5s_df[cols].to_csv(current_5s_resampled_file, mode='a', index=False, float_format='%.4f', header=False, encoding='utf-8-sig')
                else:
                    pd.DataFrame(columns=cols).to_csv(current_5s_resampled_file, mode='a', index=False, header=False, encoding='utf-8-sig')

                # Resample 10s
                with open(current_10s_resampled_file, mode='w', newline='', encoding='utf-8-sig') as f:
                    csv.DictWriter(f, fieldnames=cols).writeheader()
                chunk_size = 1000
                resampled_10s_chunks = []
                enc = pick_file_encoding(current_raw_file)
                for chunk in pd.read_csv(current_raw_file, chunksize=chunk_size, encoding=enc, engine='python', on_bad_lines='skip'):
                    chunk['timestamp'] = pd.to_datetime(
                        chunk['Date'] + ' ' + chunk['Time'],
                        format='%d/%m/%Y %H:%M:%S.%f',
                        errors='coerce'
                    )
                    chunk = chunk.dropna(subset=['timestamp'])
                    chunk['timestamp'] = chunk['timestamp'].dt.tz_localize(BANGKOK_TZ)
                    chunk = chunk.drop_duplicates(subset=['timestamp'], keep='last')
                    chunk = chunk.set_index('timestamp')
                    numeric_cols = [f"{sensor}_{axis}" for sensor in ["ACC", "GYRO", "MAG"] for axis in ["X", "Y", "Z"]] + ["Heading_Deg"]
                    resampled_numeric = (chunk[numeric_cols]
                        .resample('10S', closed='right', label='left')
                        .mean())
                    def _dir_from_deg(d):
                        return heading_to_cardinal_th(d) if pd.notna(d) else ""
                    dir_series = resampled_numeric['Heading_Deg'].apply(_dir_from_deg)
                    resampled_numeric = resampled_numeric.fillna(0)
                    resampled_chunk = resampled_numeric.copy()
                    resampled_chunk['Heading_Dir'] = dir_series
                    resampled_chunk['Activity'] = (chunk['Activity']
                        .resample('10S', closed='right', label='left')
                        .ffill())
                    resampled_chunk['Date'] = resampled_chunk.index.strftime('%d/%m/%Y')
                    resampled_chunk['Time'] = resampled_chunk.index.strftime('%H:%M:%S')
                    resampled_10s_chunks.append(resampled_chunk[cols])
                if resampled_10s_chunks:
                    resampled_10s_df = pd.concat(resampled_10s_chunks).reset_index()
                    resampled_10s_df = resampled_10s_df.drop_duplicates(subset='timestamp', keep='last')
                    resampled_10s_df['Activity'] = resampled_10s_df['Activity'].fillna(current_activity)
                    resampled_10s_df[cols].to_csv(current_10s_resampled_file, mode='a', index=False, float_format='%.4f', header=False, encoding='utf-8-sig')
                else:
                    pd.DataFrame(columns=cols).to_csv(current_10s_resampled_file, mode='a', index=False, header=False, encoding='utf-8-sig')

                socketio.emit("recording_stopped", {
                    "status": "success",
                    "download_urls": {
                        "raw": f"/download/raw/{current_activity}/{os.path.basename(current_raw_file)}",
                        "resampled": f"/download/resampled/{current_activity}/{os.path.basename(current_resampled_file)}",
                        "5s_resampled": f"/download/resampled_5s/{current_activity}/{os.path.basename(current_5s_resampled_file)}",
                        "10s_resampled": f"/download/resampled_10s/{current_activity}/{os.path.basename(current_10s_resampled_file)}"
                    }
                })
        else:
            cols = ['Date', 'Time', 'Activity'] + \
                [f"{sensor}_{axis}" for sensor in ["ACC", "GYRO", "MAG"] for axis in ["X", "Y", "Z"]] + \
                ["Heading_Deg", "Heading_Dir"]
            pd.DataFrame(columns=cols).to_csv(current_resampled_file, mode='w', index=False, encoding='utf-8-sig')
            pd.DataFrame(columns=cols).to_csv(current_5s_resampled_file, mode='w', index=False, encoding='utf-8-sig')
            pd.DataFrame(columns=cols).to_csv(current_10s_resampled_file, mode='w', index=False, encoding='utf-8-sig')
            socketio.emit("recording_stopped", {
                "status": "success",
                "download_urls": {
                    "raw": f"/download/raw/{current_activity}/{os.path.basename(current_raw_file)}",
                    "resampled": f"/download/resampled/{current_activity}/{os.path.basename(current_resampled_file)}",
                    "5s_resampled": f"/download/resampled_5s/{current_activity}/{os.path.basename(current_5s_resampled_file)}",
                    "10s_resampled": f"/download/resampled_10s/{current_activity}/{os.path.basename(current_10s_resampled_file)}"
                }
            })
    except Exception as e:
        socketio.emit("recording_stopped", {"status": "error", "message": str(e)})

# -------------------- Utility Events --------------------
@socketio.on("clear_data")
def clear_data():
    for sensor in data_dict:
        for axis in data_dict[sensor]:
            data_dict[sensor][axis] = 0.0
    socketio.emit("sensor_data", data_dict)

@socketio.on("set_mag_baseline")
def set_mag_baseline():
    global MAG_baseline
    for axis in ["x", "y", "z"]:
        current_value = raw_values["MAG"].get(axis, 1.0)  # ใช้ "ค่าดิบ" ของ MAG
        if current_value != 0:
            MAG_baseline[axis] = current_value
    socketio.emit("mag_baseline_status", MAG_baseline)

# -------------------- OSC Server Thread --------------------
def osc_server_thread():
    dispatcher = Dispatcher()
    dispatcher.set_default_handler(print_all)
    server = BlockingOSCUDPServer((OSC_IP, OSC_PORT), dispatcher)
    logger.info(f"OSC Server started at {OSC_IP}:{OSC_PORT}")
    server.serve_forever()

# -------------------- Main --------------------
if __name__ == "__main__":
    osc_thread = threading.Thread(target=osc_server_thread, daemon=True)
    osc_thread.start()
    socketio.run(app, host="0.0.0.0", port=5000)
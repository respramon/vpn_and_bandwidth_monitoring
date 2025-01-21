from fastapi import FastAPI
from pydantic import BaseModel
import psutil
import subprocess
import os
import tempfile
import sqlite3
import threading
import time
from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Middleware CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fungsi untuk mendapatkan koneksi database
def get_db_connection():
    return sqlite3.connect("bandwidth_log.db", check_same_thread=False)

# Buat tabel log jika belum ada
def initialize_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bandwidth_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            bytes_sent INTEGER,
            bytes_received INTEGER,
            upload_speed INTEGER,
            download_speed INTEGER
        )
    """)
    conn.commit()
    conn.close()

# Inisialisasi database
initialize_db()

# Fungsi untuk menyimpan log ke database
def save_log_to_db(bytes_sent, bytes_received, upload_speed, download_speed):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO bandwidth_log (timestamp, bytes_sent, bytes_received, upload_speed, download_speed)
            VALUES (?, ?, ?, ?, ?)
        """, (timestamp, bytes_sent, bytes_received, upload_speed, download_speed))
        conn.commit()
    finally:
        conn.close()

# Variabel global untuk melacak waktu penggunaan VPN
vpn_start_time = None
vpn_usage_time = 0  # Dalam detik
vpn_status = "Disconnected"  # Status awal VPN

# Variabel global untuk menyimpan data sebelumnya
previous_bytes_sent = psutil.net_io_counters().bytes_sent
previous_bytes_received = psutil.net_io_counters().bytes_recv

@app.get("/internet_speed")
def get_internet_speed():
    global previous_bytes_sent, previous_bytes_received

    # Data saat ini
    current_bytes_sent = psutil.net_io_counters().bytes_sent
    current_bytes_received = psutil.net_io_counters().bytes_recv

    # Hitung kecepatan dalam bytes per detik
    upload_speed = current_bytes_sent - previous_bytes_sent
    download_speed = current_bytes_received - previous_bytes_received

    # Perbarui nilai sebelumnya
    previous_bytes_sent = current_bytes_sent
    previous_bytes_received = current_bytes_received

    # Simpan log ke database
    save_log_to_db(current_bytes_sent, current_bytes_received, upload_speed, download_speed)

    return {
        "upload_speed_bps": upload_speed,
        "download_speed_bps": download_speed
    }

@app.get("/bandwidth")
def get_bandwidth_usage():
    network_io = psutil.net_io_counters()

    # Simpan log ke database tanpa kecepatan
    save_log_to_db(
        bytes_sent=network_io.bytes_sent,
        bytes_received=network_io.bytes_recv,
        upload_speed=0,
        download_speed=0
    )

    return {
        "bytes_sent": network_io.bytes_sent,
        "bytes_received": network_io.bytes_recv
    }

# Model untuk konfigurasi VPN
class VPNConfig(BaseModel):
    server: str
    username: str
    password: str

@app.post("/vpn/connect")
def connect_vpn(config: VPNConfig):
    global vpn_start_time, vpn_status

    try:
        with tempfile.NamedTemporaryFile(delete=False) as auth_file:
            auth_file.write(f"{config.username}\n{config.password}".encode())
            auth_file_path = auth_file.name

        command = f"sudo openvpn --config {config.server} --auth-user-pass {auth_file_path}"
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        stdout, stderr = process.communicate()

        if "Initialization Sequence Completed" in str(stdout):
            vpn_start_time = datetime.now()  # Simpan waktu mulai
            vpn_status = "Donnected"  # Ubah status VPN
            return {"status": vpn_status, "server": config.server}
        else:
            return {"status": "failed", "error": stderr.decode()}

    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/vpn/disconnect")
def disconnect_vpn():
    global vpn_start_time, vpn_usage_time, vpn_status

    try:
        if vpn_status == "Connected" and vpn_start_time:
            # Hitung durasi penggunaan
            end_time = datetime.now()
            usage_duration = (end_time - vpn_start_time).total_seconds()
            vpn_usage_time += usage_duration

        vpn_start_time = None
        vpn_status = "Disconnected"  # Ubah status VPN

        os.system("sudo killall openvpn")
        return {"status": vpn_status, "total_usage_time_seconds": vpn_usage_time}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.get("/vpn/status")
def get_vpn_status():
    global vpn_status

    try:
        # Periksa apakah proses OpenVPN sedang berjalan
        output = subprocess.check_output("pgrep openvpn", shell=True)
        vpn_status = "Connected" if output else "Disconnected"
    except subprocess.CalledProcessError:
        vpn_status = "Disconnected"

    return {"status": vpn_status}

@app.get("/vpn/usage_time")
def get_vpn_usage_time():
    global vpn_start_time, vpn_usage_time, vpn_status

    if vpn_status == "Connected" and vpn_start_time:
        current_time = datetime.now()
        current_usage = (current_time - vpn_start_time).total_seconds()
        total_time = vpn_usage_time + current_usage
    else:
        total_time = vpn_usage_time

    hours, remainder = divmod(int(total_time), 3600)
    minutes, seconds = divmod(remainder, 60)
    formatted_time = f"{hours:02}:{minutes:02}:{seconds:02}"

    return {"usage_time": formatted_time}

@app.get("/logs")
def get_logs():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT * FROM bandwidth_log ORDER BY id DESC")
        logs = cursor.fetchall()
        return {
            "logs": [
                {
                    "id": log[0],
                    "timestamp": log[1],
                    "bytes_sent": log[2],
                    "bytes_received": log[3],
                    "upload_speed": log[4],
                    "download_speed": log[5]
                }
                for log in logs
            ]
        }
    finally:
        conn.close()

# Fungsi logging berkala untuk menyimpan log ke database setiap 10 detik
def log_bandwidth_periodically():
    global previous_bytes_sent, previous_bytes_received

    while True:
        try:
            current_bytes_sent = psutil.net_io_counters().bytes_sent
            current_bytes_received = psutil.net_io_counters().bytes_recv

            upload_speed = current_bytes_sent - previous_bytes_sent
            download_speed = current_bytes_received - previous_bytes_received

            save_log_to_db(current_bytes_sent, current_bytes_received, upload_speed, download_speed)

            previous_bytes_sent = current_bytes_sent
            previous_bytes_received = current_bytes_received

            time.sleep(10)
        except Exception as e:
            print(f"Error in periodic logging: {e}")

# Jalankan thread logging di latar belakang
threading.Thread(target=log_bandwidth_periodically, daemon=True).start()

@app.get("/")
def read_root():
    return {"message": "Integrated VPN and Bandwidth Monitor API is running."}

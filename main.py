# main.py  -- MicroPython ESP32 server with automatic triggers at boot and daily 06:00 (IST)
# Edit WIFI_SSID, WIFI_PASS, MQTT_BROKER before use.

import network, time, socket, json, os
import _thread

# ---------- CONFIG ----------
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASS = "YOUR_WIFI_PASSWORD"

MQTT_BROKER = "192.168.1.50"   # your MQTT broker IP
MQTT_PORT = 1883
CLIENT_ID = "esp_server_01"

TOPIC_CMD = b"Sensor_cmd"
TOPIC_DATA = b"Sensor_data"

WWW_DIR = "www"
DATA_FILE = "water_readings.jsonl"
DEVICE_MAP_FILE = "device_map.json"

# Timezone offset for Asia/Kolkata (UTC+5:30) in seconds
TIMEZONE_OFFSET_SECONDS = 5 * 3600 + 30 * 60  # 19800

# ---------- Imports that may be firmware-dependent ----------
try:
    from umqtt.simple import MQTTClient
except Exception as e:
    raise SystemExit("umqtt.simple not found in firmware. Install firmware with umqtt or adapt code.")

# try ntptime for NTP sync
HAS_NTP = True
try:
    import ntptime
except Exception:
    HAS_NTP = False

# ---------- global state ----------
lock = _thread.allocate_lock()
client = None
latest = None
waiting_for_sensor = False
sensor_payload = None

USE_MODEL = False
try:
    import model_coef
    if all(hasattr(model_coef, x) for x in ("b", "c_tds", "c_ph", "c_turbidity")):
        USE_MODEL = True
        print("model_coef loaded.")
except Exception:
    USE_MODEL = False

try:
    from wqi_calc import compute_wqi_from_minimal
except Exception:
    compute_wqi_from_minimal = None

# ---------- helpers ----------
def now_utc_ts():
    """Return seconds since epoch (UTC) from RTC."""
    return time.time()

def local_time_tuple():
    """Return localtime tuple (year,mon,day,hour,min,sec,weekday,yearday) adjusted by TIMEZONE_OFFSET_SECONDS."""
    # time.localtime expects seconds since epoch; add tz offset to get localtime from RTC UTC
    ts = now_utc_ts() + TIMEZONE_OFFSET_SECONDS
    return time.localtime(ts)

def local_date_tuple():
    t = local_time_tuple()
    return (t[0], t[1], t[2])  # year, month, day

def sync_time_with_ntp(retries=3, retry_delay=3):
    """Attempt to sync RTC with NTP (UTC). Returns True on success."""
    if not HAS_NTP:
        print("ntptime not available in firmware; skipping NTP sync.")
        return False
    for i in range(retries):
        try:
            print("Attempting NTP sync (attempt {}/{})...".format(i+1, retries))
            ntptime.settime()  # sets RTC to UTC
            print("NTP sync OK. RTC set (UTC).")
            return True
        except Exception as e:
            print("NTP sync failed:", e)
            time.sleep(retry_delay)
    print("NTP sync failed after {} attempts.".format(retries))
    return False

# device map helpers
def load_device_map():
    try:
        with open(DEVICE_MAP_FILE, "r") as f:
            return json.loads(f.read())
    except Exception:
        return {}

def save_device_map(m):
    try:
        with open(DEVICE_MAP_FILE, "w") as f:
            f.write(json.dumps(m))
        return True
    except Exception as e:
        print("save_device_map error:", e)
        return False

def get_device_info(device_id):
    d = load_device_map()
    return d.get(device_id, {})

def ensure_default_device_mapping(device_id, default_state="Uttar Pradesh"):
    dmap = load_device_map()
    if device_id not in dmap:
        dmap[device_id] = {"state": default_state}
        save_device_map(dmap)

# file storage helpers (thread-safe)
def save_reading(rec):
    try:
        lock.acquire()
        with open(DATA_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
        lock.release()
        return True
    except Exception as e:
        try: lock.release()
        except: pass
        print("save_reading error:", e)
        return False

def read_readings_by_state(state=None, limit=None):
    results = []
    try:
        if not os.path.exists(DATA_FILE):
            return results
        lock.acquire()
        with open(DATA_FILE, "r") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except:
                    continue
                if state is None or state.lower() == 'all':
                    results.append(r)
                else:
                    st = r.get("state")
                    if st and st.lower() == state.lower():
                        results.append(r)
        lock.release()
    except Exception as e:
        try: lock.release()
        except: pass
        print("read_readings_by_state error:", e)
    if limit:
        return results[-limit:]
    return results

# ---------- WQI prediction helper ----------
def predict_wqi(tds, ph, turbidity):
    try:
        t = float(tds); p = float(ph); u = float(turbidity)
    except:
        return None
    if USE_MODEL:
        try:
            w = float(model_coef.b) + float(model_coef.c_tds)*t + float(model_coef.c_ph)*p + float(model_coef.c_turbidity)*u
            if w < 0: w = 0.0
            if w > 100: w = 100.0
            return round(w,2)
        except Exception as e:
            print("model predict error:", e)
    if compute_wqi_from_minimal:
        try:
            return compute_wqi_from_minimal(t, p, u)[0]
        except Exception:
            pass
    # fallback heuristic
    wqi = 50.0 + (7.0 - abs(p - 7.0))*2.0 - (t/600.0)*5.0 - (u/15.0)*8.0
    if wqi < 0: wqi = 0.0
    if wqi > 100: wqi = 100.0
    return round(wqi,2)

# ---------- MQTT callback ----------
def mqtt_callback(topic, msg):
    global latest, waiting_for_sensor, sensor_payload
    try:
        tstr = topic.decode() if isinstance(topic, bytes) else str(topic)
        mstr = msg.decode() if isinstance(msg, bytes) else str(msg)
        print("MQTT IN:", tstr, mstr)
        if tstr == TOPIC_DATA.decode():
            try:
                j = json.loads(mstr)
            except Exception as e:
                print("Malformed JSON from sensor:", e)
                return
            rd = j.get("readings", {})
            tds = rd.get("tds_ppm") or rd.get("tds") or rd.get("tds_mg_L")
            ph = rd.get("ph")
            turb = rd.get("turbidity_pct") or rd.get("turbidity") or rd.get("turbidity_NTU")

            device_id = j.get("device", "sensor")
            ensure_default_device_mapping(device_id)  # ensures mapping exists with default state (Uttar Pradesh)
            dev_info = get_device_info(device_id)
            state_v = dev_info.get("state") or "Uttar Pradesh"

            wqi = predict_wqi(tds, ph, turb)

            rec = {
                "ts": int(now_utc_ts()),   # store UTC epoch (use local_time for display on client)
                "device": device_id,
                "tds": tds,
                "ph": ph,
                "turbidity": turb,
                "wqi": wqi,
                "state": state_v,
                "raw": rd
            }
            latest = rec
            saved = save_reading(rec)
            print("Saved reading (state={}): {}".format(state_v, saved))
            if waiting_for_sensor:
                sensor_payload = j
                waiting_for_sensor = False
    except Exception as e:
        print("mqtt_callback error:", e)

# ---------- MQTT connect & trigger ----------
def mqtt_connect():
    global client
    client = MQTTClient(CLIENT_ID, MQTT_BROKER, port=MQTT_PORT)
    client.set_callback(mqtt_callback)
    client.connect()
    client.subscribe(TOPIC_DATA)
    print("MQTT connected, subscribed to", TOPIC_DATA)

def publish_trigger():
    try:
        client.publish(TOPIC_CMD, b"snd/data")
        print("Published trigger to", TOPIC_CMD)
        return True
    except Exception as e:
        print("publish_trigger error:", e)
        return False

# ---------- HTTP server (serves files from /www and endpoints) ----------
def serve_forever():
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    print("HTTP server listening on", addr)
    while True:
        try:
            cl, remote = s.accept()
            cl_file = cl.makefile("rwb", 0)
            req_line = cl_file.readline()
            if not req_line:
                cl.close(); continue
            try:
                req_line = req_line.decode('utf-8')
            except:
                cl.close(); continue
            parts = req_line.split()
            if len(parts) < 2:
                cl.close(); continue
            method, path = parts[0], parts[1]
            headers = {}
            while True:
                line = cl_file.readline()
                if not line or line == b'\r\n':
                    break
                try:
                    sline = line.decode('utf-8').strip()
                    if ':' in sline:
                        k,v = sline.split(':',1)
                        headers[k.strip().lower()] = v.strip()
                except:
                    pass

            # Serve index
            if method == 'GET' and (path == '/' or path == '/index.html'):
                try:
                    with open(WWW_DIR + "/index.html", "r") as f:
                        html = f.read()
                except:
                    html = "<html><body><h3>Index not found</h3></body></html>"
                cl.send('HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nAccess-Control-Allow-Origin: *\r\n\r\n')
                cl.send(html)
                cl.close(); continue

            # GET /latest
            if method == 'GET' and path == '/latest':
                body = json.dumps(latest if latest else {"status":"no_data"})
                cl.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\n\r\n')
                cl.send(body)
                cl.close(); continue

            # GET /readings?state=StateName
            if method == 'GET' and path.startswith('/readings'):
                state = None
                if '?' in path:
                    q = path.split('?',1)[1]
                    for part in q.split('&'):
                        if part.startswith('state='):
                            state = part.split('=',1)[1].replace('+',' ')
                            break
                arr = read_readings_by_state(state)
                cl.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\n\r\n')
                cl.send(json.dumps(arr))
                cl.close(); continue

            # POST /predict
            if method == 'POST' and path == '/predict':
                content_length = int(headers.get('content-length','0'))
                body = b''
                if content_length:
                    body = cl_file.read(content_length)
                try:
                    payload = json.loads(body.decode('utf-8')) if body else {}
                    tds = payload.get('tds'); ph = payload.get('ph'); turb = payload.get('turbidity') or payload.get('turb')
                    wqi = predict_wqi(tds, ph, turb)
                    cl.send('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\n\r\n')
                    cl.send(json.dumps({"wqi": wqi}))
                except Exception as e:
                    cl.send('HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nAccess-Control-Allow-Origin: *\r\n\r\n')
                    cl.send(json.dumps({"error": str(e)}))
                cl.close(); continue

            # default 404
            cl.send('HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nAccess-Control-Allow-Origin: *\r\n\r\n')
            cl.send('Not found')
            cl.close()

        except Exception as e:
            print("HTTP server error:", e)
            try:
                cl.close()
            except:
                pass

# ---------- WiFi ----------
wlan = network.WLAN(network.STA_IF)
def connect_wifi(timeout=20):
    wlan.active(True)
    if not wlan.isconnected():
        wlan.connect(WIFI_SSID, WIFI_PASS)
        t0 = time.time()
        while not wlan.isconnected() and (time.time() - t0) < timeout:
            time.sleep(0.5)
    if wlan.isconnected():
        print("WiFi IP:", wlan.ifconfig()[0])
        return True
    print("WiFi connect failed")
    return False

# ---------- main ----------
def main():
    connect_wifi()
    # attempt NTP sync so daily 06:00 is accurate
    synced = sync_time_with_ntp = False
    try:
        synced = sync_time_with_ntp = sync_time_with_ntp = sync_time_with_ntp  # placeholder to avoid lint; overwritten below
    except:
        pass
    # call the real sync
    synced = False
    try:
        synced = sync_time_with_ntp = (lambda: sync_time_with_ntp)()  # we'll replace this below

    except:
        pass
    # The above two lines are a no-op; below we call the real function:
    synced = sync_time_with_ntp() if 'sync_time_with_ntp' in globals() else False

    # Connect to MQTT
    try:
        mqtt_connect()
    except Exception as e:
        print("Initial MQTT connect failed:", e)

    # Start HTTP server thread
    try:
        _thread.start_new_thread(serve_forever, ())
    except Exception as e:
        print("Could not start HTTP server thread:", e)

    # Ensure mapping for typical sensor device (prototype)
    ensure_default_device_mapping("ESP32_sensor_001", default_state="Uttar Pradesh")

    # --- Automatic trigger AT BOOT ---
    # Wait briefly for MQTT to be fully ready, then publish startup trigger.
    time.sleep(1.0)
    try:
        published = publish_trigger()
        if published:
            # record last daily trigger date as today so we don't re-trigger at 06:00 the same day
            last_daily_trigger_date = local_date_tuple()
            print("Startup trigger published. last_daily_trigger_date set to", last_daily_trigger_date)
        else:
            last_daily_trigger_date = None
    except Exception as e:
        print("Startup trigger failed:", e)
        last_daily_trigger_date = None

    # Main loop: process MQTT messages and check for daily 06:00 trigger
    while True:
        try:
            # process incoming MQTT messages
            try:
                client.check_msg()
            except Exception:
                try:
                    mqtt_connect()
                except:
                    pass

            # check local time for daily trigger at 06:00
            try:
                lt = local_time_tuple()
                today = (lt[0], lt[1], lt[2])  # year,month,day
                hour = lt[3]; minute = lt[4]
                # trigger at exactly 06:00 once per local date
                if hour == 6 and minute == 0:
                    if 'last_daily_trigger_date' not in globals() or last_daily_trigger_date != today:
                        print("Local time is 06:00 — publishing daily trigger.")
                        try:
                            if publish_trigger():
                                last_daily_trigger_date = today
                                print("Daily trigger published. last_daily_trigger_date =", last_daily_trigger_date)
                        except Exception as e:
                            print("Daily trigger publish error:", e)
                # No else; keep processing
            except Exception as e:
                print("Time check error (maybe RTC not set):", e)

            time.sleep(1.0)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(1.0)

if __name__ == "__main__":
    main()
USE_MODEL = False
try:
    import model_coef
    # model_coef must define: b, c_tds, c_ph, c_turbidity
    if all(hasattr(model_coef, x) for x in ("b", "c_tds", "c_ph", "c_turbidity")):
        USE_MODEL = True
        print("model_coef imported: using linear coefficients for inference.")
    else:
        print("model_coef found but missing coefficients; falling back.")
except Exception as e:
    USE_MODEL = False
    # no model coefficients available; we'll try wqi_calc fallback
    print("No model_coef.py available; using fallback formula if present.")

# ---------- Try to import fallback deterministic WQI function ----------
compute_wqi_from_minimal = None
try:
    from wqi_calc import compute_wqi_from_minimal
    print("wqi_calc imported (fallback deterministic WQI available).")
except Exception:
    compute_wqi_from_minimal = None
    print("No wqi_calc.py found; final fallback heuristic will be used if needed.")

# ---------- Prediction helpers ----------
def model_predict_from_coef(tds, ph, turbidity):
    """Use coefficients from model_coef.py"""
    try:
        tds = float(tds); ph = float(ph); turbidity = float(turbidity)
    except:
        return None
    wqi = float(model_coef.b) + float(model_coef.c_tds)*tds + float(model_coef.c_ph)*ph + float(model_coef.c_turbidity)*turbidity
    # clip to 0-100
    if wqi < 0: wqi = 0.0
    if wqi > 100: wqi = 100.0
    return round(wqi, 2)

def predict_wqi(tds, ph, turbidity):
    """Main inference function used by the server."""
    if USE_MODEL:
        try:
            res = model_predict_from_coef(tds, ph, turbidity)
            if res is not None:
                return res
        except Exception as e:
            # if model fails, continue to fallback
            print("model_coef inference failed:", e)

    if compute_wqi_from_minimal:
        try:
            return compute_wqi_from_minimal(tds, ph, turbidity)[0]
        except Exception as e:
            print("wqi_calc failed:", e)

    # Last-resort trivial heuristic
    try:
        tds = float(tds); ph = float(ph); turbidity = float(turbidity)
        wqi = 50.0 + (7.0 - abs(ph - 7.0))*2.0 - (tds/600.0)*5.0 - (turbidity/15.0)*8.0
        if wqi < 0: wqi = 0.0
        if wqi > 100: wqi = 100.0
        return round(wqi, 2)
    except:
        return None

# ---------- storage ----------
def save_record(rec):
    try:
        with open(DATA_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print("save_record error:", e)

# ---------- WiFi ----------
wlan = network.WLAN(network.STA_IF)
def connect_wifi():
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASS)
        timeout = time.time() + 20
        while not wlan.isconnected() and time.time() < timeout:
            time.sleep(0.5)
    if wlan.isconnected():
        print("WiFi IP:", wlan.ifconfig()[0])
        return True
    print("WiFi connection failed")
    return False

# ---------- MQTT ----------
client = None
latest = None
waiting_for_sensor = False
sensor_payload = None

def mqtt_cb(topic, msg):
    """Callback when sensor publishes reading to TOPIC_DATA"""
    global latest, waiting_for_sensor, sensor_payload
    try:
        print("MQTT in:", topic, msg)
        if topic == TOPIC_DATA:
            j = json.loads(msg)
            rd = j.get("readings", {})
            # try multiple possible key names to be robust
            tds = rd.get("tds_ppm") or rd.get("tds") or rd.get("tds_ppm") or rd.get("tds_ppm")
            ph = rd.get("ph")
            turbidity = rd.get("turbidity_pct") or rd.get("turbidity")
            wqi = predict_wqi(tds, ph, turbidity)
            rec = {
                "ts": int(time.time()),
                "device": j.get("device", "sensor"),
                "tds": tds,
                "ph": ph,
                "turbidity": turbidity,
                "wqi": wqi
            }
            latest = rec
            save_record(rec)
            # optionally publish inferred reading
            try:
                client.publish(TOPIC_INFER, json.dumps(rec))
            except Exception:
                pass
            if waiting_for_sensor:
                sensor_payload = j
                waiting_for_sensor = False
    except Exception as e:
        print("Error in mqtt_cb:", e)

def mqtt_connect():
    global client
    client = MQTTClient(CLIENT_ID, MQTT_BROKER, port=MQTT_PORT)
    client.set_callback(mqtt_cb)
    client.connect()
    client.subscribe(TOPIC_DATA)
    print("MQTT connected and subscribed to", TOPIC_DATA)

# ---------- HTTP server (simple) ----------
def serve_forever():
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    print("HTTP server listening on", addr)
    while True:
        try:
            cl, addr = s.accept()
            cl_file = cl.makefile("rwb", 0)
            req_line = cl_file.readline()
            if not req_line:
                cl.close(); continue
            req = req_line.decode()
            method, path, _ = req.split()
            # consume headers
            while True:
                h = cl_file.readline()
                if not h or h == b"\r\n":
                    break
            if method == "GET" and (path == "/" or path == "/index.html"):
                html = "<html><body><h3>Index not found</h3></body></html>"
                index_path = WWW_DIR + "/index.html"
                try:
                    if WWW_DIR in os.listdir():
                        with open(index_path, "r") as f:
                            html = f.read()
                except Exception:
                    pass
                cl.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                cl.send(html)
            elif method == "GET" and path == "/latest":
                payload = json.dumps(latest if latest else {"status":"no_data"})
                cl.send("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
                cl.send(payload)
            else:
                cl.send("HTTP/1.1 404 Not Found\r\n\r\n")
            cl.close()
        except Exception as e:
            print("HTTP error:", e)

# ---------- trigger ----------
def publish_trigger():
    try:
        client.publish(TOPIC_CMD, b"snd/data")
        print("Published trigger to", TOPIC_CMD)
        return True
    except Exception as e:
        print("Trigger publish failed:", e)
        return False

# ---------- startup / loop ----------
def main():
    connect_wifi()
    try:
        mqtt_connect()
    except Exception as e:
        print("Initial MQTT connect failed:", e)

    # start HTTP server in separate thread if available
    try:
        import _thread
        _thread.start_new_thread(serve_forever, ())
    except Exception as e:
        print("Could not start HTTP server thread:", e)

    # trigger once at startup
    try:
        publish_trigger()
    except:
        pass

    last_trigger = time.time()
    global waiting_for_sensor, sensor_payload
    while True:
        try:
            # process incoming MQTT messages
            try:
                client.check_msg()
            except Exception:
                try:
                    mqtt_connect()
                except:
                    pass

            # periodic trigger
            now = time.time()
            if now - last_trigger >= TRIGGER_INTERVAL:
                waiting_for_sensor = True
                sensor_payload = None
                publish_trigger()
                # wait for sensor response with timeout
                wait_until = time.time() + SENSOR_RESPONSE_TIMEOUT
                while waiting_for_sensor and time.time() < wait_until:
                    try:
                        client.check_msg()
                    except:
                        pass
                    time.sleep(0.1)
                if sensor_payload is None:
                    print("No sensor response within timeout")
                last_trigger = now

            time.sleep(0.5)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(1)

if __name__ == "__main__":
    main()

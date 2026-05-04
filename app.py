import serial
import time
import threading
import re
import binascii
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'sms_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")

# ── Cấu hình modem ──────────────────────────────────────────
MODEM_PORT = '/dev/ttyUSB2'
BAUD_RATE  = 115200
TIMEOUT    = 5

messages = []
modem_status = {'connected': False, 'info': ''}
serial_lock = threading.Lock()
ser = None


# ── UCS2 helpers ────────────────────────────────────────────
def text_to_ucs2(text):
    """Chuyển chuỗi Python → chuỗi hex UCS2 (big-endian)."""
    return text.encode('utf-16-be').hex().upper()

def ucs2_to_text(hex_str):
    """Chuyển chuỗi hex UCS2 → chuỗi Python."""
    hex_str = re.sub(r'[^0-9A-Fa-f]', '', hex_str.strip())
    if not hex_str or len(hex_str) % 4 != 0:
        return hex_str
    try:
        return bytes.fromhex(hex_str).decode('utf-16-be', errors='replace')
    except Exception:
        return hex_str

def _try_ucs2(s):
    """Thử decode UCS2, nếu không được trả nguyên."""
    s = s.strip()
    clean = re.sub(r'[^0-9A-Fa-f]', '', s)
    if len(clean) >= 4 and len(clean) % 4 == 0:
        try:
            decoded = bytes.fromhex(clean).decode('utf-16-be', errors='replace')
            if any(c.isprintable() for c in decoded):
                return decoded
        except Exception:
            pass
    return s


# ── Hàm AT command cơ bản ───────────────────────────────────
def open_modem():
    global ser
    try:
        ser = serial.Serial(MODEM_PORT, BAUD_RATE, timeout=TIMEOUT)
        time.sleep(1)
        ser.flushInput()
        return True
    except Exception as e:
        print(f"[MODEM] Không mở được cổng: {e}")
        return False

def send_at(command, wait=1):
    global ser
    with serial_lock:
        try:
            if ser is None or not ser.is_open:
                if not open_modem():
                    return None, "Không kết nối được modem"
            ser.flushInput()
            ser.write((command + '\r\n').encode())
            time.sleep(wait)
            response = b''
            deadline = time.time() + wait + 1
            while time.time() < deadline:
                if ser.in_waiting:
                    response += ser.read(ser.in_waiting)
                    time.sleep(0.1)
                else:
                    break
            return response.decode('utf-8', errors='replace').strip(), None
        except Exception as e:
            return None, str(e)

def check_modem():
    resp, err = send_at('AT', wait=1)
    if err:
        return False, err
    if resp and 'OK' in resp:
        model, _  = send_at('AT+CGMM', wait=1)
        imei, _   = send_at('AT+CGSN', wait=1)
        signal, _ = send_at('AT+CSQ',  wait=1)
        info = (f"Model: {parse_single(model)} | "
                f"IMEI: {parse_single(imei)} | "
                f"Signal: {parse_csq(signal)}")
        return True, info
    return False, "Modem không phản hồi"

def parse_single(resp):
    if not resp:
        return 'N/A'
    lines = [l.strip() for l in resp.splitlines()
             if l.strip() and l.strip() not in ('OK', 'ERROR')]
    return lines[0] if lines else 'N/A'

def parse_csq(resp):
    if not resp:
        return 'N/A'
    m = re.search(r'\+CSQ:\s*(\d+)', resp or '')
    if m:
        val  = int(m.group(1))
        if val == 99:
            return 'Không có tín hiệu'
        dbm  = -113 + val * 2
        bars = '▁▃▅▇'[min(val // 8, 3)]
        return f"{bars} {dbm} dBm (rssi={val})"
    return 'N/A'


# ── Gửi SMS (UCS2) ──────────────────────────────────────────
def send_sms(phone, text):
    r, e = send_at('AT+CMGF=1', wait=0.5)
    if e:
        return False, e
    send_at('AT+CSCS="UCS2"', wait=0.5)

    phone_hex = text_to_ucs2(phone)
    text_hex  = text_to_ucs2(text)

    global ser
    with serial_lock:
        try:
            ser.write((f'AT+CMGS="{phone_hex}"\r').encode())
            time.sleep(1.5)
            resp = b''
            deadline = time.time() + 3
            while time.time() < deadline:
                if ser.in_waiting:
                    resp += ser.read(ser.in_waiting)
                    time.sleep(0.1)
                else:
                    break
            resp_str = resp.decode('latin-1', errors='replace')
            if '>' not in resp_str:
                return False, f"Modem không trả về dấu nhắc '>': {resp_str!r}"

            ser.write((text_hex + chr(26)).encode())
            final = b''
            deadline = time.time() + 15
            while time.time() < deadline:
                if ser.in_waiting:
                    final += ser.read(ser.in_waiting)
                    decoded = final.decode('latin-1', errors='replace')
                    if 'OK' in decoded or 'ERROR' in decoded or '+CMGS' in decoded:
                        break
                time.sleep(0.3)
            final_str = final.decode('latin-1', errors='replace').strip()
            if '+CMGS' in final_str or 'OK' in final_str:
                return True, final_str
            return False, final_str or "Hết thời gian chờ"
        except Exception as ex:
            return False, str(ex)


# ── Đọc SMS từ SIM (UCS2) ───────────────────────────────────
def read_sms_all():
    send_at('AT+CMGF=1', wait=0.5)
    send_at('AT+CSCS="UCS2"', wait=0.5)
    resp, err = send_at('AT+CMGL="ALL"', wait=4)
    if err:
        return [], err
    return parse_sms_list(resp), None

def parse_sms_list(resp):
    if not resp:
        return []
    result = []
    lines  = resp.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r'\+CMGL:\s*(\d+),"([^"]*)","([^"]*)"[^"]*,"([^"]*)"', line)
        if m:
            idx        = m.group(1)
            status     = m.group(2)
            sender_raw = m.group(3)
            dt_raw     = m.group(4)
            sender     = _try_ucs2(sender_raw)
            dt         = _try_ucs2(dt_raw)
            body_parts = []
            i += 1
            while i < len(lines):
                l = lines[i].strip()
                if l.startswith('+CMGL:') or l in ('OK', 'ERROR'):
                    break
                if l:
                    body_parts.append(l)
                i += 1
            body_hex = ''.join(body_parts)
            body     = _try_ucs2(body_hex)
            result.append({'id': idx, 'status': status, 'from': sender, 'date': dt, 'body': body})
        else:
            i += 1
    return result


# ── Xóa SMS ─────────────────────────────────────────────────
def delete_sms(idx):
    resp, err = send_at(f'AT+CMGD={idx}', wait=2)
    if err:
        return False, err
    return 'OK' in (resp or ''), resp

def delete_sms_multi(indices):
    results = {}
    for idx in indices:
        ok, detail = delete_sms(idx)
        results[str(idx)] = {'ok': ok, 'detail': detail}
        time.sleep(0.3)
    return results

def delete_sms_all():
    resp, err = send_at('AT+CMGD=1,4', wait=5)
    if err:
        return False, err
    return 'OK' in (resp or ''), resp


# ── Polling SMS mới ──────────────────────────────────────────
known_ids = set()

def sms_poller():
    global known_ids
    while True:
        try:
            sms_list, _ = read_sms_all()
            for sms in sms_list:
                if sms['id'] not in known_ids:
                    known_ids.add(sms['id'])
                    sms['direction'] = 'in'
                    sms['timestamp'] = datetime.now().isoformat()
                    messages.append(sms)
                    socketio.emit('new_sms', sms)
        except Exception as e:
            print(f"[POLLER] {e}")
        time.sleep(10)


# ── Flask routes ─────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def api_status():
    ok, info = check_modem()
    modem_status['connected'] = ok
    modem_status['info'] = info
    return jsonify({'connected': ok, 'info': info})

@app.route('/api/send', methods=['POST'])
def api_send():
    data  = request.get_json()
    phone = (data.get('phone') or '').strip()
    text  = (data.get('text')  or '').strip()
    if not phone or not text:
        return jsonify({'ok': False, 'error': 'Thiếu số điện thoại hoặc nội dung'})
    ok, detail = send_sms(phone, text)
    if ok:
        msg = {
            'id': f'out_{int(time.time())}', 'direction': 'out',
            'to': phone, 'from': 'Tôi', 'body': text,
            'date': datetime.now().strftime('%y/%m/%d,%H:%M:%S'),
            'timestamp': datetime.now().isoformat(), 'status': 'SND SENT'
        }
        messages.append(msg)
        socketio.emit('new_sms', msg)
    return jsonify({'ok': ok, 'detail': detail})

@app.route('/api/messages')
def api_messages():
    return jsonify(messages)

@app.route('/api/inbox')
def api_inbox():
    sms_list, err = read_sms_all()
    if err:
        return jsonify({'ok': False, 'error': err})
    return jsonify({'ok': True, 'messages': sms_list})

@app.route('/api/delete/<idx>', methods=['DELETE'])
def api_delete_one(idx):
    ok, detail = delete_sms(idx)
    return jsonify({'ok': ok, 'detail': detail})

@app.route('/api/delete-multi', methods=['POST'])
def api_delete_multi():
    data    = request.get_json()
    indices = data.get('indices', [])
    if not indices:
        return jsonify({'ok': False, 'error': 'Không có index nào được chọn'})
    results = delete_sms_multi(indices)
    all_ok  = all(v['ok'] for v in results.values())
    return jsonify({'ok': all_ok, 'results': results})

@app.route('/api/delete-all', methods=['DELETE'])
def api_delete_all():
    ok, detail = delete_sms_all()
    return jsonify({'ok': ok, 'detail': detail})

@app.route('/api/at', methods=['POST'])
def api_at():
    data = request.get_json()
    cmd  = (data.get('cmd')  or '').strip()
    wait = float(data.get('wait', 1))
    if not cmd:
        return jsonify({'ok': False, 'error': 'Thiếu lệnh'})
    resp, err = send_at(cmd, wait=wait)
    return jsonify({'ok': err is None, 'response': resp, 'error': err})

@socketio.on('connect')
def on_connect():
    emit('history', messages)

if __name__ == '__main__':
    t = threading.Thread(target=sms_poller, daemon=True)
    t.start()
    print("[INFO] SMS Gateway đang chạy tại http://0.0.0.0:5000")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False)

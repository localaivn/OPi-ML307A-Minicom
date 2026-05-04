import serial
import time
import threading
import re
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


# ══════════════════════════════════════════════════════════════
# UCS2 helpers
# ══════════════════════════════════════════════════════════════

def text_to_ucs2(text: str) -> str:
    """Python string → uppercase hex UCS2-BE (dùng khi GỬI)."""
    return text.encode('utf-16-be').hex().upper()


def _is_pure_hex(s: str) -> bool:
    """Kiểm tra chuỗi có phải toàn ký tự hex không (0-9, A-F)."""
    return bool(s) and all(c in '0123456789ABCDEFabcdef' for c in s)


def decode_ucs2_body(raw: str) -> str:
    """
    Decode body/sender từ modem UCS2.

    Modem trả về chuỗi hex thuần (chỉ có 0-9 A-F).
    Số điện thoại như '+84901234567' KHÔNG phải hex → trả nguyên.
    Nếu độ dài không chia hết cho 4 → không phải UCS2 → trả nguyên.
    """
    raw = raw.strip()
    if not raw:
        return raw

    # Lấy phần hex sạch
    clean = re.sub(r'[^0-9A-Fa-f]', '', raw)

    # Điều kiện cần: chuỗi gốc phải là hex thuần (không có +, dấu, khoảng trắng...)
    # Và độ dài phải là bội của 4 (mỗi ký tự UCS2 = 4 hex)
    if not _is_pure_hex(raw.replace(' ', '')) or len(clean) % 4 != 0 or len(clean) < 4:
        return raw  # không phải UCS2 hex → trả nguyên

    try:
        decoded = bytes.fromhex(clean).decode('utf-16-be', errors='replace')
        # Sanity check: nếu > 50% ký tự bị replace (U+FFFD) → không đúng
        if decoded.count('\ufffd') > len(decoded) * 0.5:
            return raw
        return decoded
    except Exception:
        return raw


# ══════════════════════════════════════════════════════════════
# AT command layer
# ══════════════════════════════════════════════════════════════

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


def send_at(command: str, wait: float = 1) -> tuple:
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
            deadline = time.time() + wait + 1.5
            while time.time() < deadline:
                if ser.in_waiting:
                    response += ser.read(ser.in_waiting)
                    time.sleep(0.1)
                else:
                    break
            return response.decode('latin-1', errors='replace').strip(), None
        except Exception as e:
            return None, str(e)


def check_modem():
    resp, err = send_at('AT', wait=1)
    if err:
        return False, err
    if resp and 'OK' in resp:
        model, _   = send_at('AT+CGMM', wait=1)
        imei, _    = send_at('AT+CGSN', wait=1)
        signal, _  = send_at('AT+CSQ',  wait=1)
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


# ══════════════════════════════════════════════════════════════
# Gửi SMS (UCS2 text mode)
# ══════════════════════════════════════════════════════════════


def build_pdu(phone: str, text: str):
    """
    Tạo PDU SMS dạng UCS2 (hỗ trợ tiếng Việt đầy đủ).
    Trả về (pdu_hex_string, tpdu_length).
    """
    smsc = '00'  # dùng SMSC mặc định trong SIM

    # Encode số điện thoại → BCD semi-octets
    if phone.startswith('+'):
        phone_digits = phone[1:]
        phone_type   = '91'  # international
    else:
        phone_digits = phone
        phone_type   = '81'  # national

    padded    = phone_digits if len(phone_digits) % 2 == 0 else phone_digits + 'F'
    phone_bcd = ''.join(padded[i+1] + padded[i] for i in range(0, len(padded), 2))
    phone_len = f'{len(phone_digits):02X}'

    da = phone_len + phone_type + phone_bcd

    # Encode nội dung UCS2
    ud_bytes = text.encode('utf-16-be')
    ud_hex   = ud_bytes.hex().upper()
    udl      = f'{len(ud_bytes):02X}'  # User Data Length = số byte

    # PDU body: SMS-SUBMIT(01) + MR(00) + DA + PID(00) + DCS(08=UCS2) + UDL + UD
    pdu_body = '01' + '00' + da + '00' + '08' + udl + ud_hex
    tpdu_len = len(pdu_body) // 2
    return smsc + pdu_body, tpdu_len


def send_sms(phone: str, text: str):
    """
    Gửi SMS qua PDU mode — đảm bảo UCS2/tiếng Việt đúng 100%.
    Text mode với AT+CSCS="UCS2" bị lỗi vì modem reset encoding sau '>'.
    PDU mode không phụ thuộc vào AT+CSCS nên luôn hoạt động đúng.
    """
    r, e = send_at('AT+CMGF=0', wait=0.5)
    if e:
        return False, e

    try:
        pdu_str, tpdu_len = build_pdu(phone, text)
    except Exception as ex:
        return False, f"Lỗi tạo PDU: {ex}"

    global ser
    with serial_lock:
        try:
            ser.write((f'AT+CMGS={tpdu_len}\r').encode())
            time.sleep(1.5)

            resp = b''
            deadline = time.time() + 4
            while time.time() < deadline:
                if ser.in_waiting:
                    resp += ser.read(ser.in_waiting)
                    time.sleep(0.1)
                else:
                    break
            resp_str = resp.decode('latin-1', errors='replace')

            if '>' not in resp_str:
                return False, f"Modem không trả về dấu nhắc '>': {resp_str!r}"

            # Gửi PDU hex + Ctrl+Z
            ser.write((pdu_str + chr(26)).encode())

            final    = b''
            deadline = time.time() + 15
            while time.time() < deadline:
                if ser.in_waiting:
                    final  += ser.read(ser.in_waiting)
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

# ══════════════════════════════════════════════════════════════
# Đọc SMS từ SIM
# ══════════════════════════════════════════════════════════════

def read_sms_all():
    send_at('AT+CMGF=1', wait=0.5)
    send_at('AT+CSCS="UCS2"', wait=0.5)
    resp, err = send_at('AT+CMGL="ALL"', wait=5)
    if err:
        return [], err
    return parse_sms_list(resp), None


def parse_sms_list(resp: str) -> list:
    """
    Parse output AT+CMGL="ALL" với UCS2.

    Header:  +CMGL: <idx>,"<status>","<sender_ucs2>"[,<alpha>],"<date>"
    Body:    <body_ucs2_hex>   (có thể nhiều dòng với tin dài)
    """
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

            # sender: số điện thoại thường không phải UCS2 hex → decode_ucs2_body xử lý đúng
            sender = decode_ucs2_body(sender_raw)
            dt     = dt_raw  # date luôn là ASCII, không cần decode

            # Thu thập body (có thể nhiều dòng)
            body_parts = []
            i += 1
            while i < len(lines):
                l = lines[i].strip()
                if l.startswith('+CMGL:') or l in ('OK', 'ERROR'):
                    break
                if l:
                    body_parts.append(l)
                i += 1

            # Ghép các phần hex lại rồi decode một lần
            body_hex = ''.join(body_parts)
            body     = decode_ucs2_body(body_hex)

            result.append({
                'id':     idx,
                'status': status,
                'from':   sender,
                'date':   dt,
                'body':   body,
            })
        else:
            i += 1
    return result


# ══════════════════════════════════════════════════════════════
# Xóa SMS
# ══════════════════════════════════════════════════════════════

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
    """AT+CMGD=1,4 → xóa toàn bộ (delflag=4)."""
    resp, err = send_at('AT+CMGD=1,4', wait=6)
    if err:
        return False, err
    return 'OK' in (resp or ''), resp


# ══════════════════════════════════════════════════════════════
# Polling SMS mới
# ══════════════════════════════════════════════════════════════
known_ids: set = set()


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


# ══════════════════════════════════════════════════════════════
# Flask routes
# ══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    ok, info = check_modem()
    modem_status.update({'connected': ok, 'info': info})
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
            'timestamp': datetime.now().isoformat(), 'status': 'SND SENT',
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


# ── Xóa SMS endpoints ────────────────────────────────────────

@app.route('/api/delete/<idx>', methods=['DELETE'])
def api_delete_one(idx):
    ok, detail = delete_sms(idx)
    if ok:
        # Xóa khỏi feed và known_ids
        _remove_from_feed_by_sim_id(idx)
    return jsonify({'ok': ok, 'detail': detail})


@app.route('/api/delete-multi', methods=['POST'])
def api_delete_multi():
    data    = request.get_json()
    indices = data.get('indices', [])
    if not indices:
        return jsonify({'ok': False, 'error': 'Không có index nào được chọn'})
    results = delete_sms_multi(indices)
    # Xóa những cái thành công khỏi feed
    for idx, v in results.items():
        if v['ok']:
            _remove_from_feed_by_sim_id(idx)
    all_ok = all(v['ok'] for v in results.values())
    return jsonify({'ok': all_ok, 'results': results})


@app.route('/api/delete-all', methods=['DELETE'])
def api_delete_all():
    ok, detail = delete_sms_all()
    if ok:
        # Xóa toàn bộ tin nhắn đến trong feed + reset known_ids
        _clear_incoming_feed()
        socketio.emit('feed_cleared')  # thông báo tất cả clients
    return jsonify({'ok': ok, 'detail': detail})


@app.route('/api/clear-feed', methods=['DELETE'])
def api_clear_feed():
    """Xóa feed trên giao diện (không xóa SIM)."""
    _clear_incoming_feed()
    socketio.emit('feed_cleared')
    return jsonify({'ok': True})


def _remove_from_feed_by_sim_id(sim_id: str):
    """Xóa tin nhắn khỏi feed và known_ids theo SIM index."""
    global messages, known_ids
    messages = [m for m in messages if m.get('id') != sim_id]
    known_ids.discard(sim_id)
    socketio.emit('remove_msg', {'id': sim_id})


def _clear_incoming_feed():
    """Xóa tất cả tin đến khỏi feed + reset known_ids."""
    global messages, known_ids
    messages  = [m for m in messages if m.get('direction') == 'out']
    known_ids = set()


# ── AT Console ───────────────────────────────────────────────

@app.route('/api/at', methods=['POST'])
def api_at():
    data = request.get_json()
    cmd  = (data.get('cmd')  or '').strip()
    wait = float(data.get('wait', 1))
    if not cmd:
        return jsonify({'ok': False, 'error': 'Thiếu lệnh'})
    resp, err = send_at(cmd, wait=wait)
    return jsonify({'ok': err is None, 'response': resp, 'error': err})


# ── SocketIO ─────────────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    emit('history', messages)


# ── Main ─────────────────────────────────────────────────────

if __name__ == '__main__':
    t = threading.Thread(target=sms_poller, daemon=True)
    t.start()
    print("[INFO] SMS Gateway đang chạy tại http://0.0.0.0:5001")
    socketio.run(app, host='0.0.0.0', port=5001, debug=False)

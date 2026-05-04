# SMS Gateway — AT Command

Ứng dụng web gửi/nhận SMS qua modem GSM sử dụng AT command.

## Cài đặt

```bash
git clone https://github.com/localaivn/OPi-ML307A-Minicom
cd OPi-ML307A-Minicom

# Tạo virtualenv (khuyến nghị)
python3 -m venv venv
source venv/bin/activate

# Cài thư viện
pip install -r requirements.txt
```

## Cấu hình

Mở `app.py`, chỉnh 2 dòng nếu cần:

```python
MODEM_PORT = '/dev/ttyUSB2'   # cổng serial của modem
BAUD_RATE  = 115200            # tốc độ baud
```

## Quyền truy cập cổng serial

```bash
sudo usermod -aG dialout $USER
# Đăng xuất rồi đăng nhập lại, hoặc dùng sudo khi chạy
```

## Chạy

```bash
python app.py
# Hoặc dùng sudo nếu chưa thêm user vào group dialout:
sudo python app.py
```

Mở trình duyệt tại: **http://localhost:5000**

## Tính năng

| Tính năng | Mô tả |
|-----------|-------|
| Gửi SMS | Nhập số điện thoại + nội dung, nhấn GỬI |
| Nhận SMS | Tự động polling mỗi 10 giây, hiển thị real-time qua WebSocket |
| SIM Inbox | Đọc tất cả tin nhắn đang lưu trên thẻ SIM |
| Xóa SMS | Xóa khỏi SIM hoặc khỏi luồng hiển thị |
| AT Console | Gửi AT command thủ công để debug |
| Thông báo | Push notification khi có SMS mới |

## API Endpoints

| Method | URL | Mô tả |
|--------|-----|-------|
| GET | `/api/status` | Kiểm tra kết nối modem |
| POST | `/api/send` | Gửi SMS `{phone, text}` |
| GET | `/api/inbox` | Đọc SMS từ SIM |
| DELETE | `/api/delete/<id>` | Xóa SMS khỏi SIM |
| POST | `/api/at` | Gửi AT command `{cmd, wait}` |

## Cấu trúc

```
OPi-ML307A-Minicom/
├── app.py              # Backend Flask + AT command
├── requirements.txt
├── README.md
└── templates/
    └── index.html      # Giao diện web
```

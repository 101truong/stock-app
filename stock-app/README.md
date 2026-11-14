# Ứng dụng báo cáo phân tích cổ phiếu

Nhập mã cổ phiếu, nhận báo cáo gồm: giá và biến động trong phiên, biểu đồ kỹ thuật ngắn hạn (SMA5, SMA20, RSI14),
P/E, EPS, tỷ suất cổ tức, vốn hóa, 3 tin đáng chú ý trong 24 giờ qua, điểm tâm lý thị trường và nhận định xu hướng.

## Nguồn dữ liệu
- Cổ phiếu Việt Nam (mã 3 chữ cái, ví dụ VPB, FPT): thư viện `vnstock`, nguồn VCI
- Cổ phiếu quốc tế (ví dụ AAPL, NVDA): `yfinance`
- Tin tức: Google News RSS (lọc 24 giờ)
- Tâm lý thị trường: Claude nếu có `ANTHROPIC_API_KEY`, nếu không thì quy tắc từ khóa và chỉ báo kỹ thuật

## Chạy trên máy
```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # điền ANTHROPIC_API_KEY nếu muốn phân tích bằng AI
uvicorn app:app --reload
```
Mở http://127.0.0.1:8000

## Triển khai
Dùng được trên Render, Railway, Fly.io hoặc VPS bất kỳ có Python 3.10+.
Lệnh khởi động: `uvicorn app:app --host 0.0.0.0 --port $PORT`.
Đặt `ANTHROPIC_API_KEY` trong phần biến môi trường của dịch vụ, không đưa vào mã nguồn.

## Lưu ý
- Dữ liệu từ nguồn công khai, có thể trễ vài phút. Đây là bản làm mới theo yêu cầu (nút "Tự làm mới mỗi 60 giây"), không phải luồng dữ liệu từng giây.
- Cấu trúc dữ liệu của `vnstock` thay đổi theo phiên bản. Nếu P/E, EPS, cổ tức hoặc vốn hóa của cổ phiếu Việt Nam hiện dấu "—",
  hãy chỉnh hàm `fetch_vn` trong `app.py` cho khớp tên cột của phiên bản bạn cài.
- Phần tóm tắt tin chỉ dựa trên tiêu đề, không đọc toàn văn bài báo.
- Chỉ mang tính tham khảo, không phải khuyến nghị đầu tư.

# Kairozen KHQR API

KHQR payment API platform — users register, get API key, use it to generate QR in their bots.

## Deploy to Render

1. Push folder to GitHub repo
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
   - **Environment:** Python 3
5. Add env var: `SECRET_KEY` = any random string
6. Deploy → Done ✅

## API Endpoints

All endpoints require header: `x-api-key: kz_xxxxx`

### POST /api/v1/create-qr
```json
{
  "amount": 5000,
  "currency": "KHR",
  "note": "Order #1"
}
```

### GET /api/v1/check-payment
```
?md5=xxxx
or
?transaction_id=xxxx
```

### GET /api/v1/info
Returns shop info + total_paid

## Usage in Telegram Bot

```python
import telebot, requests

bot = telebot.TeleBot("TOKEN")
HEADERS = {"x-api-key": "kz_your_key"}
BASE = "https://your-app.onrender.com"

@bot.message_handler(commands=["pay"])
def pay(msg):
    r = requests.post(f"{BASE}/api/v1/create-qr",
        headers=HEADERS,
        json={"amount": 5000, "currency": "KHR"})
    qr = r.json()["data"]["qrString"]
    bot.send_message(msg.chat.id, f"📱 QR:\n`{qr}`", parse_mode="Markdown")

bot.polling()
```

Playwright версия бота.

В GitHub заменить:
- main.py
- requirements.txt

В Render поставить Build Command:
pip install -r requirements.txt && playwright install chromium

Start Command:
python main.py

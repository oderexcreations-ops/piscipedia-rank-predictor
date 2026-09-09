# Piscipedia — ICAR AIEEA PG Fisheries Rank Predictor

Full-stack Flask application with SQLite, PDF ingestion, dataset versioning, candidate submissions, rank prediction, admin dashboard, verified-result feedback, and email notifications.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open http://127.0.0.1:5000

Default admin credentials are configured by `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `.env`; change them before deployment.

The supplied 2026 Piscipedia PDF has been seeded into `seed_data.py` as Dataset v1. The PDF can also be uploaded from the Admin dashboard to create a new version.

## Production

Use a production WSGI server such as Gunicorn behind HTTPS/reverse proxy. Set a strong `SECRET_KEY`, admin credentials, and SMTP credentials. For multi-instance deployments, replace SQLite with PostgreSQL via the repository layer.

The supplied source PDF is unofficial and its own note says actual ranks may differ depending on the complete candidate population and official ICAR/NTA evaluation.


## Render deployment
This project is intentionally self-contained. The existing Render service can start it with `python app.py`; the application binds to `0.0.0.0` and uses Render's `PORT`. If the service Start Command is changed, use `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app`.

IMPORTANT: upload the contents of this project to the repository root. The `templates/` and `static/` folders must be uploaded with their files. Do not upload only `app.py`.

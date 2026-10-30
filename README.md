Cloud Storage — Flask + PostgreSQL File Hosting
A self-hosted cloud storage web app built with Flask, PostgreSQL (Neon-compatible), and a single-file architecture. Includes a public upload/download interface and a password-protected admin panel with full site-content editing.

✨ Features
Public file hosting — drag-and-drop or click-to-upload with live progress bar

1 GB max upload size per file (configurable)

File type allowlist — 30+ extensions supported (docs, images, video, audio, archives, code)

Admin panel with:

File management (download / delete)

Live editing of all site text (title, subtitle, badges, footer, empty-state messages)

Password change for the current admin

Multi-admin support (add / remove admins)

PostgreSQL backend (works with Neon, Supabase, Railway, or local Postgres)

Session-based auth via Flask signed cookies

Auto-opens browser on startup (disable with NO_BROWSER=1)

Single-file app — no blueprints, no templating files, everything in one .py

📋 Requirements
Python 3.8+

A PostgreSQL database (Neon free tier works great)

pip packages:

text
flask
flask-cors
werkzeug
psycopg2-binary
python-dotenv
Install with:

bash
pip install flask flask-cors werkzeug psycopg2-binary python-dotenv
psycopg2-binary is used for portability. For production, prefer building psycopg2 against your system libpq.

🚀 Quick Start
1. Get a PostgreSQL database
The easiest option is Neon (free tier, serverless Postgres):

Create a project

Copy the connection string — it looks like:

text
postgresql://user:password@ep-xxx.aws.neon.tech/neondb?sslmode=require
The app automatically strips channel_binding and forces sslmode=require, so you can paste the connection string verbatim from the Neon dashboard.

2. Configure environment
Create a .env file in the project root:

env
# Required
DATABASE_URL=postgresql://user:password@host/dbname?sslmode=require

# Strongly recommended in production (sessions are forgeable if left default)
SECRET_KEY=change-me-to-a-long-random-string

# Optional
UPLOAD_FOLDER=uploads          # default: ./uploads
HOST=0.0.0.0                   # default: 0.0.0.0
PORT=5000                      # default: 5000
FLASK_DEBUG=0                  # default: 0
NO_BROWSER=0                   # set to 1 to prevent auto-opening the browser
Generate a strong secret key with:

bash
python -c "import secrets; print(secrets.token_hex(32))"
3. Run
bash
python app.py

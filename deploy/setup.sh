#!/usr/bin/env bash
# VoiceBill one-shot server setup for a fresh Ubuntu 24.04 droplet. Run as root:
#   DOMAIN=bills.example.com EMAIL=you@example.com bash setup.sh
# Optional: BACKEND_REPO, FRONTEND_REPO (git URLs; for private repos put a GitHub token in the URL).
# Safe to run again: it updates the code, rebuilds and restarts, and keeps your .env and database.
set -euo pipefail

: "${DOMAIN:?Set DOMAIN, e.g. DOMAIN=bills.example.com}"
: "${EMAIL:?Set EMAIL (for the free HTTPS certificate)}"
BACKEND_REPO="${BACKEND_REPO:-https://github.com/moizambilal71/voicebill-backend.git}"
FRONTEND_REPO="${FRONTEND_REPO:-https://github.com/moizambilal71/voicebill-frontend.git}"
APP=/opt/voicebill
export DEBIAN_FRONTEND=noninteractive

echo "==> 1/8 System packages"
apt-get update -y
apt-get install -y python3-venv python3-pip nginx git ufw curl ca-certificates certbot python3-certbot-nginx
if ! command -v node >/dev/null || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 22 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

echo "==> 2/8 Swap file (the frontend build needs memory)"
if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> 3/8 App user and code"
id voicebill >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin voicebill
mkdir -p "$APP" && chown voicebill:voicebill "$APP"
clone_or_pull() {  # url dir
  if [ -d "$2/.git" ]; then sudo -u voicebill git -C "$2" pull --ff-only
  else sudo -u voicebill git clone "$1" "$2"; fi
}
clone_or_pull "$BACKEND_REPO" "$APP/backend"
clone_or_pull "$FRONTEND_REPO" "$APP/frontend"

echo "==> 4/8 Backend"
cd "$APP/backend"
[ -d .venv ] || sudo -u voicebill python3 -m venv .venv
sudo -u voicebill .venv/bin/pip install -q -r requirements.txt
if [ ! -f .env ]; then
  SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")
  cat > .env <<ENV
DJANGO_SECRET_KEY=$SECRET
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=$DOMAIN,127.0.0.1,localhost
CORS_ALLOWED_ORIGINS=https://$DOMAIN
CSRF_TRUSTED_ORIGINS=https://$DOMAIN
DJANGO_SECURE_COOKIES=1
DJANGO_HSTS_SECONDS=0
TRUSTED_PROXY_COUNT=2
FRONTEND_URL=https://$DOMAIN
ADMIN_BASE_URL=https://$DOMAIN
STOCK_MONITOR=0
# Fill these in (see the guide), then run: systemctl restart voicebill-api
OPENAI_API_KEY=
EMAIL_BACKEND=accounts.brevo.BrevoEmailBackend
BREVO_API_KEY=
DEFAULT_FROM_EMAIL=VoiceBill <your-verified-sender@example.com>
ADMIN_NOTIFY_EMAIL=
ADMIN_WHATSAPP_NUMBER=
GOOGLE_CLIENT_ID=
PRO_PRICE_LABEL=Rs 1,999 / month
ENV
  chown voicebill:voicebill .env && chmod 600 .env
fi
sudo -u voicebill .venv/bin/python manage.py migrate --noinput
sudo -u voicebill .venv/bin/python manage.py collectstatic --noinput

echo "==> 5/8 Frontend (the build takes a few minutes)"
cd "$APP/frontend"
sudo -u voicebill npm ci
sudo -u voicebill env BACKEND_URL=http://127.0.0.1:8000 npm run build

echo "==> 6/8 Background services"
cat > /etc/systemd/system/voicebill-api.service <<UNIT
[Unit]
Description=VoiceBill API (Django)
After=network.target
[Service]
User=voicebill
WorkingDirectory=$APP/backend
ExecStart=$APP/backend/.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 2 --timeout 120
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/voicebill-web.service <<UNIT
[Unit]
Description=VoiceBill website (Next.js)
After=network.target
[Service]
User=voicebill
WorkingDirectory=$APP/frontend
Environment=NODE_ENV=production
Environment=BACKEND_URL=http://127.0.0.1:8000
ExecStart=/usr/bin/npm run start -- -p 3000 -H 127.0.0.1
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/voicebill-monitor.service <<UNIT
[Unit]
Description=VoiceBill stock and plan checks
After=network.target
[Service]
User=voicebill
WorkingDirectory=$APP/backend
ExecStart=$APP/backend/.venv/bin/python manage.py check_stock --loop
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable voicebill-api voicebill-web voicebill-monitor >/dev/null
systemctl restart voicebill-api voicebill-web voicebill-monitor

echo "==> 7/8 Web server (nginx)"
cat > /etc/nginx/sites-available/voicebill <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 20m;
    proxy_read_timeout 120s;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;

    location ^~ /admin/  { proxy_pass http://127.0.0.1:8000; }
    location ^~ /static/ { proxy_pass http://127.0.0.1:8000; }
    location /           { proxy_pass http://127.0.0.1:3000; }
}
NGINX
ln -sf /etc/nginx/sites-available/voicebill /etc/nginx/sites-enabled/voicebill
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "==> 8/8 Firewall and HTTPS"
ufw allow OpenSSH >/dev/null; ufw allow 'Nginx Full' >/dev/null; ufw --force enable >/dev/null
if certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --no-eff-email --redirect --non-interactive; then
  echo "HTTPS is on."
else
  echo "!! HTTPS failed. Check that $DOMAIN points to this server's IP, then run:"
  echo "   certbot --nginx -d $DOMAIN -m $EMAIL --agree-tos --redirect"
fi

echo
echo "Done. Open https://$DOMAIN"
echo "Next: create your admin login:  cd $APP/backend && sudo -u voicebill .venv/bin/python manage.py createsuperuser"
echo "Then edit $APP/backend/.env (OpenAI, Brevo, Google keys) and run: systemctl restart voicebill-api"

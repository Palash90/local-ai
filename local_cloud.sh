#!/usr/bin/env bash
set -e

echo "=== 1. Setting permissions on external media drive ==="
# sudo chown -R www-data:www-data /mnt/wwn-0x50014ee2173893e0-part1/BackUp-Copy-2/
# sudo chmod -R 0750 /mnt/wwn-0x50014ee2173893e0-part1/BackUp-Copy-2/

echo "=== 2. Creating Dashboard HTML ==="
sudo mkdir -p /var/www/dashboard
cat << 'EOF' | sudo tee /var/www/dashboard/index.html > /dev/null
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Homeserver Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0d1117; color: #c9d1d9; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; width: 100%; max-width: 900px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 24px; text-decoration: none; color: inherit; transition: transform 0.2s, border-color 0.2s; text-align: center; }
        .card:hover { transform: translateY(-4px); border-color: #58a6ff; }
        .icon { font-size: 40px; margin-bottom: 12px; }
        .title { font-size: 18px; font-weight: 600; color: #58a6ff; margin-bottom: 6px; }
        .desc { font-size: 13px; color: #8b949e; }
    </style>
</head>
<body>
    <div class="grid">
        <a href="/ai/" class="card">
            <div class="icon">🤖</div>
            <div class="title">Local AI</div>
            <div class="desc">/ai/</div>
        </a>
        <a href="/stories/" class="card">
            <div class="icon">📖</div>
            <div class="title">AI Generated Stories</div>
            <div class="desc">/stories/</div>
        </a>
        <a href="/search/" class="card">
            <div class="icon">🔍</div>
            <div class="title">Search Engine</div>
            <div class="desc">/search/</div>
        </a>
        <a href="/cloud/" class="card">
            <div class="icon">☁️</div>
            <div class="title">Nextcloud</div>
            <div class="desc">/cloud/</div>
        </a>
        <a href="/cloud/apps/files/files/136?dir=/Media/Public/E-books/" class="card">
            <div class="icon">📚</div>
            <div class="title">Books</div>
            <div class="desc">E-books</div>
        </a>
        <a href="/code/" class="card">
            <div class="icon">💻</div>
            <div class="title">Code Hoster</div>
            <div class="desc">/code/</div>
        </a>
        <a href="/track/" class="card">
            <div class="icon">📊</div>
            <div class="title">Request Tracker</div>
            <div class="desc">/track/</div>
        </a>
    </div>
</body>
</html>
EOF

sudo chown -R www-data:www-data /var/www/dashboard
sudo chmod -R 755 /var/www/dashboard

echo "=== 3. Writing Nginx Master Configuration ==="
cat << 'EOF' | sudo tee /etc/nginx/sites-available/homeserver > /dev/null
map $http_x_via_gcp $gcp_overlay {
    "true"  '<div id="gcp-overlay" style="position:fixed;top:0;left:0;width:100vw;height:100vh;background-color:rgba(239,68,68,0.05);border-top:3px solid rgba(239,68,68,0.6);pointer-events:none;z-index:999998;"></div>';
    default '';
}

map $http_user_agent $cloud_inject {
    "~*nextcloud-(android|ios|desktop)" '';
    default '$gcp_overlay<div id="global-nav-bar" style="position:absolute;bottom:16px;right:16px;z-index:999999;display:flex;gap:8px;background:rgba(22,27,34,0.95);padding:6px 12px;border-radius:20px;border:1px solid #30363d;box-shadow:0 4px 12px rgba(0,0,0,0.5);font-family:sans-serif;font-size:13px;backdrop-filter:blur(4px);"><a href="/" style="color:#58a6ff;text-decoration:none;font-weight:600;">Home</a><span style="color:#484f58;">|</span><a href="/ai/" style="color:#c9d1d9;text-decoration:none;">AI</a><a href="/stories/" style="color:#c9d1d9;text-decoration:none;">Stories</a><a href="/code/" style="color:#c9d1d9;text-decoration:none;">Code</a><a href="/search/" style="color:#c9d1d9;text-decoration:none;">Search</a><a href="/cloud/" style="color:#c9d1d9;text-decoration:none;">Cloud</a><a href="/track/" style="color:#c9d1d9;text-decoration:none;">Track</a></div></body>';
}

upstream ak_outpost {
    server 127.0.0.1:9010;
}
upstream ak_server {
    server 127.0.0.1:9008;
}
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

log_format track escape=json '{"time":"$time_iso8601","gcp":"$http_x_via_gcp","ip":"$remote_addr","method":"$request_method","uri":"$request_uri","status":$status,"in":$request_length,"out":$bytes_sent,"ua":"$http_user_agent"}';

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name home.palashkantikundu.in;

    ssl_certificate     /etc/letsencrypt/live/home.palashkantikundu.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/home.palashkantikundu.in/privkey.pem;

    client_max_body_size 512M;
    client_body_buffer_size 128k;

    access_log /var/log/nginx/track.log track;

    # Expose Outpost traffic directly so browser auth flow and internal calls do not fail
    location /outpost.goauthentik.io/ {
        proxy_pass http://ak_outpost;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    # Internal auth checking block
    location /ak-auth-ai {
        internal;
        proxy_pass http://ak_outpost/outpost.goauthentik.io/auth/nginx;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URL $scheme://$http_host$request_uri;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_no_cache 1;
        proxy_cache_bypass 1;
    }

    # Redirect unauthenticated users directly to Outpost start portal
    location @ak-sso-ai {
        internal;
        return 302 /outpost.goauthentik.io/start?rd=$scheme://$http_host$request_uri;
    }

    location / {
        root /var/www/dashboard;
        index index.html;

        sub_filter_once off;
        sub_filter_types text/html;
        sub_filter '</body>' '$gcp_overlay<div id="global-nav-bar" style="position:absolute;bottom:16px;right:16px;z-index:999999;display:flex;gap:8px;background:rgba(22,27,34,0.95);padding:6px 12px;border-radius:20px;border:1px solid #30363d;box-shadow:0 4px 12px rgba(0,0,0,0.5);font-family:sans-serif;font-size:13px;backdrop-filter:blur(4px);"><a href="/" style="color:#58a6ff;text-decoration:none;font-weight:600;">Home</a><span style="color:#484f58;">|</span><a href="/ai/" style="color:#c9d1d9;text-decoration:none;">AI</a><a href="/stories/" style="color:#c9d1d9;text-decoration:none;">Stories</a><a href="/code/" style="color:#c9d1d9;text-decoration:none;">Code</a><a href="/search/" style="color:#c9d1d9;text-decoration:none;">Search</a><a href="/cloud/" style="color:#c9d1d9;text-decoration:none;">Cloud</a><a href="/track/" style="color:#c9d1d9;text-decoration:none;">Track</a></div></body>';
    }

    # Authentik SSO Core (matches both /sso and /sso/*)
    location /sso {
        proxy_pass http://ak_server;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
    }

    # 1. Local AI App
    location /ai/ {
        auth_request /ak-auth-ai;
        auth_request_set $authentik_username $upstream_http_x_authentik_username;
        auth_request_set $authentik_groups $upstream_http_x_authentik_groups;
        auth_request_set $authentik_email $upstream_http_x_authentik_email;
        auth_request_set $authentik_name $upstream_http_x_authentik_name;
        auth_request_set $authentik_uid $upstream_http_x_authentik_uid;
        error_page 401 = @ak-sso-ai;

        proxy_pass http://127.0.0.1:3001/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Accept-Encoding "";
        proxy_set_header X-Authentik-Username $authentik_username;
        proxy_set_header X-Authentik-Groups $authentik_groups;
        proxy_set_header X-Authentik-Email $authentik_email;
        proxy_set_header X-Authentik-Name $authentik_name;
        proxy_set_header X-Authentik-UID $authentik_uid;

        sub_filter_once off;
        sub_filter_types text/html;
        sub_filter '</body>' '$gcp_overlay<div id="global-nav-bar" style="position:absolute;top:8px;right:80px;z-index:999999;display:flex;gap:8px;background:rgba(22,27,34,0.95);padding:6px 12px;border-radius:20px;border:1px solid #30363d;box-shadow:0 4px 12px rgba(0,0,0,0.5);font-family:sans-serif;font-size:13px;backdrop-filter:blur(4px);"><a href="/" style="color:#58a6ff;text-decoration:none;font-weight:600;">Home</a><span style="color:#484f58;">|</span><a href="/ai/" style="color:#c9d1d9;text-decoration:none;">AI</a><a href="/stories/" style="color:#c9d1d9;text-decoration:none;">Stories</a><a href="/code/" style="color:#c9d1d9;text-decoration:none;">Code</a><a href="/search/" style="color:#c9d1d9;text-decoration:none;">Search</a><a href="/cloud/" style="color:#c9d1d9;text-decoration:none;">Cloud</a><a href="/track/" style="color:#c9d1d9;text-decoration:none;">Track</a></div></body>';
    }

    location ^~ /api/public/ {
        proxy_pass http://127.0.0.1:3001/api/public/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /api/ {
        auth_request /ak-auth-ai;
        auth_request_set $authentik_username $upstream_http_x_authentik_username;
        auth_request_set $authentik_groups $upstream_http_x_authentik_groups;
        auth_request_set $authentik_email $upstream_http_x_authentik_email;
        auth_request_set $authentik_name $upstream_http_x_authentik_name;
        auth_request_set $authentik_uid $upstream_http_x_authentik_uid;
        error_page 401 = @ak-sso-ai;

        proxy_pass http://127.0.0.1:3001/api/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Authentik-Username $authentik_username;
        proxy_set_header X-Authentik-Groups $authentik_groups;
        proxy_set_header X-Authentik-Email $authentik_email;
        proxy_set_header X-Authentik-Name $authentik_name;
        proxy_set_header X-Authentik-UID $authentik_uid;
    }

    # 2. Chat Stories App
    location /stories/ {
        auth_request /ak-auth-ai;
        auth_request_set $authentik_username $upstream_http_x_authentik_username;
        auth_request_set $authentik_groups $upstream_http_x_authentik_groups;
        auth_request_set $authentik_email $upstream_http_x_authentik_email;
        auth_request_set $authentik_name $upstream_http_x_authentik_name;
        auth_request_set $authentik_uid $upstream_http_x_authentik_uid;
        error_page 401 = @ak-sso-ai;

        proxy_pass http://127.0.0.1:3002/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Accept-Encoding "";
        proxy_set_header X-Authentik-Username $authentik_username;
        proxy_set_header X-Authentik-Groups $authentik_groups;
        proxy_set_header X-Authentik-Email $authentik_email;
        proxy_set_header X-Authentik-Name $authentik_name;
        proxy_set_header X-Authentik-UID $authentik_uid;

        sub_filter_once off;
        sub_filter_types text/html;
        sub_filter '</body>' '$gcp_overlay<div id="global-nav-bar" style="position:absolute;bottom:16px;right:16px;z-index:999999;display:flex;gap:8px;background:rgba(22,27,34,0.95);padding:6px 12px;border-radius:20px;border:1px solid #30363d;box-shadow:0 4px 12px rgba(0,0,0,0.5);font-family:sans-serif;font-size:13px;backdrop-filter:blur(4px);"><a href="/" style="color:#58a6ff;text-decoration:none;font-weight:600;">Home</a><span style="color:#484f58;">|</span><a href="/ai/" style="color:#c9d1d9;text-decoration:none;">AI</a><a href="/stories/" style="color:#c9d1d9;text-decoration:none;">Stories</a><a href="/code/" style="color:#c9d1d9;text-decoration:none;">Code</a><a href="/search/" style="color:#c9d1d9;text-decoration:none;">Search</a><a href="/cloud/" style="color:#c9d1d9;text-decoration:none;">Cloud</a><a href="/track/" style="color:#c9d1d9;text-decoration:none;">Track</a></div></body>';
    }

    location /story/ {
        auth_request /ak-auth-ai;
        auth_request_set $authentik_username $upstream_http_x_authentik_username;
        auth_request_set $authentik_groups $upstream_http_x_authentik_groups;
        auth_request_set $authentik_email $upstream_http_x_authentik_email;
        auth_request_set $authentik_name $upstream_http_x_authentik_name;
        auth_request_set $authentik_uid $upstream_http_x_authentik_uid;
        error_page 401 = @ak-sso-ai;

        proxy_pass http://127.0.0.1:3002/story/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Authentik-Username $authentik_username;
        proxy_set_header X-Authentik-Groups $authentik_groups;
        proxy_set_header X-Authentik-Email $authentik_email;
        proxy_set_header X-Authentik-Name $authentik_name;
        proxy_set_header X-Authentik-UID $authentik_uid;

        sub_filter_once off;
        sub_filter_types text/html;
        sub_filter '</body>' '$gcp_overlay<div id="global-nav-bar" style="position:absolute;bottom:16px;right:16px;z-index:999999;display:flex;gap:8px;background:rgba(22,27,34,0.95);padding:6px 12px;border-radius:20px;border:1px solid #30363d;box-shadow:0 4px 12px rgba(0,0,0,0.5);font-family:sans-serif;font-size:13px;backdrop-filter:blur(4px);"><a href="/" style="color:#58a6ff;text-decoration:none;font-weight:600;">Home</a><span style="color:#484f58;">|</span><a href="/ai/" style="color:#c9d1d9;text-decoration:none;">AI</a><a href="/stories/" style="color:#c9d1d9;text-decoration:none;">Stories</a><a href="/code/" style="color:#c9d1d9;text-decoration:none;">Code</a><a href="/search/" style="color:#c9d1d9;text-decoration:none;">Search</a><a href="/cloud/" style="color:#c9d1d9;text-decoration:none;">Cloud</a><a href="/track/" style="color:#c9d1d9;text-decoration:none;">Track</a></div></body>';
    }

    location /media/ {
        auth_request /ak-auth-ai;
        auth_request_set $authentik_username $upstream_http_x_authentik_username;
        auth_request_set $authentik_groups $upstream_http_x_authentik_groups;
        auth_request_set $authentik_email $upstream_http_x_authentik_email;
        auth_request_set $authentik_name $upstream_http_x_authentik_name;
        auth_request_set $authentik_uid $upstream_http_x_authentik_uid;
        error_page 401 = @ak-sso-ai;

        proxy_pass http://127.0.0.1:3002/media/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Authentik-Username $authentik_username;
        proxy_set_header X-Authentik-Groups $authentik_groups;
        proxy_set_header X-Authentik-Email $authentik_email;
        proxy_set_header X-Authentik-Name $authentik_name;
        proxy_set_header X-Authentik-UID $authentik_uid;
    }

    # 3. SearXNG
    location /search/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Script-Name /search;
        proxy_set_header Accept-Encoding "";

        sub_filter_once off;
        sub_filter_types text/html;
        sub_filter '</body>' '$gcp_overlay<div id="global-nav-bar" style="position:absolute;bottom:16px;right:16px;z-index:999999;display:flex;gap:8px;background:rgba(22,27,34,0.95);padding:6px 12px;border-radius:20px;border:1px solid #30363d;box-shadow:0 4px 12px rgba(0,0,0,0.5);font-family:sans-serif;font-size:13px;backdrop-filter:blur(4px);"><a href="/" style="color:#58a6ff;text-decoration:none;font-weight:600;">Home</a><span style="color:#484f58;">|</span><a href="/ai/" style="color:#c9d1d9;text-decoration:none;">AI</a><a href="/stories/" style="color:#c9d1d9;text-decoration:none;">Stories</a><a href="/code/" style="color:#c9d1d9;text-decoration:none;">Code</a><a href="/search/" style="color:#c9d1d9;text-decoration:none;">Search</a><a href="/cloud/" style="color:#c9d1d9;text-decoration:none;">Cloud</a><a href="/track/" style="color:#c9d1d9;text-decoration:none;">Track</a></div></body>';
    }

    # 4. Nextcloud
    location /cloud/ {
        proxy_pass http://127.0.0.1:8082/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Port 443;
        
        proxy_max_temp_file_size 2048m;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_set_header Accept-Encoding "";

        sub_filter_once off;
        sub_filter_types text/html;
        sub_filter '</body>' '$cloud_inject';
    }

    location /.well-known/carddav { return 301 $scheme://$host/cloud/remote.php/dav; }
    location /.well-known/caldav { return 301 $scheme://$host/cloud/remote.php/dav; }

    # 5. Code Hoster
    location /code/ {
        proxy_pass http://127.0.0.1:9000/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Accept-Encoding "";

        sub_filter_once off;
        sub_filter_types text/html;
        sub_filter '</body>' '$gcp_overlay<div id="global-nav-bar" style="position:absolute;bottom:16px;right:16px;z-index:999999;display:flex;gap:8px;background:rgba(22,27,34,0.95);padding:6px 12px;border-radius:20px;border:1px solid #30363d;box-shadow:0 4px 12px rgba(0,0,0,0.5);font-family:sans-serif;font-size:13px;backdrop-filter:blur(4px);"><a href="/" style="color:#58a6ff;text-decoration:none;font-weight:600;">Home</a><span style="color:#484f58;">|</span><a href="/ai/" style="color:#c9d1d9;text-decoration:none;">AI</a><a href="/stories/" style="color:#c9d1d9;text-decoration:none;">Stories</a><a href="/code/" style="color:#c9d1d9;text-decoration:none;">Code</a><a href="/search/" style="color:#c9d1d9;text-decoration:none;">Search</a><a href="/cloud/" style="color:#c9d1d9;text-decoration:none;">Cloud</a><a href="/track/" style="color:#c9d1d9;text-decoration:none;">Track</a></div></body>';
    }

    # 6. Request tracking dashboard
    location /track/ {
        auth_request /ak-auth-ai;
        auth_request_set $authentik_username $upstream_http_x_authentik_username;
        auth_request_set $authentik_groups $upstream_http_x_authentik_groups;
        auth_request_set $authentik_email $upstream_http_x_authentik_email;
        auth_request_set $authentik_name $upstream_http_x_authentik_name;
        auth_request_set $authentik_uid $upstream_http_x_authentik_uid;
        error_page 401 = @ak-sso-ai;

        proxy_pass http://127.0.0.1:8093/;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Accept-Encoding "";
        proxy_set_header X-Authentik-Username $authentik_username;
        proxy_set_header X-Authentik-Groups $authentik_groups;
        proxy_set_header X-Authentik-Email $authentik_email;
        proxy_set_header X-Authentik-Name $authentik_name;
        proxy_set_header X-Authentik-UID $authentik_uid;

        sub_filter_once off;
        sub_filter_types text/html;
        sub_filter '</body>' '$gcp_overlay<div id="global-nav-bar" style="position:absolute;top:8px;right:80px;z-index:999999;display:flex;gap:8px;background:rgba(22,27,34,0.95);padding:6px 12px;border-radius:20px;border:1px solid #30363d;box-shadow:0 4px 12px rgba(0,0,0,0.5);font-family:sans-serif;font-size:13px;backdrop-filter:blur(4px);"><a href="/" style="color:#58a6ff;text-decoration:none;font-weight:600;">Home</a><span style="color:#484f58;">|</span><a href="/ai/" style="color:#c9d1d9;text-decoration:none;">AI</a><a href="/stories/" style="color:#c9d1d9;text-decoration:none;">Stories</a><a href="/code/" style="color:#c9d1d9;text-decoration:none;">Code</a><a href="/search/" style="color:#c9d1d9;text-decoration:none;">Search</a><a href="/cloud/" style="color:#c9d1d9;text-decoration:none;">Cloud</a><a href="/track/" style="color:#c9d1d9;text-decoration:none;">Track</a></div></body>';
    }
}

server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name home.palashkantikundu.in;
    return 301 https://$host$request_uri;
}
EOF

echo "=== 4. Reloading Nginx ==="
sudo mkdir -p /var/log/nginx
sudo rm -f /etc/nginx/sites-enabled/default
sudo ln -sf /etc/nginx/sites-available/homeserver /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

echo "=== 4b. Request Tracking Dashboard ==="
sudo touch /var/log/nginx/track.log
sudo chmod 640 /var/log/nginx/track.log
if [ -n "$CHAT_DIR" ] && [ -f "$CHAT_DIR/server/track_dashboard.py" ]; then
    if ! pgrep -f "server/track_dashboard.py" > /dev/null; then
        nohup python3 "$CHAT_DIR/server/track_dashboard.py" --log /var/log/nginx/track.log \
            > /tmp/track_dashboard.log 2>&1 &
        echo "    Request Tracker started on http://localhost:8093/track/"
    else
        echo "    Request Tracker already running"
    fi
else
    echo "    Skipping python tracker start: \$CHAT_DIR is unset or file missing."
fi

echo "=== 5. Dynamic Nextcloud Container Configuration ==="
NC_CONTAINER=$(docker ps --format '{{.Names}}' | grep -E 'cloud-app|nextcloud' | head -n 1)

if [ -n "$NC_CONTAINER" ]; then
    echo "Found Nextcloud container: $NC_CONTAINER"
    
    docker exec --user www-data "$NC_CONTAINER" php occ files:scan --all
    docker exec --user www-data "$NC_CONTAINER" php occ config:system:set overwritewebroot --value="/cloud"
    docker exec --user www-data "$NC_CONTAINER" php occ config:system:set overwrite.cli.url --value="https://home.palashkantikundu.in/cloud"
    docker exec --user www-data "$NC_CONTAINER" php occ config:system:set overwriteprotocol --value="https"
    docker exec --user www-data "$NC_CONTAINER" php occ config:system:set trusted_domains 0 --value="*"
    docker exec --user www-data "$NC_CONTAINER" php occ config:system:set files_external_allow_create_new_local --value="true" --type=boolean
    docker exec --user www-data "$NC_CONTAINER" php occ app:enable files_external

    echo "Restarting Nextcloud container..."
    docker restart "$NC_CONTAINER"
else
    echo "Error: Nextcloud container not found!"
    exit 1
fi

echo "=== All tasks completed successfully ==="
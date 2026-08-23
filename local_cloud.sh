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
    </div>
</body>
</html>
EOF

echo "=== 2b. Creating nav overlay stylesheet ==="
cat << 'EOF' | sudo tee /var/www/dashboard/nav-overlay.css > /dev/null
/* Floating vertical icon dock, identical on desktop and mobile */
#global-nav-wrapper {
    position: fixed;
    top: 50%;
    right: 12px;
    transform: translateY(-50%);
    z-index: 999999;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
#global-nav-wrapper .nav-dock {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    background: rgba(22, 27, 34, 0.9);
    border: 1px solid #30363d;
    border-radius: 22px;
    padding: 8px 5px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.5);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
}
#global-nav-wrapper .nav-dock a {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    font-size: 16px;
    line-height: 1;
    border-radius: 50%;
    text-decoration: none;
    opacity: 0.82;
    transition: opacity .15s ease, background-color .15s ease, transform .15s ease;
}
#global-nav-wrapper .nav-dock a:hover,
#global-nav-wrapper .nav-dock a:active {
    opacity: 1;
    background: rgba(88,166,255,0.16);
    transform: scale(1.08);
}
#global-nav-wrapper .nav-dock a.home { opacity: 1; }
#global-nav-wrapper .nav-dock .sep {
    width: 16px;
    height: 1px;
    background: #30363d;
    margin: 3px 0;
}
#global-nav-wrapper .nav-dock a.active {
    opacity: 1;
    background: rgba(88,166,255,0.22);
    box-shadow: inset 0 0 0 1px rgba(88,166,255,0.5);
}
/* Label tooltip, flyout to the left so it never clips off-screen */
#global-nav-wrapper .nav-dock a::after {
    content: attr(data-label);
    position: absolute;
    right: 38px;
    top: 50%;
    transform: translateY(-50%);
    background: rgba(22,27,34,0.95);
    border: 1px solid #30363d;
    color: #c9d1d9;
    font-size: 12px;
    font-weight: 500;
    padding: 4px 9px;
    border-radius: 6px;
    white-space: nowrap;
    opacity: 0;
    pointer-events: none;
    transition: opacity .15s ease;
}
#global-nav-wrapper .nav-dock a:hover::after { opacity: 1; }
@media (max-width: 600px) {
    #global-nav-wrapper .nav-dock a::after { display: none; }
    /* Sleeker mobile treatment: slimmer capsule, smaller icons, more glass */
    #global-nav-wrapper .nav-dock {
        gap: 0;
        padding: 6px 4px;
        border-radius: 18px;
        background: rgba(22, 27, 34, 0.72);
        border-color: rgba(48, 54, 61, 0.6);
        box-shadow: 0 2px 10px rgba(0,0,0,0.35);
    }
    #global-nav-wrapper .nav-dock a {
        width: 26px;
        height: 26px;
        font-size: 14px;
        opacity: 0.7;
    }
    #global-nav-wrapper .nav-dock a.home { opacity: 0.9; }
    #global-nav-wrapper .nav-dock .sep {
        width: 12px;
        margin: 2px 0;
    }
}
EOF

sudo chown -R www-data:www-data /var/www/dashboard
sudo chmod -R 755 /var/www/dashboard

echo "=== 3. Writing Nginx Master Configuration ==="
cat << 'EOF' | sudo tee /etc/nginx/sites-available/homeserver > /dev/null
map $http_x_via_gcp $gcp_overlay {
    "true"  '<div id="gcp-overlay" style="position:fixed;top:0;left:0;width:100vw;height:100vh;background-color:rgba(239,68,68,0.05);border-top:3px solid rgba(239,68,68,0.6);pointer-events:none;z-index:999998;"></div>';
    default '';
}

map $http_user_agent $nav_overlay {
    "~*nextcloud-(android|ios|desktop)" '';
    default '$gcp_overlay<link rel="stylesheet" href="/nav-overlay.css">
        <div id="global-nav-wrapper">
        <div class="nav-dock">
            <a href="/" class="home" data-label="Home">🏠</a>
            <div class="sep"></div>
            <a href="/ai/" data-label="AI">🤖</a>
            <a href="/stories/" data-label="Stories">📖</a>
            <a href="/code/" data-label="Code">💻</a>
            <a href="/search/" data-label="Search">🔍</a>
            <a href="/cloud/" data-label="Cloud">☁️</a>
        </div>
        </div>
        <script>
            (function(){
            var path = location.pathname;
            var links = document.querySelectorAll("#global-nav-wrapper .nav-dock a[href]");
            var best = null;
            links.forEach(function(a){
                var href = a.getAttribute("href");
                if (href === "/" ? path === "/" : path.indexOf(href) === 0) {
                if (!best || href.length > best.getAttribute("href").length) best = a;
                }
            });
            if (best) best.classList.add("active");
            })();
        </script>
        </body>';
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

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name home.palashkantikundu.in;

    ssl_certificate     /etc/letsencrypt/live/home.palashkantikundu.in/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/home.palashkantikundu.in/privkey.pem;

    client_max_body_size 512M;
    client_body_buffer_size 128k;

    # Global HTML substitution settings (inherited by location blocks below)
    sub_filter_once off;
    sub_filter_types text/html;
    sub_filter '</body>' '$nav_overlay';

    # Expose Outpost traffic directly
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

    location = /nav-overlay.css {
        alias /var/www/dashboard/nav-overlay.css;
        add_header Cache-Control "no-cache";
    }

    location / {
        root /var/www/dashboard;
        index index.html;
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
    docker exec --user www-data "$NC_CONTAINER" php occ config:system:set skeletondirectory --value=""
    
    echo "Restarting Nextcloud container..."
    docker restart "$NC_CONTAINER"
else
    echo "Error: Nextcloud container not found!"
    exit 1
fi

echo "=== All tasks completed successfully ==="
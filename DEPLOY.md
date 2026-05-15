# 服务器部署速查 (xhs-parser)

## 目标

- 站点目录: `/www/wwwroot/xhs-parser/`
- 端口: Nginx **39083** → Gunicorn 127.0.0.1:**8103**
- 访问: <http://8.138.150.200:39083>
- venv: `/www/wwwroot/xhs-parser/backend/.venv` (Python 3.8)
- systemd: `xhs-parser.service`

## 一次性部署 (本地一行命令)

在本地项目目录 (`视频平台解析/`) 的父目录执行：

```bash
# 打包 (排除 venv / data / __pycache__)
tar --exclude='视频平台解析/backend/.venv' \
    --exclude='视频平台解析/data' \
    --exclude='**/__pycache__' \
    -czf /tmp/xhs-parser.tar.gz 视频平台解析/
scp /tmp/xhs-parser.tar.gz server:/tmp/xhs-parser.tar.gz
ssh server 'bash -s' < deploy_remote.sh
```

## 服务器端 systemd 单元

`/etc/systemd/system/xhs-parser.service`:

```ini
[Unit]
Description=XHS Parser (FastAPI)
After=network.target

[Service]
Type=simple
User=www
WorkingDirectory=/www/wwwroot/xhs-parser/backend
Environment="PATH=/www/wwwroot/xhs-parser/backend/.venv/bin"
Environment="PORT=8103"
ExecStart=/www/wwwroot/xhs-parser/backend/.venv/bin/gunicorn \
  -k uvicorn.workers.UvicornWorker \
  -w 2 -b 127.0.0.1:8103 \
  --access-logfile /www/wwwroot/xhs-parser/logs/access.log \
  --error-logfile /www/wwwroot/xhs-parser/logs/error.log \
  app.main:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## Nginx 配置

`/www/server/panel/vhost/nginx/xhs-parser.conf`:

```nginx
server {
    listen 39083;
    server_name 8.138.150.200;
    client_max_body_size 100m;

    location / {
        proxy_pass http://127.0.0.1:8103;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;     # 流式下载
        proxy_read_timeout 300s;
    }

    access_log /www/wwwlogs/xhs-parser.access.log;
    error_log  /www/wwwlogs/xhs-parser.error.log;
}
```

## 更新代码

```bash
tar --exclude='视频平台解析/backend/.venv' --exclude='视频平台解析/data' \
    --exclude='**/__pycache__' -czf /tmp/xhs-parser.tar.gz 视频平台解析/
scp /tmp/xhs-parser.tar.gz server:/tmp/
ssh server 'tar -xzf /tmp/xhs-parser.tar.gz -C /tmp/ && \
  rsync -a --delete --exclude=.venv --exclude=data /tmp/视频平台解析/ /www/wwwroot/xhs-parser/ && \
  chown -R www:www /www/wwwroot/xhs-parser && \
  systemctl restart xhs-parser'
```

## 排错

```bash
systemctl status xhs-parser
journalctl -u xhs-parser -f
tail -f /www/wwwroot/xhs-parser/logs/error.log
curl -s http://127.0.0.1:8103/api/health
```

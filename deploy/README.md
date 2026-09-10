# Развёртывание Eurith API

Сервер разворачивает только уже проверенный commit или tag из GitHub. Рабочие
файлы на сервере вручную не редактируются.

После одноразовой настройки обновление выполняется из `/opt/eurith`. Скрипт
намеренно не имеет версии по умолчанию: нужно передать проверенный commit или tag:

```bash
./backend/deploy/deploy.sh 0123456789abcdef
# или
./backend/deploy/deploy.sh v1.2.0
```

Скрипт останавливается при незакоммиченных серверных изменениях, делает дамп
PostgreSQL, собирает новый образ, применяет Alembic-миграции, проверяет публичный
`/health` и возвращает предыдущий commit, если проверка не прошла.

## Новое изменение схемы БД

Модель SQLAlchemy и миграция меняются в одном commit. Локально, с запущенной
тестовой PostgreSQL:

```bash
alembic revision --autogenerate -m "add example field"
alembic upgrade head
```

Автоматически созданную миграцию нужно прочитать и проверить до commit. Миграции
production выполняет `deploy.sh` после резервной копии и до перезапуска API.

Пароли, `.env` и Firebase service account не хранятся в GitHub.

## Центр обновлений Android: volume, nginx и recovery

APK не является частью Docker image: он хранится в постоянном host-volume.
Создайте его до первого выпуска и не заменяйте при `docker compose up`:

```text
host:  /opt/eurith/releases
api:   /var/lib/eurith/releases:rw
nginx: /srv/eurith/releases:ro
```

Пример Docker mounts: `/opt/eurith/releases:/var/lib/eurith/releases:rw` для
API и `/opt/eurith/releases:/srv/eurith/releases:ro` для nginx. На хосте:

Use a dedicated shared group. Never assume that an image UID/GID equals a host
account with the same number. Resolve the API UID from the built image and the
shared GID from the host, then create the volume with those actual numeric IDs:

```bash
groupadd --system eurith-releases
export RELEASE_SHARED_GID="$(getent group eurith-releases | cut -d: -f3)"
API_UID="$(docker compose run --rm --no-deps --entrypoint id api -u)"
install -d -o "$API_UID" -g "$RELEASE_SHARED_GID" -m 2770 /opt/eurith/releases
install -d -o "$API_UID" -g "$RELEASE_SHARED_GID" -m 2770 /opt/eurith/releases/.staging
install -d -o "$API_UID" -g "$RELEASE_SHARED_GID" -m 2770 /opt/eurith/releases/android/sha256
usermod -aG eurith-releases eurith
usermod -aG eurith-releases www-data
```

Use the resolved GID in Compose for every process that touches the volume:

```yaml
services:
  api:
    group_add:
      - "${RELEASE_SHARED_GID:?set RELEASE_SHARED_GID from getent}"
    volumes:
      - /opt/eurith/releases:/var/lib/eurith/releases:rw
  nginx:
    group_add:
      - "${RELEASE_SHARED_GID:?set RELEASE_SHARED_GID from getent}"
    volumes:
      - /opt/eurith/releases:/srv/eurith/releases:ro
```

For host nginx, add its actual service user (`www-data` above; often `nginx` on
RPM-based systems) to `eurith-releases` and expose the release tree read-only
inside its service mount namespace (for example with systemd
`BindReadOnlyPaths=/opt/eurith/releases:/srv/eurith/releases`). Restart nginx and verify with
`sudo -u www-data test -r /opt/eurith/releases/android/sha256/<digest>.apk`.
Directories are `2770`: group members can traverse/write and setgid preserves
the shared GID. Staging files stay `0600` while validation is in progress;
after atomic finalization the API sets the final APK to `0640`, so nginx can
read it but cannot change it. The API runtime identity owns writes; the host
cleanup service runs as `eurith` with `SupplementaryGroups=eurith-releases`.
Paths deliberately differ by mount namespace: the API/container uses
`RELEASE_STORAGE_ROOT=/var/lib/eurith/releases`, while host cleanup uses
`RELEASE_STORAGE_ROOT=/opt/eurith/releases`.

### Release secrets and environment files

Generate three independent 256-bit values directly into the protected API
EnvironmentFile. Do this once on the server as root; never replace them with
shared or human-created passwords:

```bash
install -o root -g eurith -m 0640 /dev/null /etc/eurith/api-release.env
{
  printf 'RELEASE_PUBLISHER_TOKEN='
  openssl rand -hex 32
  printf 'RELEASE_OPERATOR_TOKEN='
  openssl rand -hex 32
  printf 'GITHUB_WEBHOOK_SECRET='
  openssl rand -hex 32
  printf 'RELEASE_STORAGE_ROOT=/var/lib/eurith/releases\n'
} > /etc/eurith/api-release.env
chown root:eurith /etc/eurith/api-release.env
chmod 0640 /etc/eurith/api-release.env
```

- `RELEASE_PUBLISHER_TOKEN` authenticates CI publication, CI-run binding and
  withdrawal endpoints. Store the matching value as a masked GitHub Actions
  repository/environment secret and in `/etc/eurith/api-release.env`; do not
  give it to cleanup.
- `RELEASE_OPERATOR_TOKEN` controls the manual mandatory-update endpoint.
  Store its client copy in the operator password manager/secret vault and its
  server copy only in `/etc/eurith/api-release.env`; do not put it in GitHub
  Actions or cleanup.
- `GITHUB_WEBHOOK_SECRET` verifies `X-Hub-Signature-256` on push webhooks.
  Copy the matching value into the GitHub repository webhook **Secret** field
  and keep its server copy only in `/etc/eurith/api-release.env`; it is not a
  bearer token and must not be reused as either release token.

### CI retry binding contract

The protected `POST /internal/app-releases/lanes/android/{channel}/ci-run`
endpoint is **latest-attempt-wins for the lane's current webhook target SHA**.
The workflow must bind its own CI run immediately before publishing. A retry
for the exact same `source_commit` atomically replaces the earlier run ID; the
earlier run then receives `409` from every subsequent publish attempt, while an
idempotent repeat of the newest binding remains successful. A bind for any SHA
other than the lane's current webhook target receives `409` and cannot change
the lane. The next accepted GitHub push advances the target SHA and clears the
run binding before a new workflow binds.

Keep GitHub workflow concurrency serialized per release lane. The backend also
serializes bind and publish decisions with the same PostgreSQL lane lock, so a
late request can never publish after a newer binding has committed. Only the
trusted release workflow may receive `RELEASE_PUBLISHER_TOKEN`; never expose
the binding endpoint or token to an artifact, pull-request job, or mobile
client.

Create `/etc/eurith/release-cleanup.env` separately. It contains only
`DATABASE_URL` and host-visible
`RELEASE_STORAGE_ROOT=/opt/eurith/releases`; it never receives publisher,
operator or webhook secrets. Both EnvironmentFiles are `root:eurith` mode
`0640`; never copy them into checkout, image, command output, logs or unit
files. Point only the API/container service at `api-release.env`, and only the
host cleanup unit below at `release-cleanup.env`.

Подключите `deploy/nginx/releases.conf` к HTTPS virtual host. До reload проверьте
конфигурацию, затем примените её:

```bash
nginx -t && systemctl reload nginx
```

`/_release_files/` — internal location: внешний HTTP-клиент не получает доступ к
пути volume напрямую. Nginx допускает запрос multipart до 256 MiB, чтобы вместить
обёртку multipart; API по-прежнему отклоняет сам APK payload больше 250 MiB.
Request buffering выключен, к Uvicorn/container port извне доступа быть не должно.

### Ежедневная очистка и контроль места

Timer запускает только dry-run. Создайте `/etc/systemd/system/eurith-release-cleanup.service`:

```ini
[Service]
Type=oneshot
User=eurith
Group=eurith
SupplementaryGroups=eurith-releases
UMask=0007
WorkingDirectory=/opt/eurith/backend
EnvironmentFile=/etc/eurith/release-cleanup.env
ExecStart=/opt/eurith/venv/bin/python -m scripts.cleanup_app_releases
```

и `/etc/systemd/system/eurith-release-cleanup.timer`:

```ini
[Timer]
OnCalendar=*-*-* 02:10:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
```

После `systemctl daemon-reload && systemctl enable --now eurith-release-cleanup.timer`
оператор читает JSON-lines из `journalctl -u eurith-release-cleanup.service` и лишь
затем запускает явный apply с тем же защищённым EnvironmentFile. Автоматический
`--apply` в timer/cron запрещён. Оставляйте alert при свободном месте volume
ниже 20% и расследуйте любой unexpected candidate. Скрипт не следует symlink,
не удаляет свежие staging parts, shared SHA, published/latest или mandatory APK.
Старый withdrawn APK удаляется только после retention, его DB row остаётся с
`artifact_deleted_at` сначала фиксируется как deletion intent для аудита, затем
файл удаляется; следующий запуск устраняет файл, оставшийся после сбоя между
этими фазами. Cleanup и direct publication используют один session advisory
lock на закреплённом PostgreSQL connection, поэтому финализация APK не
соревнуется с orphan scan.

### Backup, restore и rollback

Бэкап состоит из согласованных PostgreSQL dump и `/opt/eurith/releases`.
При восстановлении сначала восстановите volume в закрытый путь, затем для каждого
APK сравните `sha256sum` с `app_releases.artifact_sha256`, и лишь затем замените
production mount. Missing artifact у published release — operational incident,
а не повод перепубликовать тот же versionCode.

Withdraw выполняется защищённым release endpoint: latest перестаёт предлагать
релиз, download отвечает `410`; для direct APK исправление выпускают с большим
versionCode. EAS OTA откатывают средствами EAS rollout/rollback, не удалением
истории release registry. Direct и Google Play — разные delivery lanes: Play
клиент не получает APK с нашего сервера.

Текущая migration реестра additive: rollback кода допустим только после проверки
совместимости со схемой. Любая будущая destructive migration требует отдельного
expand/contract плана, backup и явного production gate.

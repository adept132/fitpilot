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
owner: uid 1000, gid 1000 (пользователь API)
```

Пример Docker mounts: `/opt/eurith/releases:/var/lib/eurith/releases:rw` для
API и `/opt/eurith/releases:/srv/eurith/releases:ro` для nginx. На хосте:

```bash
install -d -o 1000 -g 1000 -m 0750 /opt/eurith/releases
```

В `.env` API задаются `DATABASE_URL`, `RELEASE_STORAGE_ROOT=/var/lib/eurith/releases`,
`RELEASE_PUBLISHER_TOKEN`, `RELEASE_OPERATOR_TOKEN` и `GITHUB_WEBHOOK_SECRET`.
Значения генерируют вне репозитория, например `openssl rand -hex 32`, и передают
GitHub production environment только через секреты. Токен publisher доступен CI,
operator — только ручной аварийной процедуре; webhook secret — только GitHub и API.

Подключите `deploy/nginx/releases.conf` к HTTPS virtual host. До reload проверьте
конфигурацию, затем примените её:

```bash
nginx -t && systemctl reload nginx
```

`/_release_files/` — internal location: внешний HTTP-клиент не получает доступ к
пути volume напрямую. Upload location имеет лимит 250 MiB и выключенную request
buffering; к Uvicorn/container port извне доступа быть не должно.

### Ежедневная очистка и контроль места

Сначала запускайте только dry-run и сохраняйте JSON-lines журнал; `--apply` —
единственный режим удаления. Пример systemd timer или cron (UTC):

```cron
10 2 * * * cd /opt/eurith/backend && /opt/eurith/venv/bin/python -m scripts.cleanup_app_releases >> /var/log/eurith/release-cleanup.log 2>&1
25 2 * * * cd /opt/eurith/backend && /opt/eurith/venv/bin/python -m scripts.cleanup_app_releases --apply >> /var/log/eurith/release-cleanup.log 2>&1
```

Перед apply оператор читает dry-run; оставляйте alert при свободном месте volume
ниже 20% и расследуйте любой unexpected candidate. Скрипт не следует symlink,
не удаляет свежие staging parts, shared SHA, published/latest или mandatory APK.
Старый withdrawn APK удаляется только после retention, его DB row остаётся с
`artifact_deleted_at` для аудита. Cleanup и direct publication используют один
transaction advisory lock, поэтому финализация APK не соревнуется с orphan scan.

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

# Sound

Alertes et diffusion, campus Epitech Moulins. Voir `cdc.md` et `PLAN.md`.

## Dev local

    pip install -r requirements-dev.txt
    ruff check . && pytest

Tests DB/Redis : `TEST_DATABASE_URL` (Postgres 16) et `TEST_REDIS_URL` (ex. `redis://localhost:6379/15`) ; sans eux, les tests concernés sont ignorés.

## Interface web

Pages Jinja2 + HTMX (`app/templates`, `app/static`, servies par l'API ; aucun build front, rien à ajouter au compose). `/login` (bouton SSO → `/auth/login`), `/` (accueil, navigation selon le rôle), `/music`, `/announce` (admin + staff), `/admin` (admin : utilisateurs/rôles, enceintes avec `notify_service` et `ma_player_id`, messages pré-enregistrés). Non connecté : 302 vers `/login` ; rôle insuffisant : 403. Fragments HTMX sous `/ui/*`. Les actions appellent les routes JSON existantes (`/music/*`, `/announcements`, `/admin/users`, `/speakers`, `/presets`) avec le cookie JWT ; session expirée : renouvellement via `/auth/refresh` sinon retour au login. Charte : `docs/charte-graphique.md` (tokens clair/sombre dans `app/static/app.css`). Les polices et les icônes viennent de Google Fonts (accès Internet requis côté navigateur).

## Déploiement Portainer (stack Git)

1. Stacks > Add stack > Repository : URL du dépôt GitHub, compose path `docker-compose.yml`.
2. Renseigner les variables (cf. `.env.example`) dans l'UI : `DOMAIN`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `LOG_LEVEL`, `ENTRA_CLIENT_SECRET`, `JWT_SECRET` (32+ car.), `BOOTSTRAP_ADMIN_EMAIL` (ex. admin@example.org ; premier admin créé au démarrage, email insensible à la casse). Redirect URI Entra : `https://sound.example.org/auth/callback` (DOMAIN=sound.example.org, PUBLIC_BASE_URL=https://sound.example.org).
3. Deploy. Pour mettre à jour : push puis « Pull and redeploy » (rebuild des images sur l'hôte).

Seul Caddy expose 80/443 (TLS automatique pour `DOMAIN`). Les migrations Alembic s'exécutent au démarrage de l'API.
Sondes : `/healthz` (liveness), `/readyz` (Postgres + Redis).

## Home Assistant (annonces TTS)

Variables : `HA_URL` (ex. `http://homeassistant:8123`), `HA_TOKEN` (long-lived token, secret, jamais dans le dépôt), `HA_TTS_SERVICE` (`domaine.service`, défaut `notify.alexa_media` ; pour un autre service TTS le payload est `entity_id` + `message`), `TEST_PREFIX` (préfixe vocal forcé quand `is_test`). Sans `HA_URL`/`HA_TOKEN` l'API démarre normalement : `POST /announcements` répond 503 et le worker ne consomme pas la file.

### Musique (Music Assistant)

Variables : `MA_URL` (ex. `http://music-assistant:8095`), `MA_TOKEN` (token long durée créé dans MA, secret, requis par MA ≥ 2.7 ; jamais dans le dépôt), `MA_TIMEOUT` (secondes, défaut 10). Routes staff+admin : `GET /music/players`, `GET /music/search?q=`, `POST /music/players/{id}/{play,pause,resume,next,previous,volume}`. Sans `MA_URL` : démarrage normal, routes `/music` → 503. Ducking : pendant une annonce, le worker met en pause les players MA en lecture mappés via `speakers.ma_player_id`, puis les relance (best-effort, n'empêche jamais l'annonce). Fin de lecture : le worker écoute les événements MA (`queue_updated`/`player_updated`) ; une queue passant de `playing` à `idle` avec `repeat_mode=off` reçoit `player_queues/stop` (les Echo Dot bouclent sinon) ; jamais si la répétition est active. Après une annonce, seuls les players qui jouaient sont repris. Aucun identifiant de streaming n'est stocké ici.

API : `/speakers` et `/presets` (CRUD admin, lecture staff), `POST /announcements` (admin/staff ; `text` ou `preset_id`, `speaker_ids`, `is_test`), `GET /announcements[/{id}]`. Priorité déduite du rôle (admin=1, staff=2 ; `alert`=0 réservé aux alertes internes, phase 5). File Redis (sorted set) consommée par le worker arq (un seul consommateur sous verrou, FIFO par niveau).

## Alertes de sécurité (phase 5)

Routes staff+admin : `POST /incidents` (brouillon : `type`, `title`, `message`, `is_test`), `GET /incidents[/{id}]`, `POST /incidents/{id}/send` (corps `{"confirm": "..."}`), `POST /incidents/{id}/close`. Machine d'états `draft -> sent -> closed` (transition atomique, 409 sinon), chaque transition écrit `audit_log` (`incident_create/send/delivery/close`, `incident_send_refused`).

Double confirmation : `confirm` doit valoir exactement `ALERT_CONFIRM_TEXT` (défaut `ALERTE`) ; pour un incident de test (`is_test`, fixé à la création) le texte attendu est `ALERT_TEST_CONFIRM_TEXT` (défaut `TEST`), et le préfixe vocal `TEST_PREFIX` est forcé côté serveur.

Envoi : annonce TTS de priorité `alert` (0) sur toutes les enceintes actives, dans la file Redis existante (passe devant admin/staff ; ducking MA borné par `ALERT_DUCK_TIMEOUT`, jamais bloquant), et diffusion Teams en parallèle. Les deux canaux sont indépendants : un échec est consigné dans `incident.delivery` (`tts` / `teams`) sans bloquer l'autre. Le statut final de lecture reste dans l'annonce liée (`announcement_id`).

Teams : interface `TeamsNotifier` (`app/alerts/teams.py`). Sans configuration : `LogTeamsNotifier` (journal seulement). Si `TEAMS_APP_ID`, `TEAMS_APP_SECRET` (secret), `TEAMS_SERVICE_URL`, `TEAMS_CONVERSATION_ID` sont définis (`TEAMS_TENANT_ID` sinon `ENTRA_TENANT_ID`, `TEAMS_TIMEOUT`) : `GraphTeamsNotifier` poste une Adaptive Card (3 réponses : en sécurité sur le campus / hors campus / en danger, texte libre optionnel, `Action.Execute` verbe `incident_response`) via le Bot Framework Connector. Non testé contre un vrai tenant. UI : `/alerts`.
